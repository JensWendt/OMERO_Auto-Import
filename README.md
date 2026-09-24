# Automated OMERO Import

Automated OMERO Import is a two-stage Python workflow for importing recently
created or copied image files from a watched directory tree into OMERO.

1. `parser.py` scans a base directory, applies local import rules, derives an
	 OMERO target for each eligible file, and publishes a timestamped transfer manifest.
2. `OMERO_import.py` claims the newest ready transfer manifest, validates the requested OMERO
	 users/groups, creates missing Projects and Datasets when needed, then imports
	 each file into the resolved Dataset.

The workflow is designed for scheduled execution from PowerShell or another
scheduler. It can derive import targets from a folder convention, from Metafold
metadata sidecars, or from both.

## Current capabilities

- Recursively scans one base directory for files created within the last 24 hours (configurable).
- Uses hierarchical whitelist and blacklist rule files to control traversal and
	eligible files.
- Derives OMERO group, user, Project, and Dataset from the folder layout.
- Uses `*-metadata.json` Metafold sidecars to override path-derived target metadata.
- Supports Metafold fallback, sidecar-only, and sidecar-ignore modes.
- Validates groups and users, creates missing Projects and Datasets, and imports
	with an OMERO administrator account using `sudo` connections.
- Writes parser logs and rotating importer logs; preserves OMERO CLI logs for
	failed imports.

## Repository contents

- `parser.py`: scans a watch tree and generates an import transfer file.
- `OMERO_import.py`: imports the generated transfer file into OMERO.
- `IMPORT_WORKFLOW_DESIGN.md`: consolidated parser/importer design baseline for
	future maintenance and implementation work.
- `EXAMPLE_run-import.ps1`: PowerShell automation template.
- `EXAMPLE.import.json`: example transfer file structure.
- `EXAMPLE.credentials_auto_import.json`: credentials-file structure.
- `Metafold_example-metadata.json`: example Metafold sidecar structure.

## Prerequisites

- Python environment containing the OMERO Python client.
- Network access from the machine running the importer to the OMERO server and
	the source files.
- An OMERO account with administrator permission to impersonate target users.
- A credentials JSON file, kept somewhere safe:

	```json
	{
		"user": "auto_import",
		"password": "replace-with-a-secret"
	}
	```

`OMERO_import.py` currently uses the server host and port defined by its `HOST`
and `PORT` constants. Adjust them for the target OMERO installation.

## Quick start

Set the credentials and importer-log locations, then run the parser followed by
the importer. The parser output may be located outside the scanned tree, but it
is commonly stored in the base directory.

```powershell
$python = "C:\path\to\omero-venv\python.exe"
$basePath = "E:\PROJECTS\AUTOUPLOAD"
$transferDirectory = "E:\PROJECTS\AUTOUPLOAD\transfer_manifests"
$parserLog = "E:\PROJECTS\AUTOUPLOAD\Logs\parser.log"

$env:OMERO_CREDENTIALS = "C:\secure\credentials_auto_import.json"
$env:OMERO_IMPORT_LOG_DIR = "E:\PROJECTS\AUTOUPLOAD\Logs"
# Optional manifest archive locations. Defaults are beside the transfer directory.
$env:SUCCESS_IMPORT_DIR = "E:\PROJECTS\AUTOUPLOAD\success_imports"
$env:FAILED_IMPORT_DIR = "E:\PROJECTS\AUTOUPLOAD\failed_imports"

& $python .\parser.py $basePath $transferDirectory --log-file $parserLog --metafold fallback
if ($LASTEXITCODE -ne 0) {
		throw "Parser failed with exit code $LASTEXITCODE"
}

& $python .\OMERO_import.py $transferDirectory --tag-use self
if ($LASTEXITCODE -ne 0) {
		throw "Importer failed with exit code $LASTEXITCODE"
}
```

For unattended operation, schedule this wrapper at an interval appropriate for
your setup. Ensure overlapping runs cannot import the same files
concurrently.

## Parser

Run the parser directly:

```text
python parser.py BASE_PATH PATH_TO_OUTPUT_JSON [--log-file LOG_FILE] [--metafold MODE]
```

`MODE` is one of:

- `fallback` (default): use valid Metafold metadata when available; otherwise
	derive metadata from the folder path.
- `only`: include only files with a valid Metafold sidecar.
- `ignore`: skip files in directories containing a Metafold sidecar; parse files
	in all other directories from their paths.

The parser ignores rule files and `*-metadata.json` sidecars as import
candidates. It writes `in-place: false`, empty `Tag` arrays, and empty
`kv-pair` objects for every file in this first implementation.

`PATH_TO_OUTPUT_JSON` may be a transfer directory or the legacy path ending in
`import.json`; in either case, the parser publishes
`import_YYYY-MM-DDTHH-MM.json` in that directory. A manifest for the same
minute is never overwritten: the parser logs an error and fails.

### Folder-derived targets

The expected minimum path below the base directory is:

```text
<group>/<user>/<dataset>/<file>
```

For example, `E:\PROJECTS\AUTOUPLOAD\test_01\wendtj\NewDataset\01.tif`
selects group `test_01`, user `wendtj`, and Dataset `NewDataset`.

When exactly two folders appear between the user and the file, the first is the
Project and the second is the Dataset:

```text
<group>/<user>/<project>/<dataset>/<file>
```

For deeper layouts, exactly one directory must end in `_dataset`; its prefix is
used as the Dataset identifier. An optional directory ending in `_project`
supplies the Project identifier. Other intermediate folders are ignored.

## Rule files

Rules apply recursively and may appear at the base directory or any nested
directory:

- `.omero_import_whitelist.json`
- `.omero_import_blacklist.json`

Each rule file may contain `folders`, `suffixes`, and `files` arrays. Folder
and file entries match exact names unless they include regular-expression
metacharacters which then gets interpreted as regular expression patterns. Suffix entries are Python regular expressions matched against
the full file name. **Blacklist matches always take precedence over whitelist matches and sub-level matches always trump top-level matches**.

Example base whitelist:

```json
{
	"folders": ["test_01", "test_02"],
	"suffixes": ["\\.(czi|tif|tiff)$"],
	"files": []
}
```

Example nested blacklist:

```json
{
	"folders": ["archive"],
	"suffixes": ["\\.ome\\.tiff$"],
	"files": ["preview.tif"]
}
```

Nested suffix and file rules are added to inherited rules. Folder rules decide
whether traversal enters a directory at that level.

## Metafold metadata

When a single `*-metadata.json` file is next to an import candidate, the parser
can read OMERO metadata from the JSON node:

```json
["metafold_integration"].["external_links"].["omero"]
```

`dataset_id`, `user_name`, and `group_name` must be present and non-empty.
`project_id` is optional. Valid sidecar values override the corresponding
folder-derived group, user, Dataset, and Project values. In `fallback` mode,
missing, invalid, or ambiguous sidecars cause the parser to use the folder
layout instead.

## Import transfer file

The parser produces a nested JSON document grouped by OMERO group and user.
Each Dataset entry has a Dataset identifier, an optional Project identifier,
and a map of files. See `EXAMPLE.import.json` for the full structure.

Dataset and Project identifiers may be names or OMERO IDs. The importer uses
numeric identifiers as IDs; otherwise, it resolves an existing object by name
or creates a missing object in the requested user/group context.

Pass the transfer directory to `OMERO_import.py`. It selects the newest valid
ready manifest matching `import_YYYY-MM-DDTHH-MM.json`, atomically renames it
to `*_in-process.json`, and ignores unrelated files, temporary files, and
stale in-process manifests. A fully successful import moves its claimed
manifest to `SUCCESS_IMPORT_DIR`, changing the suffix to
`*_successfull.json`. Any failed import, missing returned image IDs, or failed
annotation moves it to `FAILED_IMPORT_DIR` with its original ready filename.
When unset, these directories default to `success_imports/` and
`failed_imports/` under the transfer directory. Existing archive files are
never overwritten; a collision leaves the claimed manifest in place for
manual recovery.

## Tag selection

The importer applies each file entry's `Tag` and `kv-pair` values to every
successfully imported image. Use `--tag-use` to select a pre-existing tag when
multiple users own tags with the same text:

- `self` (default): use a tag owned by the target user; create one for that
	user when no matching tag exists.
- `all`: use the first tag returned by OMERO, regardless of owner; create a tag
	for the target user only when no matching tag exists.
- `<omeName>`: use a tag owned by that specified OMERO user; create a tag for
	the target user when no matching tag exists.

For example, `python OMERO_import.py transfer_manifests --tag-use jane` reuses tags
owned by the OMERO user `jane` when available.

## Logging and failures


- The parser writes to `parser.log` in the transfer directory unless `--log-file` is
	provided.
- Set `OMERO_IMPORT_LOG_DIR` to choose the importer log directory. It defaults
	to `/var/log`.
- The importer rotates `omero_import.log` daily and retains 14 backups;
	`omero_import_errors.log` retains 30 backups.
- Failed OMERO CLI import logs are copied to
	`<OMERO_IMPORT_LOG_DIR>/failed_imports/<run-id>`.

## Email notifications

The importer can send one completion email to each resolved OMERO user after
their files in a manifest have been processed. It reads the recipient address
from the user's OMERO `Experimenter` record. Notifications are disabled by
default and use Python's standard-library SMTP client, so the same setup works
on Windows, Linux, and macOS without an OS-specific mail command.

After University IT provides the SMTP relay details, configure the importer
process with these environment variables:

```powershell
$env:OMERO_EMAIL_ENABLED = "true"
$env:OMERO_EMAIL_SMTP_HOST = "smtp.university.example"
$env:OMERO_EMAIL_SMTP_PORT = "587"
$env:OMERO_EMAIL_SMTP_SECURITY = "starttls" # starttls, ssl, or none
$env:OMERO_EMAIL_FROM = "omero-import@university.example"
$env:OMERO_EMAIL_SMTP_USER = "smtp-account" # omit both user and password if unused
$env:OMERO_EMAIL_SMTP_PASSWORD = "smtp-password"
```

`OMERO_EMAIL_SMTP_HOST` and `OMERO_EMAIL_FROM` are required when email is
enabled. `OMERO_EMAIL_SMTP_SECURITY` defaults to `starttls`; use `ssl` only
when the relay expects implicit TLS, or `none` only for a trusted relay that
does not require transport encryption. SMTP delivery failures and missing OMERO
email addresses are logged without changing the import result.

Each user receives one plain-text message for all outcomes. It includes file
and image totals, up to 20 successfully imported source paths, and up to 10
failed source paths with concise reasons. SMTP passwords are not stored in this
repository or in the OMERO credentials file; provide them through the process
environment or an operating-system secret-management mechanism.

Before invoking the OMERO import command for each source file, the importer
checks its size and modification time, waits two seconds, and checks again. If
either value changed, it repeats the check until the file is stable. The wait
is bounded by 30 seconds by default; a file that continues changing is treated
as a failed import and is reported in the normal failure summary. Override the
defaults with:

```powershell
$env:OMERO_FILE_STABILITY_INTERVAL = "2"
$env:OMERO_FILE_STABILITY_MAX_WAIT = "30"
```

This is a practical safeguard against ordinary copies still in progress. It
cannot prove that a producer will never reopen or modify the file later, so
the existing OMERO checksum validation remains valuable as the final check.

## Current limitations

- The parser uses a fixed 24-hour recency window.
- Tags and key-value annotations are present in the transfer file format but are not
	populated by the parser.
- The importer currently targets Datasets; Screen and Plate workflows are not
	implemented.
- Source-path mapping between Windows shares and server mount paths is not yet
	implemented.
- Files marked `in-place: true` use linked import transfer (`ln_s`), so the
	OMERO server must be able to access those source paths. Files marked
	`in-place: false` use normal upload transfer.

## Outlook
- additional flags to set the time window
- additional SQLite database containing imported paths and resulting OMERO Image IDs
- Screen/Plate as import target

## Disclaimer

This script was developed with assistance of GitHub Copilot AI, namely model GPT-5.6 Luna.
