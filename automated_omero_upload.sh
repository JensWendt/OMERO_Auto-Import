#!/usr/bin/env bash
#
# upload_new_images.sh
#   - Load watch paths from WATCH_LIST_FILE
#   - For each watch path:
#       • Verify it exists
#       • Load its suffix list from   <watch_path>/suffixes.json
#       • Find new image files (created or copied <24h) matching those suffixes
#       • Read ome_user & dataset_id from elabftw-metadata.json next to each file
#       • Import via the OMERO CLI, using --sudo for the admin account
#   - Log all output to /var/log/omero_upload.log
#
# Requires: bash, sudo, jq, find, stat, awk
# Install missing tools with:
#   sudo apt update && sudo apt install -y jq

set -euo pipefail

# ——— Configuration —————————————————————————————————————————————
WATCH_LIST_FILE="/path/to/your/.watch_directory_list.json"
ADMIN_CRED_FILE="/opt/omero/credentials_auto_in-place_import.json"
SUFFIX_FILE=".suffixes.json"
OMERO_BIN="/opt/omero/server/venv3/bin/omero"
CLIENT_DIR="/opt/omero/server/OMERO.server/lib/client"
METADATA_FILE="elabftw-metadata.json"
README_FILE="README*.html"
OMERO_HOST="localhost"
LOGFILE="/var/log/omero_upload.log"
TIME_SPAN=86400  # 24 hours in seconds

# ——— package checks ————————————————————————————————————————
command -v jq   >/dev/null 2>&1 || { echo "ERROR: jq not installed"; exit 1; }
command -v find >/dev/null 2>&1 || { echo "ERROR: find not installed"; exit 1; }
command -v stat >/dev/null 2>&1 || { echo "ERROR: stat not installed"; exit 1; }
command -v awk  >/dev/null 2>&1 || { echo "ERROR: awk not installed"; exit 1; }

# Redirect stdout+stderr into the log
exec >>"${LOGFILE}" 2>&1

# Ensure we're not stuck in /root as cwd (avoids PermissionError when sudo'ing down into omero-server)
cd /tmp/omero || exit 1

echo "===== $(date '+%F %T') Starting new-images upload run ====="

# ——— Load & verify watch directories ————————————————————————————
if [[ ! -r "${WATCH_LIST_FILE}" ]]; then
    echo "ERROR: cannot read watch list ${WATCH_LIST_FILE}"
    exit 1
fi

mapfile -t WATCH_DIRS < <(jq -r '.paths[]' "${WATCH_LIST_FILE}")
if (( ${#WATCH_DIRS[@]} == 0 )); then
    echo "ERROR: no paths defined in ${WATCH_LIST_FILE}"
    exit 1
fi

VALID_DIRS=()
for d in "${WATCH_DIRS[@]}"; do
    if [[ -d "$d" ]]; then
        VALID_DIRS+=( "$d" )
    else
        echo "WARN: watch path '$d' does not exist or is not a directory, skipping."
    fi
done

if (( ${#VALID_DIRS[@]} == 0 )); then
    echo "ERROR: no valid watch directories found, exiting."
    exit 1
fi

# ——— Load admin OMERO credentials ——————————————————————————————
if [[ ! -r "${ADMIN_CRED_FILE}" ]]; then
    echo "ERROR: cannot read admin credentials ${ADMIN_CRED_FILE}"
    exit 1
fi
ADMIN_USER=$(jq -r '.user'     "${ADMIN_CRED_FILE}")
ADMIN_PASS=$(jq -r '.password' "${ADMIN_CRED_FILE}")
if [[ -z "${ADMIN_USER}" || -z "${ADMIN_PASS}" ]]; then
    echo "ERROR: empty admin user or password in ${ADMIN_CRED_FILE}"
    exit 1
fi

# ——— Process each watch directory ———————————————————————————————
for WATCH_DIR in "${VALID_DIRS[@]}"; do
    echo "--- Scanning '${WATCH_DIR}' ---"

    # 1) Load suffixes for this directory
    SUFFIX_FILE_PATH="${WATCH_DIR}/${SUFFIX_FILE}"
    if [[ ! -r "${SUFFIX_FILE_PATH}" ]]; then
        echo "WARN: no .suffixes.json in ${WATCH_DIR}, skipping."
        continue
    fi
    mapfile -t SUFFIXES < <(jq -r '.suffixes[]' "${SUFFIX_FILE_PATH}")
    if (( ${#SUFFIXES[@]} == 0 )); then
        echo "WARN: empty suffix list in ${SUFFIX_FILE_PATH}, skipping."
        continue
    fi

    # Build find predicate for these suffixes: -iname "*.tif" -o -iname "*.czi" …
    FIND_EXPR=()
    for s in "${SUFFIXES[@]}"; do
        FIND_EXPR+=( -iname "*${s}" -o )
    done
    unset 'FIND_EXPR[${#FIND_EXPR[@]}-1]'

    # 2) Find new files (created or copied <24h) matching suffixes
    echo "Looking for new image files in '${WATCH_DIR}'…"
    mapfile -t NEWFILES < <(
        find "${WATCH_DIR}" -type f \( "${FIND_EXPR[@]}" \) -print0 \
            | xargs -0 stat --format=$'%n\t%W' \
            | awk -F $'\t' '$2 > systime() - '"${TIME_SPAN}"' { print $1 }'
    )

    if (( ${#NEWFILES[@]} == 0 )); then
        echo "No new files found in '${WATCH_DIR}'."
        continue
    fi
    echo "Found ${#NEWFILES[@]} new file(s) in '${WATCH_DIR}'."

    # 3) Import each new file
    for filepath in "${NEWFILES[@]}"; do
        dir=$(dirname "${filepath}")
        meta="${dir}/${METADATA_FILE}"
        if [[ ! -r "${meta}" ]]; then
            echo "WARN: missing ${METADATA_FILE} next to ${filepath}, skipping."
            continue
        fi
        # extract dataset_id & ome_user from metadata file
        dataset_id=$(jq -r '.metafold_integration.external_links.omero.dataset_id' "${meta}")
        ome_user=$(jq -r '.metafold_integration.external_links.omero.user_name' "${meta}")

        if ! [[ "${dataset_id}" =~ ^[0-9]+$ ]]; then
            echo "WARN: invalid dataset_id '${dataset_id}' in ${meta}, skipping."
            continue
        fi
        if [[ -z "${ome_user}" ]]; then
            echo "WARN: empty ome_user in ${meta}, skipping."
            continue
        fi

        echo "Importing '${filepath}' as '${ome_user}' → dataset ${dataset_id}…"
        sudo -u omero-server \
            HOME="$(getent passwd omero-server | cut -d: -f6)" \
            "${OMERO_BIN}" import \
            --clientdir="${CLIENT_DIR}" \
            -C \
            -s="${OMERO_HOST}" \
            -u="${ome_user}" \
            -w="${ADMIN_PASS}" \
            --sudo="${ADMIN_USER}" \
            --transfer=ln_s \
            --skip=upgrade \
            --parallel-upload=4 \
            --depth=10 \
            -d="${dataset_id}" \
            "${filepath}"

        if [[ $? -eq 0 ]]; then
            echo "SUCCESS: imported '${filepath}'"
        else
            echo "ERROR: failed to import '${filepath}'"
        fi
    done

    # ─── 4) metadata + README uploads ────────────────────────────────────────────
    # 4a) find any metadata files born <24 h
    mapfile -t NEW_META < <(
        find "$WATCH_DIR" -type f -iname "${METADATA_FILE}" -print0 \
            | xargs -0 stat --format=$'%n\t%W' \
            | awk -F $'\t' '$2 > systime()-'"${TIME_SPAN}"' { print $1 }'
    )

    for meta in "${NEW_META[@]}"; do
        dir=$(dirname "$meta")

        # 4b) in same dir, find any README file born <24 h
        mapfile -t NEW_README < <(
            find "$dir" -maxdepth 1 -type f -iname "${README_FILE}" -print0 \
                | xargs -0 stat --format=$'%n\t%W' \
                | awk -F $'\t' '$2 > systime()-'"${TIME_SPAN}"' { print $1 }'
        )

        # 4c) only if we have both
        if (( ${#NEW_README[@]} > 0 )); then
            dataset_id=$(jq -r '.metafold_integration.external_links.omero.dataset_id' "${meta}")
            ome_user=$(jq -r '.metafold_integration.external_links.omero.user_name' "${meta}")
            echo "Annotating dataset ${dataset_id} with metadata + ${#NEW_README[@]} README(s)…"

            # helper function to upload a file & link it to the dataset
            upload_and_link () {
                local file="$1"
                echo "creating new session for user '$ome_user'"
                # create new session as there could be multiple users per directory
                sudo -u omero-server \
                    HOME="$(getent passwd omero-server | cut -d: -f6)" \
                    "${OMERO_BIN}" login \
                    -C \
                    -s="${OMERO_HOST}" \
                    -u="${ome_user}" \
                    -w="${ADMIN_PASS}" \
                    --sudo="${ADMIN_USER}"

                echo "uploading '$file'"
                original_id=$(sudo -u omero-server HOME="$(getent passwd omero-server | cut -d: -f6)" \
                    "${OMERO_BIN}" upload "$file")

                file_annotation=$(sudo -u omero-server HOME="$(getent passwd omero-server | cut -d: -f6)" \
                    "${OMERO_BIN}" obj new FileAnnotation file="$original_id")

                sudo -u omero-server HOME="$(getent passwd omero-server | cut -d: -f6)" \
                    "${OMERO_BIN}" obj new DatasetAnnotationLink \
                    parent=Dataset:"$dataset_id" child="$file_annotation"
            }

            # upload the metadata JSON first
            upload_and_link "$meta"
            # then upload each README
            for readme in "${NEW_README[@]}"; do
                upload_and_link "$readme"
            done
        fi
    done
done

echo "===== $(date '+%F %T') Done ====="