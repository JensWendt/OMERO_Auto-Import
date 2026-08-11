import argparse
from importlib.metadata import metadata
import json
import logging
import os
import re
import time
from pathlib import Path

#########
# ToDos #
#########

# - add path mapping for cross system compatibility (e.g. W:/ELYRA_7/GroupA/User01/Dataset/Müller_µ.tif -> /mnt/hive3000/Jens/ELYRA_7/GroupA/User01/Dataset/Müller_µ.tif)
# - add whitelist/blacklist merging/behaviour info to logging
# - add --metafold-only optional argument to only include files with a metafold metadata override (i.e. ignore path metadata)

LOGGER = logging.getLogger("omero_import_parser")
MAX_AGE = 24 * 60 * 60 #in seconds
CONTROL_FILE_NAMES = {".omero_import_whitelist.json", ".omero_import_blacklist.json"}
METADATA_FILE_SUFFIX = "-metadata.json"
REGEX_META_PATTERN = re.compile(r"[.^$*+?{}\[\]\\|()]")
RULE_KEY_MISSING = object()


def _strip_surrounding_quotes(value):
	"""Remove one matching pair of command-line quotes from a path value."""
	value = str(value).strip()
	if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
		return value[1:-1]
	return value


def configure_logging(log_file, level=logging.INFO):
	"""Configure console and UTF-8 file logging for one parser run."""
	log_file = Path(log_file).expanduser().absolute()
	log_file.parent.mkdir(parents=True, exist_ok=True)

	formatter = logging.Formatter(
		"%(asctime)s %(levelname)s %(name)s %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)
	LOGGER.setLevel(level)
	LOGGER.propagate = False
	for handler in LOGGER.handlers[:]:
		handler.close()
		LOGGER.removeHandler(handler)

	console_handler = logging.StreamHandler()
	console_handler.setLevel(level)
	console_handler.setFormatter(formatter)
	file_handler = logging.FileHandler(log_file, encoding="utf-8")
	file_handler.setLevel(level)
	file_handler.setFormatter(formatter)
	LOGGER.addHandler(console_handler)
	LOGGER.addHandler(file_handler)
	LOGGER.info("Parser logging initialized; log file: %s", log_file)


class ParserError(RuntimeError):
	"""Raised when the parser configuration or input cannot be processed."""


class PathMetadataError(ValueError):
	"""Raised when a file path does not contain enough import metadata."""


class RuleSet:
	"""Inherited file rules used while scanning one directory tree."""

	def __init__(self, whitelist_suffixes=None, whitelist_files=None,
				 blacklist_suffixes=None, blacklist_files=None):
		self.whitelist_suffixes = list(whitelist_suffixes or [])
		self.whitelist_files = list(whitelist_files or [])
		self.blacklist_suffixes = list(blacklist_suffixes or [])
		self.blacklist_files = list(blacklist_files or [])

	def child(self, local_rules):
		"""Return rules with the local suffix and file lists appended."""
		return RuleSet(
			self.whitelist_suffixes + local_rules["whitelist"]["suffixes"],
			self.whitelist_files + local_rules["whitelist"]["files"],
			self.blacklist_suffixes + local_rules["blacklist"]["suffixes"],
			self.blacklist_files + local_rules["blacklist"]["files"],
		)


def _read_rule_file(path):
	try:
		with path.open("r", encoding="utf-8") as rule_file:
			value = json.load(rule_file)
	except (OSError, json.JSONDecodeError) as error:
		raise ParserError(f"Could not read rule file {path}: {error}")

	if not isinstance(value, dict):
		raise ParserError(f"Rule file {path} must contain a JSON object.")
	return value


def _rule_list(rule_file, path, key, default=RULE_KEY_MISSING):
	values = rule_file.get(key, default)
	if values is None:
		return None if default is None else []
	if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
		raise ParserError(f"Rule file {path} must define '{key}' as a list of strings.")
	return values


def _load_local_rules(directory):
	local_rules = {
		"whitelist": {"folders": None, "suffixes": [], "files": []},
		"blacklist": {"folders": [], "suffixes": [], "files": []},
	}

	whitelist_path = directory / ".omero_import_whitelist.json"
	if whitelist_path.exists():
		whitelist = _read_rule_file(whitelist_path)
		local_rules["whitelist"]["folders"] = _rule_list(
			whitelist, whitelist_path, "folders", default=None
		)
		local_rules["whitelist"]["suffixes"] = _rule_list(whitelist, whitelist_path, "suffixes")
		local_rules["whitelist"]["files"] = _rule_list(whitelist, whitelist_path, "files")

	blacklist_path = directory / ".omero_import_blacklist.json"
	if blacklist_path.exists():
		blacklist = _read_rule_file(blacklist_path)
		local_rules["blacklist"]["folders"] = _rule_list(blacklist, blacklist_path, "folders")
		local_rules["blacklist"]["suffixes"] = _rule_list(blacklist, blacklist_path, "suffixes")
		local_rules["blacklist"]["files"] = _rule_list(blacklist, blacklist_path, "files")

	return local_rules


def _validate_regex_patterns(patterns, source):
	for pattern in patterns:
		try:
			re.compile(pattern)
		except re.error as error:
			raise ParserError(f"Invalid regex '{pattern}' in {source}: {error}")


def _matches_name(value, pattern):
	"""Match plain names exactly and explicit regex patterns by substring."""
	if REGEX_META_PATTERN.search(pattern) is None:
		return value == pattern
	return re.search(pattern, value) is not None


def _matches_suffix(value, pattern):
	"""Match suffix rules as Python regular expressions."""
	return re.search(pattern, value) is not None


def _validate_rules(local_rules, directory):
	for category in ("whitelist", "blacklist"):
		for key in ("folders", "suffixes", "files"):
			patterns = local_rules[category][key]
			if patterns is not None:
				_validate_regex_patterns(
					patterns,
					f"{directory}/.omero_import_{category}.json",
				)


def _is_blacklisted(file_name, rules):
	return (
		any(_matches_suffix(file_name, pattern) for pattern in rules.blacklist_suffixes)
		or any(_matches_name(file_name, pattern) for pattern in rules.blacklist_files)
	)


def _is_whitelisted(file_name, rules):
	if not rules.whitelist_suffixes and not rules.whitelist_files:
		return True
	return (
		any(_matches_suffix(file_name, pattern) for pattern in rules.whitelist_suffixes)
		or any(_matches_name(file_name, pattern) for pattern in rules.whitelist_files)
	)


def _is_control_file(file_name):
	return file_name in CONTROL_FILE_NAMES or file_name.endswith(METADATA_FILE_SUFFIX)


def _directory_allowed(directory_name, local_rules):
	whitelist_folders = local_rules["whitelist"]["folders"]
	blacklist_folders = local_rules["blacklist"]["folders"]
	if any(_matches_name(directory_name, pattern) for pattern in blacklist_folders):
		return False
	if not whitelist_folders:
		return True
	return any(_matches_name(directory_name, pattern) for pattern in whitelist_folders)


def _creation_time(path):
	stat_result = path.stat()
	if os.name == "nt":
		return stat_result.st_ctime
	return getattr(stat_result, "st_birthtime", stat_result.st_ctime)


def _is_recent(path, now=None, max_age=MAX_AGE):
	now = time.time() if now is None else now
	age = now - _creation_time(path)
	return 0 <= age <= max_age


def _normalise_output_path(path):
	"""Return an absolute POSIX path; mount mapping is a future extension point."""
	return str(path.absolute().as_posix())


def _physical_path_key(path):
	try:
		resolved = path.resolve(strict=False)
	except OSError:
		resolved = path.absolute()
	return os.path.normcase(str(resolved))


def _user_name(directory_name):
	return directory_name
	# if "_" not in directory_name:
	# 	return directory_name
	# return directory_name.rsplit("_", 1)[1]


def _path_metadata(base_path, file_path):
	relative_parts = file_path.relative_to(base_path).parts
	if len(relative_parts) < 4:
		raise PathMetadataError(
			f"File '{file_path}' must be below group/user/dataset directories."
		)

	group_name = relative_parts[0]
	user_directory = relative_parts[1]
	user_name = _user_name(user_directory)
	dataset_parts = relative_parts[2:-1]

	if len(dataset_parts) == 1:
		project_identifier = None
		dataset_identifier = dataset_parts[0]
	elif len(dataset_parts) == 2:
		project_identifier = dataset_parts[0]
		dataset_identifier = dataset_parts[1]
	else:
		dataset_candidates = [
			part[:-len("_dataset")]
			for part in dataset_parts
			if part.endswith("_dataset")
		]
		project_candidates = [
			part[:-len("_project")]
			for part in dataset_parts
			if part.endswith("_project")
		]
		if len(dataset_candidates) != 1:
			raise PathMetadataError(
				f"File '{file_path}' is more than two levels below user '{user_directory}' "
				"but does not have exactly one *_dataset directory."
			)
		if len(project_candidates) > 1:
			raise PathMetadataError(
				f"File '{file_path}' contains more than one *_project directory."
			)
		dataset_identifier = dataset_candidates[0]
		project_identifier = project_candidates[0] if project_candidates else None

	if not dataset_identifier:
		raise PathMetadataError(f"File '{file_path}' produced an empty dataset identifier.")

	return {
		"group": group_name,
		"user": user_name,
		"dataset_identifier": str(dataset_identifier),
		"project_identifier": (
			str(project_identifier) if project_identifier is not None else None
		),
	}


def _metadata_override(directory, cache):
	if directory in cache:
		return cache[directory]

	metadata_files = sorted(
		path for path in directory.iterdir()
		if path.is_file() and path.name.endswith(METADATA_FILE_SUFFIX)
	)
	if not metadata_files:
		result = {"present": False, "metadata": None}
		cache[directory] = result
		return result
	if len(metadata_files) > 1:
		LOGGER.warning(
			"Multiple metadata files found in %s; falling back to path metadata.",
			directory,
		)
		result = {"present": True, "metadata": None}
		cache[directory] = result
		return result

	metadata_path = metadata_files[0]
	try:
		with metadata_path.open("r", encoding="utf-8") as metadata_file:
			metadata = json.load(metadata_file)
		omero_metadata = metadata["metafold_integration"]["external_links"]["omero"]
		dataset_identifier = omero_metadata.get("dataset_id")
		user_name = omero_metadata.get("user_name")
		group_name = omero_metadata.get("group_name")
		if any(
			value is None or str(value).strip() == ""
			for value in (dataset_identifier, user_name, group_name)
		):
			raise KeyError("dataset_id, user_name, or group_name")
		override = {
			"dataset_identifier": str(dataset_identifier),
			"user": str(user_name),
			"group": str(group_name),
			"project_identifier": None,
		}
		if omero_metadata.get("project_id") is not None:
			override["project_identifier"] = str(omero_metadata["project_id"])
		result = {"present": True, "metadata": override}
		cache[directory] = result
		return result
	except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
		LOGGER.warning(
			"Could not use metadata override %s (%s); falling back to path metadata.",
			metadata_path,
			error,
		)
		result = {"present": True, "metadata": None}
		cache[directory] = result
		return result


class ImportManifestBuilder:
	"""Build the nested import.json structure and resolve duplicate files."""

	def __init__(self):
		self.data = {"group": {}}
		self._dataset_entries = {}
		self._physical_files = {}

	def add_file(self, metadata, file_path):
		physical_key = _physical_path_key(file_path)
		context = (
			metadata["group"],
			metadata["user"],
			metadata["dataset_identifier"],
			metadata.get("project_identifier"),
		)
		previous_context = self._physical_files.get(physical_key)
		if previous_context is not None:
			if previous_context != context:
				LOGGER.warning(
					"File %s already belongs to %s; skipping duplicate target %s.",
					file_path,
					previous_context,
					context,
				)
			return
		self._physical_files[physical_key] = context

		group_data = self.data["group"].setdefault(metadata["group"], {"user": {}})
		user_data = group_data["user"].setdefault(metadata["user"], {"datasets": []})
		dataset_key = (metadata["dataset_identifier"], metadata.get("project_identifier"))
		entry_key = (metadata["group"], metadata["user"], dataset_key)
		dataset_entry = self._dataset_entries.get(entry_key)
		if dataset_entry is None:
			dataset_entry = {
				"dataset_identifier": metadata["dataset_identifier"],
				"files": {},
			}
			if metadata.get("project_identifier") is not None:
				dataset_entry["project_identifier"] = metadata["project_identifier"]
			user_data["datasets"].append(dataset_entry)
			self._dataset_entries[entry_key] = dataset_entry

		output_path = _normalise_output_path(file_path)
		dataset_entry["files"][output_path] = {
			"in-place": False,
			"Tag": [],
			"kv-pair": {},
		}


class ImportParser:
	"""Scan a watch-folder tree and generate an OMERO import manifest."""

	def __init__(self, base_path, recent_seconds=MAX_AGE, metafold_mode="fallback"):
		self.base_path = Path(base_path).expanduser().absolute()
		self.recent_seconds = recent_seconds
		self.metafold_mode = metafold_mode
		self.builder = ImportManifestBuilder()
		self.metadata_cache = {}
		self.stats = {
			"directories_scanned": 0,
			"files_seen": 0,
			"files_accepted": 0,
			"files_skipped": 0,
			"files_skipped_blacklist": 0,
			"files_skipped_whitelist": 0,
			"files_skipped_old": 0,
			"files_skipped_invalid_path": 0,
			"files_skipped_no_metafold": 0,
		}

	def parse(self):
		if not self.base_path.exists():
			raise ParserError(f"Base path does not exist: {self.base_path}")
		if not self.base_path.is_dir():
			raise ParserError(f"Base path is not a directory: {self.base_path}")
		LOGGER.info(
			"Starting scan: base_path=%s max_age_seconds=%s",
			self.base_path,
			self.recent_seconds,
		)
		self._scan_directory(self.base_path, RuleSet())
		LOGGER.info(
			"Scan complete: directories=%d files_seen=%d accepted=%d skipped=%d "
			"(blacklist=%d whitelist=%d old=%d invalid_path=%d no_metafold=%d)",
			self.stats["directories_scanned"],
			self.stats["files_seen"],
			self.stats["files_accepted"],
			self.stats["files_skipped"],
			self.stats["files_skipped_blacklist"],
			self.stats["files_skipped_whitelist"],
			self.stats["files_skipped_old"],
			self.stats["files_skipped_invalid_path"],
			self.stats["files_skipped_no_metafold"],
		)
		return self.builder.data

	def _scan_directory(self, directory, inherited_rules):
		self.stats["directories_scanned"] += 1
		LOGGER.debug("Scanning directory: %s", directory)
		local_rules = _load_local_rules(directory)
		_validate_rules(local_rules, directory)
		effective_rules = inherited_rules.child(local_rules)

		try:
			entries = sorted(directory.iterdir(), key=lambda path: path.name)
		except OSError as error:
			LOGGER.warning("Could not scan directory %s: %s", directory, error)
			return

		for entry in entries:
			if entry.is_dir():
				if _directory_allowed(entry.name, local_rules):
					self._scan_directory(entry, effective_rules)
				continue
			if not entry.is_file() or _is_control_file(entry.name):
				continue
			self.stats["files_seen"] += 1
			if _is_blacklisted(entry.name, effective_rules):
				self.stats["files_skipped"] += 1
				self.stats["files_skipped_blacklist"] += 1
				LOGGER.info("Skipping blacklisted file: %s", entry)
				continue
			if not _is_whitelisted(entry.name, effective_rules):
				self.stats["files_skipped"] += 1
				self.stats["files_skipped_whitelist"] += 1
				LOGGER.info("Skipping file not matched by whitelist: %s", entry)
				continue
			sidecar = _metadata_override(entry.parent, self.metadata_cache)
			if self.metafold_mode == "ignore" and sidecar["present"]:
				self.stats["files_skipped"] += 1
				LOGGER.info("Skipping file with Metafold sidecar: %s", entry)
				continue
			if self.metafold_mode == "only" and sidecar["metadata"] is None:
				self.stats["files_skipped"] += 1
				self.stats["files_skipped_no_metafold"] += 1
				LOGGER.info("Skipping file without valid Metafold sidecar: %s", entry)
				continue
			try:
				if not _is_recent(entry, max_age=self.recent_seconds):
					self.stats["files_skipped"] += 1
					self.stats["files_skipped_old"] += 1
					LOGGER.info("Skipping file older than configured age: %s", entry)
					continue
				if sidecar["metadata"] is not None:
					metadata = {
						"group": sidecar["metadata"]["group"],
						"user": sidecar["metadata"]["user"],
						"dataset_identifier": sidecar["metadata"]["dataset_identifier"],
						"project_identifier": sidecar["metadata"].get("project_identifier"),
					}
				else:
					metadata = _path_metadata(self.base_path, entry)
			except (OSError, PathMetadataError) as error:
				self.stats["files_skipped"] += 1
				self.stats["files_skipped_invalid_path"] += 1
				LOGGER.warning("Skipping file %s: %s", entry, error)
				continue

			if sidecar["metadata"] is not None:
				LOGGER.info(
					"Applying Metafold metadata from %s to %s: group=%s user=%s dataset=%s project=%s",
					entry.parent,
					entry.name,
					metadata["group"],
					metadata["user"],
					metadata["dataset_identifier"],
					metadata.get("project_identifier"),
				)
			self.builder.add_file(metadata, entry)
			self.stats["files_accepted"] += 1
			LOGGER.info(
				"Accepted file: %s -> group=%s user=%s project=%s dataset=%s",
				entry,
				metadata["group"],
				metadata["user"],
				metadata.get("project_identifier"),
				metadata["dataset_identifier"],
			)


def generate_import_json(base_path, output_path, metafold_mode="fallback"):
	"""Scan ``base_path`` and write the generated manifest to ``output_path``."""
	parser = ImportParser(base_path, metafold_mode=metafold_mode)
	manifest = parser.parse()
	output_path = Path(output_path).expanduser()
	output_path.parent.mkdir(parents=True, exist_ok=True)
	with output_path.open("w", encoding="utf-8") as output_file:
		json.dump(manifest, output_file, indent=2, ensure_ascii=False)
		output_file.write("\n")
	LOGGER.info("Wrote import manifest to %s", output_path.absolute())
	return manifest


def parse_command_line_args():
	parser = argparse.ArgumentParser(
		description="Generate an OMERO import manifest from a watch-folder tree."
	)
	parser.add_argument("base_path", help="Base directory to scan.")
	parser.add_argument("output_json", help="Path for the generated import.json.")
	parser.add_argument(
		"--log-file",
		help="Path for the parser log; defaults to parser.log beside output_json.",
	)
	parser.add_argument(
		"--metafold",
		choices=("fallback", "only", "ignore"),
		default="fallback",
		help="Metafold sidecar mode: only requires valid sidecars; ignore skips sidecar files.",
	)
	return parser.parse_args()


def main(base_path, output_json, log_file=None, metafold_mode="fallback"):
	"""Generate an import manifest and return its parsed dictionary."""
	base_path = _strip_surrounding_quotes(base_path)
	output_json = Path(_strip_surrounding_quotes(output_json)).expanduser()
	if log_file is None:
		log_file = output_json.parent / "parser.log"
	else:
		log_file = _strip_surrounding_quotes(log_file)
	configure_logging(log_file)
	try:
		return generate_import_json(base_path, output_json, metafold_mode)
	finally:
		logging.shutdown()


if __name__ == "__main__":
	args = parse_command_line_args()
	main(args.base_path, args.output_json, args.log_file, args.metafold)
