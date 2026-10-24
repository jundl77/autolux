# Forward directly to the Python coordinator; it owns process lifecycle.
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync --extra dev --extra visualizer first.' }
Push-Location $PSScriptRoot
try {
    & $python -m autolux @args
    exit $LASTEXITCODE
}
finally { Pop-Location }
