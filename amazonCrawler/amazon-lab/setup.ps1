$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -m venv .venv
    } else {
        & python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
        if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 first.' }
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
    $deliveryPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    & $deliveryPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $deliveryPython -m pip install --no-deps -e .\amzpy
    if ($LASTEXITCODE -ne 0) { throw 'Local AmzPy installation failed.' }
    & $deliveryPython -I collect.py dry-run
    if ($LASTEXITCODE -ne 0) { throw 'Offline startup check failed.' }
} finally { Pop-Location }
