# Automated OMERO Import

Automated OMERO Import is a two-stage Python workflow for importing recently
created or copied image files from a watched directory tree into OMERO.

1. `parser.py` scans a base directory, applies local import rules, derives an
	 OMERO target for each eligible file, and writes an `import.json` transfer file.
2. `OMERO_import.py` reads that transfer file, validates the requested OMERO
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
- `parser_design.md`: parser design notes and implemented rule behaviour.
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
$transfer_file = "E:\PROJECTS\AUTOUPLOAD\import.json"
$parserLog = "E:\PROJECTS\AUTOUPLOAD\Logs\parser.log"

$env:OMERO_CREDENTIALS = "C:\secure\credentials_auto_import.json"
$env:OMERO_IMPORT_LOG_DIR = "E:\PROJECTS\AUTOUPLOAD\Logs"

& $python .\parser.py $basePath $transfer_file --log-file $parserLog --metafold fallback
if ($LASTEXITCODE -ne 0) {
		throw "Parser failed with exit code $LASTEXITCODE"
}

& $python .\OMERO_import.py $transfer_file
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
python parser.py BASE_PATH OUTPUT_JSON [--log-file LOG_FILE] [--metafold MODE]
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
metacharacters. Suffix entries are Python regular expressions matched against
the full file name. Blacklist matches always take precedence.

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

## Logging and failures

- The parser writes to `parser.log` next to the transfer file unless `--log-file` is
	provided.
- Set `OMERO_IMPORT_LOG_DIR` to choose the importer log directory. It defaults
	to `/var/log`.
- The importer rotates `omero_import.log` daily and retains 14 backups;
	`omero_import_errors.log` retains 30 backups.
- Failed OMERO CLI import logs are copied to
	`<OMERO_IMPORT_LOG_DIR>/omero_import/failed/<run-id>`.

## Current limitations

- The parser uses a fixed 24-hour recency window.
- Tags and key-value annotations are present in the transfer file format but are not
	populated by the parser.
- The importer currently targets Datasets; Screen and Plate workflows are not
	implemented.
- Source-path mapping between Windows shares and server mount paths is not yet
	implemented.
- The current importer configuration uses linked import transfer (`ln_s`), so
	the OMERO server must be able to access the source paths.

## Outlook
- additional flags to set the time window
- additional SQLite database containing imported paths and resulting OMERO Image IDs
- Screen/Plate as import target
