[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & (Join-Path $PSScriptRoot 'tools/build-native.ps1')
    $ErrorActionPreference = 'Continue'
    uv build
    if ($LASTEXITCODE -ne 0) { throw 'Python package build failed.' }
}
finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
