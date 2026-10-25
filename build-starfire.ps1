[CmdletBinding()]
param([string]$Cargo = '')

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'tools/build-native.ps1') -Package starfire -Cargo $Cargo
