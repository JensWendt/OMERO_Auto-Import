import os
import atexit
import shutil
import subprocess
import tempfile
import json
import re
import argparse
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
import logging
import logging.config
from datetime import datetime
from importlib import import_module

import omero
from omero.gateway import BlitzGateway, ExperimenterWrapper, MapAnnotationWrapper, TagAnnotationWrapper
from omero.model import ProjectI, DatasetI, ImageI, ProjectDatasetLinkI,\
TagAnnotationI, MapAnnotationI
from omero.rtypes import rstring
from omero.cli import CLI
from omero.plugins.sessions import SessionsControl

# Import the ImportControl from the OMERO plugins (if needed)
ImportControl = import_module("omero.plugins.import").ImportControl

# ToDo
#- refine the logging messages, via the logging_config
#- set up logging properly
#- change behaviour of checking for user and group matching, so that the mismatched user-node just gets skipped
#- Add Screen/Plate functionality
#- Design runId properly
#- Add runID related logging of imports in SQLlite
#- Make it clearly findable if import failed due to file not being finished writing


###############
### LOGGING ###
###############

LOG_DIR = os.getenv("OMERO_IMPORT_LOG_DIR", "/var/log")
def setup_logging(level="INFO"):
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    class ContextDefaults(logging.Filter):
        def filter(self, record):
            if not hasattr(record, "run_id"):
                record.run_id = "-"
            if not hasattr(record, "dataset"):
                record.dataset = "-"
            return True

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "context_defaults": {
                "()": ContextDefaults,
            },
        },
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)s %(name)s [run_id=%(run_id)s dataset=%(dataset)s] %(message)s"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level,
                "formatter": "standard",
                "filters": ["context_defaults"],
            },
            "file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "level": level,
                "formatter": "standard",
                "filename": str(Path(LOG_DIR) / "omero_import.log"),
                "when": "midnight",
                "backupCount": 14,
                "encoding": "utf-8",
                "filters": ["context_defaults"],
            },
            "error_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "level": "ERROR",
                "formatter": "standard",
                "filename": str(Path(LOG_DIR) / "omero_import_errors.log"),
                "when": "midnight",
                "backupCount": 30,
                "encoding": "utf-8",
                "filters": ["context_defaults"],
            },
        },
        "root": {
            "level": level,
            "handlers": ["console", "file", "error_file"],
        },
    }
    logging.config.dictConfig(logging_config)
    logging.captureWarnings(True)

def build_logger_context(parsed_metadata):
    return {
        "run_id": create_run_id(),
        "dataset": parsed_metadata.get("dataset_name", "-") if parsed_metadata else "-",
    }

def create_run_id():
    return datetime.now().strftime("%Y_%m_%d-%H-%M-%S-%f")

class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        merged = dict(self.extra)
        merged.update(extra)
        kwargs["extra"] = merged
        return msg, kwargs
    
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
base_logger = logging.getLogger("omero_import")
logger = ContextAdapter(base_logger, build_logger_context(None))
### set this up properly, so that the logger can be used in all functions and classes

#################
### Constants ###
#################

TRANSFER_FILE_NAME = "import.json"
CREDENTIALS_FILE = os.getenv("OMERO_CREDENTIALS", "/opt/omero/credentials_auto_in-place_import.json")
HOST = '10.14.28.44'
PORT = 4064
PARALLEL_UPLOAD = 4 #adjust based on your system and network capabilities
TTL_FOR_IMPORT_CONN = 6000000 # in milliseconds

sample_import_config = {
    "parallel_upload_per_worker": 4,
    "parallel_filesets_per_worker": 2,
    "skip_checksum": True,
    "skip_minmax": True,
    "skip_thumbnails": True,
    "skip_upgrade": True,
    "depth": 1,  # Adjust this value based on your directory structure
}
import_config = {
    "parallel_upload_per_worker": 4,
    "parallel_filesets_per_worker": 2,
    "skip_minmax": True,
}

########################
### Helper functions ###
########################

def parse_tag_use(value):
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError(
            "tag-use must be 'self', 'all', or an OMERO omeName."
        )
    return value

def parse_command_line_args():
    parser = argparse.ArgumentParser(
        description="Import image files described by an OMERO import manifest."
    )
    parser.add_argument(
        "json_path",
        help="Directory containing timestamped import transfer manifests.",
    )
    parser.add_argument(
        "--tag-use",
        default="self",
        type=parse_tag_use,
        help=(
            "Tag owner selection: 'self' uses tags owned by the target user, "
            "'all' uses the first matching tag, or provide an OMERO omeName."
        ),
    )
    return parser.parse_args()

def transfer_name_parts():
    transfer_path = Path(TRANSFER_FILE_NAME)
    return transfer_path.stem, transfer_path.suffix

def transfer_manifest_pattern():
    prefix, suffix = transfer_name_parts()
    return re.compile(
        rf"^{re.escape(prefix)}_(?P<timestamp>\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}){re.escape(suffix)}$"
    )

def select_latest_transfer_manifest(json_path):
    directory = Path(json_path).expanduser()
    if not directory.exists():
        raise FileNotFoundError(f"Transfer manifest directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Transfer manifest path is not a directory: {directory}")

    pattern = transfer_manifest_pattern()
    candidates = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.name)
        if match is None:
            continue
        try:
            datetime.strptime(match.group("timestamp"), "%Y-%m-%dT%H-%M")
        except ValueError:
            logger.warning("Ignoring transfer manifest with invalid timestamp: %s", path)
            continue
        candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"No ready transfer manifests found in: {directory}")
    # return the newest manifest based on the timestamp in the filename
    return max(candidates, key=lambda path: path.name)

def claim_transfer_manifest(manifest_path):
    _, suffix = transfer_name_parts()
    claimed_path = manifest_path.with_name(
        f"{manifest_path.stem}_in-process{suffix}"
    )
    if claimed_path.exists():
        raise RuntimeError(f"Transfer manifest is already in process: {claimed_path}")
    try:
        manifest_path.rename(claimed_path)
    except OSError as error:
        raise RuntimeError(
            f"Could not claim transfer manifest {manifest_path}: {error}"
        )
    logger.info("Claimed transfer manifest: %s", claimed_path)
    return claimed_path

def finalise_transfer_manifest(claimed_path, succeeded):
    _, suffix = transfer_name_parts()
    original_name = claimed_path.name.replace("_in-process", "", 1)
    if succeeded:
        claimed_path.unlink()
        logger.info("Deleted successfully processed transfer manifest: %s", claimed_path)
        return

    failed_directory = claimed_path.parent / "failed_imports"
    failed_directory.mkdir(parents=True, exist_ok=True)
    failed_path = failed_directory / original_name
    if failed_path.exists():
        raise RuntimeError(
            f"Failed transfer manifest already exists and will not be overwritten: {failed_path}"
        )
    try:
        claimed_path.rename(failed_path)
    except OSError as error:
        raise RuntimeError(
            f"Could not move failed transfer manifest {claimed_path}: {error}"
        )
    logger.warning("Moved failed transfer manifest to: %s", failed_path)

def import_records_succeeded(import_records):
    return bool(import_records) and all(
        record.get("success")
        and record.get("annotation_status", {"success": True}).get("success")
        for record in import_records
    )

def environment_boolean(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")

def email_configuration():
    if not environment_boolean("OMERO_EMAIL_ENABLED"):
        return None

    configuration = {
        "host": os.getenv("OMERO_EMAIL_SMTP_HOST", "").strip(),
        "port": int(os.getenv("OMERO_EMAIL_SMTP_PORT", "587")),
        "security": os.getenv("OMERO_EMAIL_SMTP_SECURITY", "starttls").strip().lower(),
        "username": os.getenv("OMERO_EMAIL_SMTP_USER", ""),
        "password": os.getenv("OMERO_EMAIL_SMTP_PASSWORD", ""),
        "sender": os.getenv("OMERO_EMAIL_FROM", "").strip(),
    }
    if not configuration["host"] or not configuration["sender"]:
        raise ValueError(
            "OMERO_EMAIL_SMTP_HOST and OMERO_EMAIL_FROM are required when "
            "OMERO_EMAIL_ENABLED is true."
        )
    if configuration["security"] not in {"starttls", "ssl", "none"}:
        raise ValueError(
            "OMERO_EMAIL_SMTP_SECURITY must be starttls, ssl, or none."
        )
    if bool(configuration["username"]) != bool(configuration["password"]):
        raise ValueError(
            "OMERO_EMAIL_SMTP_USER and OMERO_EMAIL_SMTP_PASSWORD must be set together."
        )
    return configuration

def import_record_succeeded(import_record):
    return (
        import_record.get("success")
        and import_record.get("annotation_status", {"success": True}).get("success")
    )

def import_record_failure_reason(import_record):
    if import_record.get("error"):
        return import_record["error"]
    annotation_errors = import_record.get("annotation_status", {}).get("errors", [])
    if annotation_errors:
        return annotation_errors[0].get("error", "Annotation failed.")
    if import_record.get("return_code") is not None:
        return f"OMERO CLI returned exit code {import_record['return_code']}."
    return "Import did not complete successfully."

def summarise_import_records(import_records):
    summaries = {}
    for import_record in import_records:
        key = (import_record["target_group"], import_record["target_username"])
        summary = summaries.setdefault(key, {
            "group": key[0],
            "username": key[1],
            "total_files": 0,
            "successful_files": [],
            "failed_files": [],
            "image_count": 0,
        })
        summary["total_files"] += 1
        if import_record_succeeded(import_record):
            summary["successful_files"].append(import_record["image_path"])
            summary["image_count"] += sum(
                len(image_ids) for image_ids in import_record.get("image_id_dict", {}).values()
            )
        else:
            summary["failed_files"].append((
                import_record["image_path"],
                import_record_failure_reason(import_record),
            ))
    return list(summaries.values())

def limited_lines(items, limit, formatter):
    lines = [formatter(item) for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"... {len(items) - limit} additional entries omitted.")
    return lines

def completion_email_message(recipient, sender, summary):
    failed_count = len(summary["failed_files"])
    successful_count = len(summary["successful_files"])
    if failed_count == 0:
        outcome = "completed successfully"
    elif successful_count == 0:
        outcome = "failed"
    else:
        outcome = "completed with failures"

    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = (
        f"OMERO import {outcome}: {successful_count}/{summary['total_files']} files, "
        f"{summary['image_count']} images"
    )
    lines = [
        f"Hello {summary['username']},",
        "",
        f"Your OMERO import {outcome}.",
        f"Group: {summary['group']}",
        f"Files processed: {summary['total_files']}",
        f"Files imported successfully: {successful_count}",
        f"OMERO images created: {summary['image_count']}",
        f"Failures: {failed_count}",
    ]
    if summary["successful_files"]:
        lines.extend(["", "Imported source files:"])
        lines.extend(limited_lines(summary["successful_files"], 20, lambda path: f"- {path}"))
    if summary["failed_files"]:
        lines.extend(["", "Failed source files:"])
        lines.extend(limited_lines(
            summary["failed_files"],
            10,
            lambda item: f"- {item[0]}: {item[1]}",
        ))
    message.set_content("\n".join(lines) + "\n")
    return message

def get_experimenter_email(conn, experimenter, username):
    try:
        user_wrapper = ExperimenterWrapper(conn, experimenter)
        email = user_wrapper._obj.getEmail().getValue()
    except Exception as error:
        logger.warning("Could not retrieve email for OMERO user '%s': %s", username, error)
        return None
    if not email or not email.strip():
        logger.warning("OMERO user '%s' has no email address; notification skipped.", username)
        return None
    return email.strip()

def send_completion_email(configuration, message):
    smtp_class = smtplib.SMTP_SSL if configuration["security"] == "ssl" else smtplib.SMTP
    with smtp_class(configuration["host"], configuration["port"], timeout=30) as smtp:
        if configuration["security"] == "starttls":
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if configuration["username"]:
            smtp.login(configuration["username"], configuration["password"])
        smtp.send_message(message)

def send_completion_notifications(conn, resolved_experimenters, import_records):
    try:
        configuration = email_configuration()
    except Exception as error:
        logger.error("Email notifications are not configured correctly: %s", error)
        return
    if configuration is None:
        logger.info("Email notifications are disabled.")
        return

    for summary in summarise_import_records(import_records):
        experimenter = resolved_experimenters.get(summary["username"])
        if experimenter is None:
            logger.warning(
                "No resolved OMERO user object for '%s'; notification skipped.",
                summary["username"],
            )
            continue
        recipient = get_experimenter_email(conn, experimenter, summary["username"])
        if recipient is None:
            continue
        message = completion_email_message(recipient, configuration["sender"], summary)
        try:
            send_completion_email(configuration, message)
            logger.info("Sent import completion notification to '%s'.", recipient)
        except Exception as error:
            logger.error(
                "Could not send import completion notification to '%s': %s",
                recipient,
                error,
            )

def run_cli_command(command, env, error_message):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{error_message}\n"
            f"Command: {result.args}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
    return result

def get_id_value(value):
    return value.getValue() if hasattr(value, "getValue") else value

def normalise_import_path(path):
    """Use one slash style for import-result keys across operating systems."""
    return str(path).replace("\\", "/")

def parse_import_image_ids(log_path, import_path):
    """Extract imported image IDs from an OMERO CLI import log.

    The OMERO CLI writes imported IDs on a line beginning with ``Image:``.
    For example, an ``import.log`` line such as::

        Image:20501,20502,20503,20504,20505,20506,20507,20508,20509

    is returned as::

        {"/path/to/image": [20501, 20502, 20503, 20504, 20505,
                             20506, 20507, 20508, 20509]}

    Square brackets and whitespace between IDs are also accepted. If the log
    file is missing or contains no matching ``Image:`` line, the function
    logs a warning and returns the import path mapped to an empty list.

    Args:
        log_path: Path to the OMERO CLI ``import.log`` file.
        import_path: Path of the imported image or file, used as the result key.

    Returns:
        A dictionary mapping the string form of ``import_path`` to a list of
        integer OMERO image IDs.
    """
    image_ids = []
    image_line_pattern = re.compile(r"^\s*Image:\s*\[?\s*([0-9,\s]+)\]?\s*$")

    if not log_path.exists():
        logger.warning(f"OMERO CLI image log was not created: {log_path}")
        return {normalise_import_path(import_path): image_ids}

    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = image_line_pattern.match(line)
        if match:
            image_ids.extend(
                int(value)
                for value in match.group(1).split(",")
                if value.strip()
            )

    if not image_ids:
        logger.warning(f"OMERO CLI image log contained no image IDs for {import_path}")

    return {normalise_import_path(import_path): image_ids}

def forward_omero_cli_log(log_path, logger_method):
    if not log_path.exists():
        logger.warning(f"OMERO CLI log was not created: {log_path}")
        return

    severity_pattern = re.compile(
        r"^\s*\d{4}-\d{2}-\d{2}\s+[^\s]+\s+\d+\s+\[[^]]+\]\s+"
        r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+(?P<message>.*)$"
    )
    level_methods = {
        "TRACE": logger.debug,
        "DEBUG": logger.debug,
        "INFO": logger.info,
        "WARN": logger.warning,
        "ERROR": logger.error,
        "FATAL": logger.critical,
    }

    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = severity_pattern.match(line)
        if match:
            level_method = level_methods[match.group("level")]
            level_method(f"OMERO_CLI_{match.group('message')}")
        else:
            logger_method(f"OMERO_CLI_{line}")

def preserve_failed_omero_logs(log_dir, run_id):
    preserved_dir = Path(LOG_DIR) / "failed_imports" / run_id
    preserved_dir.mkdir(parents=True, exist_ok=True)
    for log_path in log_dir.iterdir():
        if log_path.is_file():
            shutil.copy2(log_path, preserved_dir / log_path.name)
    return preserved_dir

def get_unique_object_by_name(conn, object_type, name):
    objects = list(conn.getObjects(object_type, attributes={'name': name}))

    if len(objects) > 1:
        message = f"Multiple {object_type}s found with name '{name}'; refusing to choose an object. "
        "Use an OMERO ID or a unique name."
        logger.error(message)
        raise RuntimeError(message)

    if not objects:
        logger.info(f"No {object_type}s found with name '{name}'.")
        return None

    obj = objects[0]
    logger.info(
        f"Found {object_type} '{name}' with ID {obj.getId()}."
    )
    return obj

def get_unique_dataset_by_name(conn, name, project_id=None):
    if project_id is None:
        return get_unique_object_by_name(conn, "Dataset", name)

    datasets = list(conn.getObjects("Dataset", attributes={"name": name}, opts={"project": project_id}))

    if len(datasets) > 1:
        message = f"Multiple datasets named '{name}' found in Project ID '{project_id}'; refusing to choose a dataset."
        logger.error(message)
        raise RuntimeError(message)

    if not datasets:
        logger.info(
            f"No dataset named '{name}' found in Project ID '{project_id}'."
        )
        return None

    dataset = datasets[0]
    logger.info(
        f"Found dataset '{name}' with ID {dataset.getId()} in "
        f"Project ID '{project_id}'."
    )
    return dataset

def resolve_dataset_without_project_by_name(conn, name):
    return get_unique_dataset_by_name(conn, name)

def ensure_dataset_linked_to_project(conn, project, dataset):
    project_id = get_id_value(project.getId())
    dataset_id = get_id_value(dataset.getId())
    project_wrapper = conn.getObject("Project", project_id)
    existing_dataset_ids = {child.getId() for child in project_wrapper.listChildren()}
    if dataset_id in existing_dataset_ids:
        logger.info(f"Dataset '{getattr(dataset.getName(), '_val', dataset.getName())}' is already linked to Project '{getattr(project.getName(), '_val', project.getName())}'.")
        return
    else:
        link = ProjectDatasetLinkI()
        link.setParent(getattr(project_wrapper, "_obj", project_wrapper))
        link.setChild(getattr(dataset, "_obj", dataset))
        conn.getUpdateService().saveObject(link)
        logger.info(f"Linked Dataset '{getattr(dataset.getName(), '_val', dataset.getName())}' to Project '{getattr(project.getName(), '_val', project.getName())}'.")

def validate_object_context(obj, object_type, pair):
    details = obj.getDetails()
    owner = details.getOwner().omeName if details and details.getOwner() else None
    group = details.getGroup().name if details and details.getGroup() else None
    if owner != pair["user"] or group != pair["group"]:
        message = (
            f"{object_type} '{obj.getName()}' (ID {obj.getId()}) belongs to "
            f"user/group '{owner}/{group}', expected "
            f"'{pair['user']}/{pair['group']}'."
        )
        logger.error(message)
        raise RuntimeError(message)

def get_target_context_connection(admin_conn, pair, ttl=TTL_FOR_IMPORT_CONN):
    target_conn = admin_conn.suConn(pair["user"], pair["group"], ttl=ttl)
    if target_conn is None:
        message = (
            f"Could not open OMERO context for user '{pair['user']}' "
            f"in group '{pair['group']}'."
        )
        logger.error(message)
        raise RuntimeError(message)
    return target_conn

def resolve_project_in_context(conn, identifier):
    if identifier is None:
        return None
    if str(identifier).strip().isdecimal():
        return conn.getObject("Project", int(str(identifier).strip()))
    else:
        return get_unique_object_by_name(conn, "Project", identifier)

def resolve_dataset_in_context(conn, identifier, project_id=None):
    if str(identifier).strip().isdecimal():
        return conn.getObject("Dataset", int(str(identifier).strip()))
    else:
        return get_unique_dataset_by_name(conn, identifier, project_id)

def extract_dataset_project_pairs(json_data):
    dataset_project_pairs = []

    for group_name, group_data in json_data.get("group", {}).items():
        for user_name, user_data in group_data.get("user", {}).items():
            for dataset_data in user_data.get("datasets", []):
                dataset_project_pairs.append({
                    "group": group_name,
                    "user": user_name,
                    "dataset": dataset_data["dataset_identifier"],
                    "project": dataset_data.get("project_identifier"),
                    "dataset_data": dataset_data,
                })

    return dataset_project_pairs

def merge_user_data(user_data, existing_user_data):
    """Merge datasets when multiple manifest names resolve to one OMERO user."""
    if existing_user_data is None:
        return user_data
    existing_user_data.setdefault("datasets", []).extend(
        user_data.get("datasets", [])
    )
    return existing_user_data

def normalise_annotation_metadata(tags, key_value_pairs):
    """Validate manifest annotation metadata and convert values to strings."""
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("'Tag' must be a list of strings.")
    if not isinstance(key_value_pairs, dict):
        raise ValueError("'kv-pair' must be a JSON object.")

    normalised_tags = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    normalised_kv_pairs = [
        (str(key), str(value))
        for key, value in key_value_pairs.items()
        if str(key).strip()
    ]
    return normalised_tags, normalised_kv_pairs


def get_or_create_tag_annotation(conn, tag_name, tag_cache, tag_use):
    """Return a tag selected by owner policy, creating one when needed."""
    cache_key = (id(conn), tag_name, tag_use)
    if cache_key in tag_cache:
        return tag_cache[cache_key]

    matching_tags = list(
        conn.getObjects("TagAnnotation", attributes={"textValue": tag_name})
    )
    if tag_use == "all":
        eligible_tags = matching_tags
    else:
        owner_name = tag_use
        if tag_use == "self":
            owner_name = conn.getEventContext().userName
        eligible_tags = [
            tag for tag in matching_tags if tag.getDetails().getOwner().getName() == owner_name
        ]

    if len(eligible_tags) > 1 and tag_use != "all":
        raise RuntimeError(
            f"Multiple TagAnnotations found with text '{tag_name}' owned by "
            f"'{owner_name}'; refusing to choose one."
        )
    if eligible_tags:
        tag = eligible_tags[0]
    else:
        tag = TagAnnotationWrapper(conn)
        tag.setValue(tag_name)
        tag.save()
        logger.info(
            f"Created TagAnnotation '{tag_name}' with ID {tag.getId()} "
            f"for tag-use '{tag_use}'."
        )

    tag_cache[cache_key] = tag
    return tag

def apply_image_metadata(conn, image_ids, tags, key_value_pairs, tag_cache, tag_use):
    """Apply manifest tags and key-value pairs to each successfully imported image."""
    result = {"success": True, "annotated_image_ids": [], "errors": []}
    try:
        tags, key_value_pairs = normalise_annotation_metadata(tags, key_value_pairs)
        resolved_tags = [
            get_or_create_tag_annotation(conn, tag_name, tag_cache, tag_use)
            for tag_name in tags
        ]
    except Exception as error:
        logger.exception(f"Could not prepare manifest annotations: {error}")
        result["success"] = False
        result["errors"].append({"error": str(error)})
        return result
    if not tags and not key_value_pairs:
        return result

    for image_id in image_ids:
        try:
            image = conn.getObject("Image", image_id)
            if image is None:
                raise RuntimeError(f"Image ID {image_id} was not found after import.")
            for tag in resolved_tags:
                image.linkAnnotation(tag)
            if key_value_pairs:
                map_annotation = MapAnnotationWrapper(conn)
                map_annotation.setValue(key_value_pairs)
                map_annotation.save()
                image.linkAnnotation(map_annotation)
            result["annotated_image_ids"].append(image_id)
            logger.info(f"Applied manifest annotations to Image ID {image_id}.")
        except Exception as error:
            logger.exception(f"Could not apply manifest annotations to Image ID {image_id}: {error}")
            result["errors"].append({"image_id": image_id, "error": str(error)})

    result["success"] = not result["errors"]
    result["annotated_image_count"] = len(result["annotated_image_ids"])
    return result

def wait_for_file_stability(file_path):
    """Wait until a file's size and modification time remain unchanged."""
    interval = float(os.getenv("OMERO_FILE_STABILITY_INTERVAL", "2"))
    max_wait = float(os.getenv("OMERO_FILE_STABILITY_MAX_WAIT", "20"))
    if interval <= 0 or max_wait < interval:
        raise ValueError(
            "OMERO_FILE_STABILITY_INTERVAL must be positive and no greater than "
            "OMERO_FILE_STABILITY_MAX_WAIT."
        )

    file_path = Path(file_path)
    start_time = time.monotonic()
    while True:
        if not file_path.is_file():
            raise FileNotFoundError(f"Import source is not a regular file: {file_path}")
        initial_stat = file_path.stat()
        time.sleep(interval)
        final_stat = file_path.stat()
        if (
            initial_stat.st_size == final_stat.st_size
            and initial_stat.st_mtime_ns == final_stat.st_mtime_ns
        ):
            logger.info("Import source is stable: %s", file_path)
            return

        elapsed = time.monotonic() - start_time
        if elapsed + interval > max_wait:
            raise RuntimeError(
                f"Import source continued changing for {max_wait:g} seconds: {file_path}"
            )
        logger.info(
            "Import source changed while waiting; checking again: %s",
            file_path,
        )

def import_to_omero(target_conn, file_path, target_id, target_type="dataset", config=None, transfer_type="ln_s", run_id=None):
    if config is None:
        config = {}

    if run_id is None:
        run_id = create_run_id()

    file_path = Path(file_path)
    logger.info(f"Starting import to OMERO - File: {file_path}, Target: {target_type} ({target_id})")
    wait_for_file_stability(file_path)
    with tempfile.TemporaryDirectory(prefix=f"omero-import_{run_id}_") as temp_dir:
        temp_dir = Path(temp_dir)
        import_log_path = temp_dir / "import.log"
        error_log_path = temp_dir / "error.log"

        cli = CLI()
        cli.register('import', ImportControl, '_')
        cli.register('sessions', SessionsControl, '_')

        arguments = [
            'import',
            '-k', target_conn.getSession().getUuid().val,
            '-s', target_conn.host,
            '-p', str(target_conn.port or PORT),
            f'--transfer={transfer_type}',
            '--no-upgrade',
            '--file', str(import_log_path),
            '--errs', str(error_log_path),
        ]

        # Add more arguments based on the config dictionary
        if 'parallel_upload_per_worker' in config:
            arguments.extend(['--parallel-upload', str(config['parallel_upload_per_worker'])])

        if 'parallel_filesets_per_worker' in config:
            arguments.extend(['--parallel-fileset', str(config['parallel_filesets_per_worker'])])

        if config.get('skip_all', False):
            arguments.extend(['--skip', 'all'])
        else:
            for skip_option in ('checksum', 'minmax', 'thumbnails', 'upgrade'):
                if config.get(f'skip_{skip_option}', False):
                    arguments.extend(['--skip', skip_option])

        if config.get('depth', False):
            arguments.extend(['--depth', str(config['depth'])])

        if target_type == 'screen':
            arguments.extend(['-r', str(target_id)])
        elif target_type == 'dataset':
            arguments.extend(['-d', str(target_id)])
        else:
            raise ValueError("Invalid target_type. Must be 'dataset' or 'screen'.")

        arguments.append(str(file_path))

        # Invoke the CLI command
        logger.info(f"Invoking OMERO CLI import command with arguments: {arguments}")
        cli.invoke(arguments)

        image_id_dict = parse_import_image_ids(import_log_path, file_path)
        forward_omero_cli_log(error_log_path, logger.info)
        preserved_log_dir = None

        if cli.rv == 0:
            logger.info(f'Imported successfully: "{file_path}"')
        # preserve the logs if the import failed
        else:
            preserved_log_dir = preserve_failed_omero_logs(temp_dir, run_id)
            logger.error(
                f'Import failed for "{file_path}" with OMERO CLI return '
                f'code {cli.rv}; logs preserved in {preserved_log_dir}'
            )

        return {
            "success": cli.rv == 0,
            "return_code": cli.rv,
            "image_id_dict": image_id_dict,
            "preserved_log_dir": str(preserved_log_dir) if preserved_log_dir else None,
        }

#####################
### Main function ###
#####################

def _import_manifest(json_file_path, tag_use="self"):
    run_id = create_run_id()
    logger.extra["run_id"] = run_id
    logger.info("Starting importer run.")

    # read OMERO admin credentials from file
    try:
        with open(CREDENTIALS_FILE, 'r', encoding="utf-8") as f:
            credentials = json.load(f)
            sudo_as = credentials['user']
            sudo_password = credentials['password']
    except FileNotFoundError:
        raise FileNotFoundError(f"Credentials file not found: {CREDENTIALS_FILE}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding JSON from credentials file {CREDENTIALS_FILE}: {e}")
    except Exception as e:
        raise RuntimeError(f"Error reading credentials from {CREDENTIALS_FILE}: {e}")
    
    # read import.json file
    try:
        with open(json_file_path, 'r', encoding="utf-8") as f:
            json_data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"JSON file not found: {json_file_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding JSON from file {json_file_path}: {e}")

    conn = BlitzGateway(sudo_as, sudo_password, host=HOST, port=PORT, secure=True)
    conn.connect()
    atexit.register(conn.close)

    query_service = conn.getQueryService()
    groups = []
    users = []
    for group_name, group_data in json_data.get('group', {}).items():
        groups.append(group_name)
        for user_name in group_data.get('user', {}).keys():
            users.append((group_name, user_name))

# Validate that all groups and users exist in the correct relation on the OMERO server
    for group_name in groups:
        params = omero.sys.ParametersI()
        params.map = {"name": rstring(group_name)}
        query = (
            "SELECT group FROM ExperimenterGroup group "
            "WHERE group.name = :name"
        )
        if query_service.findByQuery(query, params) is None:
            message = f"Group '{group_name}' does not exist on the OMERO server."
            logger.error(message)
            raise RuntimeError(message)

    resolved_users = []
    resolved_experimenters = {}
    for group_name, requested_user_name in users:
        try:
            params = omero.sys.ParametersI()
            params.map = {"user_name": rstring(requested_user_name)}
            query = (
                "SELECT user FROM Experimenter user "
                "WHERE user.omeName = :user_name"
            )
            result = query_service.findByQuery(query, params)
            resolved_user_name = requested_user_name
            if result is None:
                user_name_suffix = requested_user_name.rsplit("_", 1)[-1]
                if user_name_suffix != requested_user_name and user_name_suffix:
                    params.map = {"user_name": rstring(user_name_suffix)}
                    result = query_service.findByQuery(query, params)
                    if result is not None:
                        resolved_user_name = result.omeName.getValue()
                        logger.info(
                            f"User '{requested_user_name}' resolved from underscore suffix "
                            f"to omeName '{resolved_user_name}'."
                        )

            if result is None:
                parts = re.split(r"[ ,_-]+", requested_user_name)
                if len(parts) <= 1:
                    raise RuntimeError(
                        f"User '{requested_user_name}' does not exist on the OMERO server."
                    )
                params.map = {
                    "first_name": rstring(parts[0]),
                    "last_name": rstring(parts[-1]),
                }
                query = (
                    "SELECT user FROM Experimenter user "
                    "WHERE user.firstName = :first_name AND user.lastName = :last_name"
                )
                result = query_service.findByQuery(query, params)
                if result is None:
                    raise RuntimeError(
                        f"User '{requested_user_name}' does not exist on the OMERO server."
                    )
                resolved_user_name = result.omeName.getValue()
                logger.info(
                    f"User '{requested_user_name}' found by full name. "
                    f"Replacing with omeName '{resolved_user_name}'."
                )

            if resolved_user_name != requested_user_name:
                user_data = json_data['group'][group_name]['user'].pop(
                    requested_user_name
                )
                json_data['group'][group_name]['user'][resolved_user_name] = merge_user_data(
                    user_data,
                    json_data['group'][group_name]['user'].get(resolved_user_name),
                )
            resolved_users.append((group_name, resolved_user_name))
            resolved_experimenters[resolved_user_name] = result
        except Exception as error:
            logger.error(
                f"Skipping user '{requested_user_name}' in group '{group_name}': {error}",
                exc_info=True,
            )
            json_data['group'][group_name]['user'].pop(requested_user_name, None)

    users = []
    for group_name, user_name in resolved_users:
        params = omero.sys.ParametersI()
        params.map = {"group_name": rstring(group_name), "user_name": rstring(user_name)}
        query = (
            "SELECT membership FROM GroupExperimenterMap membership "
            "JOIN FETCH membership.child user "
            "JOIN FETCH membership.parent grp "
            "WHERE grp.name = :group_name AND user.omeName = :user_name"
        )
        try:
            if query_service.findByQuery(query, params) is None:
                raise RuntimeError(f"User '{user_name}' is not in group '{group_name}'.")
            users.append((group_name, user_name))
        except Exception as error:
            logger.error(
                f"Skipping user '{user_name}' in group '{group_name}' during membership validation: {error}",
                exc_info=True,
            )
            json_data['group'][group_name]['user'].pop(user_name, None)

    logger.info("Validated %d user/group memberships; skipped invalid users.", len(users))

# Validate that all datasets and projects exist in the correct relation on the OMERO server
    dataset_project_pairs = extract_dataset_project_pairs(json_data)
    logger.info(f"Found {len(dataset_project_pairs)} unique dataset/project combinations.")
    for pair in dataset_project_pairs:
        project_identifier = pair['project']
        dataset_identifier = pair['dataset']
        target_conn = get_target_context_connection(conn, pair)
        try:
            project = resolve_project_in_context(target_conn, project_identifier)
            if project is not None:
                validate_object_context(project, "Project", pair)
            elif project_identifier is not None:
                project = ProjectI()
                project.setName(rstring(str(project_identifier)))
                project = target_conn.getUpdateService().saveAndReturnObject(project)
                logger.info(
                    f"Project '{project_identifier}' created for "
                    f"'{pair['user']}/{pair['group']}'."
                )

            project_id = project.getId() if project is not None else None
            dataset_is_numeric = str(dataset_identifier).strip().isdecimal()
            if project is None and not dataset_is_numeric:
                dataset = resolve_dataset_without_project_by_name(
                    target_conn, dataset_identifier
                )
            else:
                dataset = resolve_dataset_in_context(
                    target_conn, dataset_identifier, project_id
                )
            if dataset is not None:
                validate_object_context(dataset, "Dataset", pair)
            else:
                dataset = DatasetI()
                dataset.setName(rstring(str(dataset_identifier)))
                dataset = target_conn.getUpdateService().saveAndReturnObject(dataset)
                logger.info(
                    f"Dataset '{dataset_identifier}' created for "
                    f"'{pair['user']}/{pair['group']}'."
                )

            if not str(dataset_identifier).strip().isdecimal():
                pair["dataset_data"]["dataset_identifier"] = dataset.getId()
            if project is not None:
                ensure_dataset_linked_to_project(target_conn, project, dataset)
        finally:
            target_conn.close()

    logger.info("All Datasets and Projects exist on the OMERO server and belong to the correct users and groups.")

# parse the metadata from the JSON data and perform the import for each filepath
    import_records = []
    tag_cache = {}
    for target_group, group_data in json_data.get("group", {}).items():
        for target_user, user_data in group_data.get("user", {}).items():
            pair = {"group": target_group, "user": target_user}
            target_conn = get_target_context_connection(conn, pair)
            try:
                target_conn.getSession().setTimeToLive(TTL_FOR_IMPORT_CONN)
                for dataset_data in user_data.get("datasets", []):
                    project_identifier = dataset_data.get("project_identifier")
                    dataset_identifier = get_id_value(dataset_data["dataset_identifier"])
                    for image_path, file_metadata in dataset_data.get("files", {}).items():
                        in_place = file_metadata.get("in-place", False)
                        tags = file_metadata.get("Tag", [])
                        key_value_pairs = file_metadata.get("kv-pair", {})
                        import_record = {
                            "target_group": target_group,
                            "target_username": target_user,
                            "project_identifier": project_identifier,
                            "dataset_identifier": dataset_identifier,
                            "image_path": image_path,
                            "in_place": in_place,
                            "tags": tags,
                            "key_value_pairs": key_value_pairs,
                        }
                        logger.info(
                            f"Starting import for '{image_path}' in Dataset {dataset_identifier} "
                            f"for user/group '{target_user}/{target_group}'."
                        )
                        # try the import safely, and if it fails, log the error and continue with the next file
                        try:
                            result = import_to_omero(
                                target_conn=target_conn,
                                file_path=image_path,
                                target_id=dataset_identifier,
                                target_type="dataset",
                                config=import_config,
                                transfer_type=(
                                    "ln_s" if import_record["in_place"] else "upload"
                                ),
                                run_id=run_id,
                            )
                        except Exception:
                            logger.exception(
                                f"Unexpected error importing '{image_path}' "
                                f"in Dataset {dataset_identifier}; continuing with next file."
                            )
                            import_record["image_id_dict"] = {}
                            import_record["success"] = False
                            import_record["return_code"] = None
                            import_record["error"] = "Unexpected importer exception"
                            import_records.append(import_record)
                            continue

                        import_record["image_id_dict"] = result["image_id_dict"]
                        import_record["success"] = result["success"]
                        import_record["return_code"] = result["return_code"]
                        if not result["success"]:
                            logger.error(
                                f"Import failed for '{image_path}' in Dataset {dataset_identifier}."
                            )
                            import_records.append(import_record)
                            continue
                        image_ids = result["image_id_dict"].get(
                            normalise_import_path(image_path), []
                        )
                        if image_ids:
                            import_record["annotation_status"] = apply_image_metadata(
                                target_conn,
                                image_ids,
                                tags,
                                key_value_pairs,
                                tag_cache,
                                tag_use,
                            )
                        else:
                            logger.warning(
                                f"Import succeeded for '{image_path}' but returned no image IDs; "
                                "manifest annotations were not applied."
                            )
                            import_record["annotation_status"] = {
                                "success": False,
                                "annotated_image_ids": [],
                                "annotated_image_count": 0,
                                "errors": [{"error": "Import returned no image IDs."}],
                            }
                        import_records.append(import_record)
                        logger.info(
                            f"Import completed for '{image_path}'; image IDs: {result['image_id_dict']}"
                        )
            finally:
                target_conn.close()

    send_completion_notifications(conn, resolved_experimenters, import_records)
    conn.close()
    return import_records_succeeded(import_records)

def main(json_path, tag_use="self"):
    """Claim, import, and finalise the newest ready transfer manifest."""
    manifest_path = select_latest_transfer_manifest(json_path)
    claimed_path = claim_transfer_manifest(manifest_path)
    succeeded = False
    try:
        succeeded = _import_manifest(claimed_path, tag_use)
        return succeeded
    finally:
        finalise_transfer_manifest(claimed_path, succeeded)

if __name__ == "__main__":
    command_line_args = parse_command_line_args()
    try:
        if not main(command_line_args.json_path, command_line_args.tag_use):
            raise RuntimeError("Transfer manifest processing failed and was quarantined.")
    except Exception:
        logger.exception("Importer terminated unexpectedly")
        logging.shutdown()
        raise
