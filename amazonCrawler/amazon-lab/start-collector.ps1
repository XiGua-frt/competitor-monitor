param(
    [ValidateSet('status','dry-run','import-saved','once','schedule','pause','resume','report','demo')]
    [string]$Mode = 'status'
)
$collectorPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$collectorEntry = Join-Path $PSScriptRoot 'collect.py'
& $collectorPython -I $collectorEntry $Mode
exit $LASTEXITCODE
