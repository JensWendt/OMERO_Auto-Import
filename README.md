# Automated OMERO Import Script

This is a bash script to automatically import Images (and certain files as FileAnnotations) from "watched" directories into OMERO Datasets. It is designed to run as cronjob.

It is designed to work with [Metafold](https://github.com/ThZobel/MetaFold), but can be adapted to work without it.

## Features

- Imports image files which have been newly created or copied into (sub)directories in the last 24h
- Flexible import time intervals
- Easily expandable list of watched/scanned directories
- Customizable list of importable image file suffixes for each watched directory
- Automatically parses target dataset and OMERO user from a `elabftw-metadata.json` file (from Metafold, but can be adapted)
- Import via an OMERO admin user with `sudo` for the different OMERO users
- Imports the `elabftw-metadata.json` and the `README.html` (both Metafold related) as FileAnnotation to the target OMERO Dataset

## What does it NOT do

- import Metadata (this is what Metafold is for in this context)
- create Datasets (also happens in Metafold)

## Installation

- Copy the `automated_omero_import.sh` script onto your Linux machine which is running OMERO.server.
- Create a `.watch-directory-list.json` wherever you please according to the sample provided in this repo. Then set the path as `WATCH_LIST_FILE` in the script parameters
- Create a `credentials_auto_import.json` wherever you please according to the sample provided in this repo. Then set the path as `ADMIN_CRED_FILE` in the script parameters
- Create the corresponding OMERO admin user (e.g. `auto_import`)
- Create in eached 'watched' directory at the top level a `.suffixes.json` according to the sample provided in this repo. For the first directory in the sample `.watch-directory-list.json` this would be `/mnt/hive/Project_01/.suffixes.json`. If you want another name for this file, change it in the `SUFFIX_FILE` script parameter.
- The script assumes you have a metadata file containing the target dataset Id and the OMERO username next to the files that are to be imported. If you use the script in tandem with Metafold this will be `elabftw-metadata.json` and automatically generated. You can change this, which is described in the last paragraph of [Details](#details)

## Details

The script is the matching piece for Metafold to enable an automatic import from Image files that have been deposited into the directories created by Metafold.

The `.watch-directory-list.json` contains all directories that are being searched for newly created or newly copied files.

Each directory in the list must contain a `.suffixes.json` at top level indicating which files are suitable for import. This allows for a fine-grained distinction of what to import, enabling use-cases e.g. HCS where only the companion file (\*.xml) needs to be imported and not every single .tif file. Changing the script parameter `SUFFIX_FILE` allows for a different file name.

The directories are then being searched utilizing the `%W` birthtime stat metadata of each file to grab all newly created and newly copied files.

The Image files must be accompanied by an `elabftw-metadata.json` file from Metafold, which gets parsed for the target OMERO dataset Id and the OMERO username of the user for whom the import happens.

If a Metafold `README.html` is found on the same level as the `elabftw-metadata.json` both are uploaded as FileAnnotations on the target OMERO dataset. If not, this is skipped.

The script assumes the standard `OMERO_BIN="/opt/omero/server/venv3/bin/omero"` and `CLIENT_DIR="/opt/omero/server/OMERO.server/lib/client"` if you installed OMERO somehow different you will need to adapt this in the script parameters. The `CLIENT_DIR` is only needed for the actual `omero import` command.

The script is written with the assumption that it is run on the system that is running the OMERO.server and has external file shares mounted. Therefore `OMERO_HOST` is set as `localhost`, but can be changed.

In the same manner we assume usage in tandem with Metafold. The parsed metadata file is therefore `METADATA_FILE="elabftw-metadata.json"`. You can change this, but then you have to change the way the `dataset_id` and `ome_user` is parsed [here](./automated_omero_upload.sh#L129-130) and [here](./automated_omero_upload.sh#L185-186).

Everything is logged to `LOGFILE="/var/log/omero_upload.log"`.

## Planned upcoming features

- Decouple time-gated search of accompanying `README.html` and `elabftw-metadata.json`
- Support for `Projects`, `Screens` and `Plates`
- If Metafold gains significant traction --> Integrate functionality into [W-IDM_OmeroImporterPy](https://github.com/WU-BIMAC/W-IDM_OmeroImporterPy)

## Prerequisites

#### OMERO server Linux:

- Kernel: 4.11+ (preferably 5.4+ for stability)
- Coreutils: 8.31+
- jq
- NFS client: nfs-common with NFSv4.2 support
- CIFS client: cifs-utils 6.8+

#### File Share server:

- NFS Server: NFSv4.2 support + birth-time-capable filesystem
- CEPHFS/CIFS/SMB Server: SMB 2.0+ with timestamp preservation

## Distinction from other Automatic Importers:

### OMERO.dropbox

- is not feasible for network shares, as it relies on OS-level file system events which are not reliable for mounted file shares

### omero_autoimport

https://github.com/erickmartins/omero_autoimport

- relies on difference in `mtime` therefore not aligned with the user intuition, which is better captured by `birthtime`

### W-IDM_OmeroImporterPy

https://github.com/WU-BIMAC/W-IDM_OmeroImporterPy

- has a rigid Project>Dataset>Images directory structure
- almost full functionality match
- keeps a record of file paths that have already been imported --> independent of file metadata which might be tricky to get
- the setup is a bit more complex with preparation of metadata.xlsx/.csv files
