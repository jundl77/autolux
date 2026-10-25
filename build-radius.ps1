[CmdletBinding()]
param([string]$Cargo = '')

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'tools/build-native.ps1') -Package radius -Cargo $Cargo
