[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PublicKey,
    [Parameter(Mandatory=$true)][string]$ControllerAddress
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this one-time receiver setup from an elevated PowerShell.'
}
if ($PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/]+={0,2}( .*)?$') {
    throw 'Expected an Ed25519 public key, never a private key.'
}
$parsedAddress = [Net.IPAddress]::Parse($ControllerAddress)
if ($parsedAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'ControllerAddress must be an IPv4 address.'
}
$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($capability.State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

$directory = Join-Path $env:ProgramData 'ssh'
$authorizedKeys = Join-Path $directory 'administrators_authorized_keys'
$existing = if (Test-Path -LiteralPath $authorizedKeys) { Get-Content -LiteralPath $authorizedKeys } else { @() }
if ($PublicKey -notin $existing) {
    [IO.File]::AppendAllText($authorizedKeys, $PublicKey.Trim() + "`n", [Text.UTF8Encoding]::new($false))
}
& icacls.exe $authorizedKeys /inheritance:r /grant '*S-1-5-32-544:F' '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to secure SSH authorized keys.' }

$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($rule) {
    Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -RemoteAddress $ControllerAddress
} else {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'Autolux SSH deployment' -Enabled True `
        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -RemoteAddress $ControllerAddress | Out-Null
}

# Pin this public host key on the controller before unattended deployment.
Get-Content -LiteralPath (Join-Path $directory 'ssh_host_ed25519_key.pub')
Write-Host 'Autolux SSH setup complete.'
