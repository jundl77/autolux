[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # Component builds format sources, then build and test their runtimes.
    & (Join-Path $PSScriptRoot 'build-radius.ps1')
    & (Join-Path $PSScriptRoot 'build-starfire.ps1')
    $ErrorActionPreference = 'Continue'
    uv build
    if ($LASTEXITCODE -ne 0) { throw 'Python package build failed.' }
}
finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
