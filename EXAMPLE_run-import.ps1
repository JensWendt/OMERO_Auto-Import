# Example for PowerShell script that can be 

$ErrorActionPreference = "Stop"

$python = "C:\miniforge\envs\omero-upload\python.exe"
$parser = "D:\OMERO_AutoImport\parser.py"
$importer = "D:\OMERO_AutoImport\OMERO_import.py"
$basePath = "E:\PROJECTS\AUTOUPLOAD"
$transfer_directory = "E:\PROJECTS\AUTOUPLOAD\"
$parser_log = "E:\PROJECTS\AUTOUPLOAD\Logs"
$env:OMERO_IMPORT_LOG_DIR = "E:\PROJECTS\AUTOUPLOAD\Logs"
$env:OMERO_CREDENTIALS = "C:\Users\admin\credentials_auto_in-place_import.json"
$env:OMERO_SUCCESS_IMPORT_DIR = "E:\PROJECTS\AUTOUPLOAD\successfull_imports"
$env:OMERO_FAILED_IMPORTS_DIR = "E:\PROJECTS\AUTOUPLOAD\failed_imports"
# $env:OMERO_EMAIL_ENABLED = "true"
# $env:OMERO_EMAIL_SMTP_HOST = "smtp.university.example"
# $env:OMERO_EMAIL_SMTP_PORT = "587"
# $env:OMERO_EMAIL_SMTP_SECURITY = "starttls"
# $env:OMERO_EMAIL_FROM = "omero-import@university.example"
# Set SMTP credentials through the environment or an OS secret store; do not commit them.

& $python $parser $basePath $transfer_directory --log-file $parser_log --metafold "fallback" --time "3600"
if ($LASTEXITCODE -ne 0) {
    throw "Parser failed with exit code $LASTEXITCODE"
}

& $python $importer $transfer_directory --tag-use self
if ($LASTEXITCODE -ne 0) {
    throw "Importer failed with exit code $LASTEXITCODE"
}