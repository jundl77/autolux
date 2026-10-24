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
    $log = Join-Path $RunDirectory 'process.log'
    $errorLog = Join-Path $RunDirectory 'error.log'
    $child = Start-Process -FilePath $exe -ArgumentList ('"' + $config + '"') -WorkingDirectory $Bundle `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $errorLog
    $child.WaitForExit()
    $child.Refresh()
    Get-Content -LiteralPath $log,$errorLog -ErrorAction SilentlyContinue
    exit $child.ExitCode
} finally {
    if ($child -and -not $child.HasExited) {
        New-Item -ItemType File -Force -Path (Join-Path $RunDirectory 'stop') | Out-Null
        if (-not $child.WaitForExit(5000)) { $child.Kill() }
    }
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
