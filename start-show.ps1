[CmdletBinding()]
param(
    [string]$DmxProject = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dmx-enttec-node'),
    [string]$MidiPort = 'loopMIDI Port',
    [string]$InputDevice = 'CABLE Output',
    [switch]$CheckOnly,
    [switch]$ReplaceAudioSender,
    [switch]$NoUi
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$senderExe = Join-Path $DmxProject 'cmake-build-release\app_audio_sender\app_audio_sender.exe'
$sourceConfig = Join-Path $DmxProject 'app_audio_sender\audio_sender.json'
foreach ($path in @($python, $senderExe, $sourceConfig)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing: $path" }
}

if (-not $CheckOnly -and -not (Get-Process loopMIDI -ErrorAction SilentlyContinue)) {
    $loopMidi = Join-Path ${env:ProgramFiles(x86)} 'Tobias Erichsen\loopMIDI\loopMIDI.exe'
    if (-not (Test-Path -LiteralPath $loopMidi)) { throw 'Start loopMIDI and create the configured MIDI port first.' }
    Start-Process -FilePath $loopMidi -WindowStyle Hidden
    Start-Sleep -Seconds 1
}

Push-Location $PSScriptRoot
try {
    $probe = @'
import json, sys
import pyaudio, rtmidi
from lib.main import PLAYBACK_DELAY_SEC
from lib.section_chain import artifacts_present
p = pyaudio.PyAudio()
try:
    api = p.get_default_host_api_info()['index']
    inputs = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
    inputs = [d for d in inputs if d['hostApi'] == api and d['maxInputChannels'] > 0
              and d['name'].casefold().startswith(sys.argv[2].casefold())]
    ports = rtmidi.MidiOut().get_ports()
    midi = [(i, n) for i, n in enumerate(ports) if n.casefold().startswith(sys.argv[1].casefold())]
    if len(inputs) != 1 or len(midi) != 1:
        raise SystemExit(f'Need one matching audio input and MIDI port; audio={inputs}, MIDI={midi}. Run auto_pilot list.')
    if not artifacts_present():
        raise SystemExit('The shipped neural model is missing; refusing to start a degraded show.')
    print(json.dumps(dict(input_index=int(inputs[0]['index']), input_name=inputs[0]['name'],
                         midi_index=midi[0][0], midi_name=midi[0][1], delay=PLAYBACK_DELAY_SEC)))
finally:
    p.terminate()
'@
    $deviceJson = & $python -c $probe $MidiPort $InputDevice
    if ($LASTEXITCODE -ne 0) { throw 'Live device/model checks failed.' }
    $devices = $deviceJson | ConvertFrom-Json
    $config = Get-Content -Raw -LiteralPath $sourceConfig | ConvertFrom-Json
    $guid = [regex]::Match($config.audio_device_id, '\{[^{}]+\}$').Value
    $deviceKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture\$guid\Properties"
    $senderInput = (Get-ItemProperty -LiteralPath $deviceKey).'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
    if (-not $senderInput -or -not $devices.input_name.StartsWith($senderInput, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The sender captures '$senderInput', but autopilot captures '$($devices.input_name)'. Select the same undelayed source."
    }
    if ($config.audio_sample_rate -ne 44100 -or $config.audio_format -ne 'SoundIoFormatFloat32LE') {
        throw 'This show setup requires the existing 44.1 kHz Float32LE sender/receiver format.'
    }
    $config | Add-Member -NotePropertyName playback_delay_seconds -NotePropertyValue $devices.delay -Force
    & $senderExe --check-config $sourceConfig | Write-Verbose
    if ($LASTEXITCODE -ne 0) { throw 'Build and install the updated audio sender first; the old binary cannot apply the playback delay.' }
    Write-Host "Input: $($devices.input_name); MIDI: $($devices.midi_name)"
    Write-Host "Room audio: $($config.audio_server_ip):$($config.audio_server_port); delay: $($devices.delay)s"
    if ($CheckOnly) { return }

    $running = Get-Process app_audio_sender -ErrorAction SilentlyContinue
    if ($running) {
        if (-not $ReplaceAudioSender) {
            throw "An audio sender is already running (PID $($running.Id -join ', ')). Use -ReplaceAudioSender to restart this project's sender with the matching delay."
        }
        $expectedSender = (Resolve-Path -LiteralPath $senderExe).Path
        foreach ($process in $running) {
            if ($process.Path -ne $expectedSender) { throw "A sender from another location is running: $($process.Path)" }
        }
        foreach ($process in $running) {
            Stop-Process -Id $process.Id
            $process.WaitForExit()
        }
    }
    $runDir = Join-Path $PSScriptRoot ('.live\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $runtimeConfig = Join-Path $runDir 'audio_sender.json'
    [IO.File]::WriteAllText($runtimeConfig, ($config | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $sender = Start-Process -FilePath $senderExe -ArgumentList ('"' + $runtimeConfig + '"') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runDir 'audio-sender.log') `
        -RedirectStandardError (Join-Path $runDir 'audio-sender-errors.log')
    try {
        Start-Sleep -Milliseconds 500
        if ($sender.HasExited) { throw "Audio sender exited. See $runDir" }
        $arguments = @('auto_pilot', 'run', [string]$devices.midi_index, '-i', [string]$devices.input_index)
        if (-not $NoUi) { $arguments += '--ui' }
        Write-Host 'Starting the show. Ctrl-C stops lighting and the audio sender.'
        & $python @arguments
        if ($LASTEXITCODE -ne 0) { throw "Autopilot exited with code $LASTEXITCODE." }
    }
    finally {
        if (-not $sender.HasExited) { Stop-Process -Id $sender.Id }
    }
}
finally {
    Pop-Location
}
