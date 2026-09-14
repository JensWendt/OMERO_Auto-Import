# Example for PowerShell script that can be 

$ErrorActionPreference = "Stop"

$python = "C:\miniforge\envs\omero-upload\python.exe"
$parser = "D:\OMERO_AutoImport\parser.py"
$importer = "D:\OMERO_AutoImport\OMERO_import.py"
$basePath = "E:\PROJECTS\AUTOUPLOAD"
$transfer_file = "E:\PROJECTS\AUTOUPLOAD\import.json"
$parser_log = "E:\PROJECTS\AUTOUPLOAD\Logs"
$env:OMERO_IMPORT_LOG_DIR = "E:\PROJECTS\AUTOUPLOAD\Logs"
$env:OMERO_CREDENTIALS = "C:\Users\admin\credentials_auto_in-place_import.json"

& $python $parser $basePath $transfer_file --log-file $parser_log --metafold fallback
if ($LASTEXITCODE -ne 0) {
    throw "Parser failed with exit code $LASTEXITCODE"
}

& $python $importer $transfer_file --tag-use self
if ($LASTEXITCODE -ne 0) {
    throw "Importer failed with exit code $LASTEXITCODE"
}