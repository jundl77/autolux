[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # The native build formats both languages, then builds and tests Radius.
    & (Join-Path $PSScriptRoot 'build-radius.ps1')
    $ErrorActionPreference = 'Continue'
    uv build
    if ($LASTEXITCODE -ne 0) { throw 'Python package build failed.' }
}
finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
