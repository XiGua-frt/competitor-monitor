$ErrorActionPreference = 'Stop'
$deliveryPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$deliveryFailures = 0
foreach ($deliveryTest in (Get-ChildItem -LiteralPath $PSScriptRoot -Filter 'test_*.py' -File | Sort-Object Name)) {
    & $deliveryPython -I $deliveryTest.FullName
    if ($LASTEXITCODE -ne 0) { $deliveryFailures++ }
}
exit ([int]($deliveryFailures -gt 0))
