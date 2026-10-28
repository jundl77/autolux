param([Parameter(Mandatory=$true)][string]$Bundle,
      [Parameter(Mandatory=$true)][string]$RunDirectory)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$mutex = New-Object Threading.Mutex($false, 'Global\Autolux-radius_rx')
$owned = $false
$child = $null
try {
    try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { throw 'A Radius receiver session already owns the hardware.' }
    $env:AUTOLUX_RUNTIME_DIR = $RunDirectory
    $exe = Join-Path $Bundle 'radius_rx.exe'
    $config = Join-Path $RunDirectory 'receiver.json'
    $settings = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
    $controllerAddress = [Net.IPAddress]::Parse($settings.peer_address).ToString()
    $busPort = [int]$settings.bus_port
    if ($busPort -lt 1 -or $busPort -gt 65535) { throw 'Invalid Radius TCP port.' }
    $firewallName = 'Autolux-Radius-TCP'
    if (Get-NetFirewallRule -Name $firewallName -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule -Name $firewallName -Enabled True -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $busPort -RemoteAddress $controllerAddress | Out-Null
    } else {
        New-NetFirewallRule -Name $firewallName -DisplayName 'Autolux Radius playback' -Enabled True `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort $busPort `
            -RemoteAddress $controllerAddress | Out-Null
    }
    $log = Join-Path $RunDirectory 'process.log'
    $errorLog = Join-Path $RunDirectory 'error.log'
    $child = Start-Process -FilePath $exe -ArgumentList ('"' + $config + '"') -WorkingDirectory $Bundle `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $errorLog
    # Relay stderr live over SSH into the controller's receiver-ssh.log, timestamped so
    # receiver warnings line up with the controller log; without it they appear only at exit.
    $reader = $null
    try {
        $reader = [IO.StreamReader]::new([IO.FileStream]::new($errorLog, [IO.FileMode]::Open,
            [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite))
    } catch {}
    while (-not $child.WaitForExit(250)) {
        if ($reader) {
            while ($null -ne ($line = $reader.ReadLine())) {
                [Console]::Out.WriteLine((Get-Date -Format 'HH:mm:ss.fff') + ' ' + $line)
            }
        }
    }
    $child.WaitForExit()
    $child.Refresh()
    if ($reader) {
        Get-Content -LiteralPath $log -ErrorAction SilentlyContinue
        $reader.ReadToEnd()
        $reader.Dispose()
    } else {
        Get-Content -LiteralPath $log,$errorLog -ErrorAction SilentlyContinue
    }
    exit $child.ExitCode
} finally {
    if ($child -and -not $child.HasExited) {
        New-Item -ItemType File -Force -Path (Join-Path $RunDirectory 'stop') | Out-Null
        if (-not $child.WaitForExit(5000)) { $child.Kill() }
    }
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
