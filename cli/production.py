"""Production topology: controller here; audio/DMX receivers on the output PC."""

import argparse
import asyncio
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from cli.native_runtime import NativeRuntime, NativeSpec, validate_native

ROOT = Path(__file__).resolve().parents[1]


def add_arguments(parser):
    parser.add_argument('--radius-dir', type=Path, default=ROOT / 'radius')
    parser.add_argument('--tx-config', type=Path)
    parser.add_argument('--rx-config', type=Path)
    parser.add_argument('--midi-port', default='loopMIDI Port 1')
    parser.add_argument('--input-device', default='CABLE Output')
    parser.add_argument('--receiver-host')
    parser.add_argument('--receiver-user', default='User')
    parser.add_argument('--ssh-key', type=Path, default=Path.home() / '.ssh/autolux_radius_ed25519')
    parser.add_argument(
        '--known-hosts', type=Path, default=Path.home() / '.ssh/known_hosts_autolux'
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Validate configs, devices and SSH without starting a show',
    )
    parser.add_argument('--no-ui', action='store_true')
    parser.add_argument('--ui-port', type=int, default=8050)
    parser.add_argument('--duration', type=float, help='Stop automatically after this many seconds')
    parser.add_argument('--report')


def read_config(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object in {path}')
    return value


def native_specs(args, delay):
    import secrets
    import socket

    root = args.radius_dir.resolve()
    session = secrets.randbits(63) or 1
    configs = [
        read_config(getattr(args, f'{role}_config') or root / f'radius_{role}/radius_{role}.json')
        for role in ('tx', 'rx')
    ]
    tx, rx = configs
    if args.receiver_host:
        tx['peer_address'] = args.receiver_host
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((tx['peer_address'], int(tx['bus_port'])))
        controller_address = route.getsockname()[0]
    rx['peer_address'] = controller_address
    rx['bus_port'] = tx['bus_port']
    tx.setdefault('artnet_address', '127.0.0.1')
    tx['playback_delay_seconds'] = delay
    specs = []
    for role, config in zip(('tx', 'rx'), configs):
        name = f'radius_{role}'
        exe = root / 'cmake-build-release' / name / f'{name}.exe'
        if not exe.is_file():
            raise RuntimeError(f'Missing {exe}. Run build-radius.ps1 first.')
        config['session'] = session
        if config.get('sample_rate') != 44100:
            raise RuntimeError('Production requires matching 44.1 kHz stereo audio.')
        specs.append(NativeSpec(name, exe, config))
    return specs


def running_processes():
    # Fixed script: no user paths or arguments are interpolated into shell code.
    script = "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('radius_tx.exe','radius_rx.exe','loopMIDI.exe') } | Select-Object Name,ProcessId,ExecutablePath | ConvertTo-Json -Compress"
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    items = json.loads(result.stdout) if result.stdout.strip() else []
    return items if isinstance(items, list) else [items]


def check_conflicts(specs, processes):
    names = {spec.executable.name.casefold() for spec in specs}
    conflicts = [p for p in processes if p['Name'].casefold() in names]
    if conflicts:
        detail = ', '.join(f'{p["Name"]} PID {p["ProcessId"]}' for p in conflicts)
        raise RuntimeError(
            f'Native processes already running: {detail}. Stop their owning session first.'
        )


def endpoint_name(device_id, kind):
    import winreg

    match = re.fullmatch(r'\{0\.0\.[01]\.00000000\}\.({[0-9a-fA-F-]+})', device_id)
    if not match:
        raise RuntimeError(f'Invalid Windows audio endpoint: {device_id}')
    key = rf'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\{kind}\{match[1]}\Properties'
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
        return winreg.QueryValueEx(handle, '{a45c254e-df1c-4efd-8020-67d146a850e0},2')[0]


def resolve_devices(midi_prefix, input_prefix, config):
    import pyaudio
    import rtmidi

    audio = pyaudio.PyAudio()
    try:
        host = audio.get_default_host_api_info()['index']
        inputs = [audio.get_device_info_by_index(i) for i in range(audio.get_device_count())]
        inputs = [
            d
            for d in inputs
            if d['hostApi'] == host
            and d['maxInputChannels'] > 0
            and d['name'].casefold().startswith(input_prefix.casefold())
        ]
    finally:
        audio.terminate()
    midi = rtmidi.MidiOut()
    try:
        ports = [
            (i, name)
            for i, name in enumerate(midi.get_ports())
            if name.casefold().startswith(midi_prefix.casefold())
        ]
    finally:
        midi.delete()
    if len(inputs) != 1 or len(ports) != 1:
        raise RuntimeError(
            f'Need one audio input and MIDI port; audio={inputs}, MIDI={ports}. Run autolux list.'
        )
    source = endpoint_name(config['audio_device_id'], 'Capture')
    if not inputs[0]['name'].casefold().startswith(source.casefold()):
        raise RuntimeError(
            f'Capture mismatch: Python uses {inputs[0]["name"]}, native sender uses {source}.'
        )
    return int(inputs[0]['index']), ports[0][0]


async def run(args):
    if os.name != 'nt':
        raise RuntimeError('Production mode currently supports Windows.')
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        raise ValueError('duration must be finite and positive')
    if not 1024 <= args.ui_port < 65535:
        raise ValueError('ui-port must be 1024..65534')
    from cli import main
    from cli.windows_runtime import RoleLock
    from cli.remote_runtime import RemoteRuntime
    from cli.health_monitor import HealthMonitor
    from contextlib import ExitStack

    tx, rx = native_specs(args, main.PLAYBACK_DELAY_SEC)
    with tempfile.TemporaryDirectory(prefix='autolux-check-') as temp:
        for spec in (tx, rx):
            validate_native(spec, temp)
    lock = RoleLock('controller')
    app = None
    app_ran = False
    try:
        processes = running_processes()
        check_conflicts([tx], processes)
        from autolux.lib.section_chain import artifacts_present

        if not artifacts_present():
            raise RuntimeError(
                'The shipped neural model is missing; refusing a degraded production show.'
            )
        if not args.check and not any(p['Name'].casefold() == 'loopmidi.exe' for p in processes):
            exe = Path(os.environ['ProgramFiles(x86)']) / 'Tobias Erichsen/loopMIDI/loopMIDI.exe'
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            subprocess.Popen([str(exe)], startupinfo=startup)
            time.sleep(1)
        input_index, midi_index = resolve_devices(args.midi_port, args.input_device, tx.config)
        run_dir = (
            ROOT / '.live' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        )
        remote = RemoteRuntime(
            rx,
            tx.config['peer_address'],
            args.receiver_user,
            args.ssh_key,
            args.known_hosts,
            run_dir / 'remote',
        )
        remote.check_connection()
        if args.check:
            logging.info('[prod] configs, model, capture, MIDI and SSH checks passed')
            return
        app = main.AutoluxController(
            midi_index,
            input_device_index=input_index,
            enable_ui=not args.no_ui,
            ui_port=args.ui_port,
            report_path=args.report,
            enable_monitoring=True,
            overlay_host='127.0.0.1',
            overlay_port=tx.config['overlay_port'],
            overlay_universe=tx.config['dmx_universe'],
        )
        logging.info('[prod] session logs: %s', run_dir)
        with ExitStack() as stack:
            stack.enter_context(remote)
            native = stack.enter_context(NativeRuntime([tx], run_dir / 'local'))
            monitor = HealthMonitor(run_dir / 'local/radius_tx/health.json', tx.config['session'])
            if args.no_ui:
                from autolux.lib.ui_bridge import SnapshotServer

                server = SnapshotServer(app.event_buffer, args.ui_port + 1)
                server.start()
                stack.callback(server.stop)
            (ROOT / '.live/current.json').write_text(
                json.dumps({'run_dir': str(run_dir), 'pid': os.getpid()})
            )
            started = time.monotonic()

            def check_health():
                native.check_health()
                remote.check_health()
                app.event_buffer.set_system_health(monitor.snapshot())
                if (run_dir / 'stop').exists() or (
                    args.duration and time.monotonic() - started >= args.duration
                ):
                    app.stop()

            main.global_app = app
            app.health_check = check_health
            app_ran = True
            await app.run()
            main.write_report(app, args.report)
    finally:
        if app is not None and not app_ran:
            app._shut_down()
        main.global_app = None
        lock.close()


async def status(args):
    current = ROOT / '.live/current.json'
    if not current.exists():
        print('No Autolux session has started.')
        return
    run_dir = Path(read_config(current)['run_dir'])
    from cli.health_monitor import HealthMonitor

    try:
        print(
            json.dumps(HealthMonitor(run_dir / 'local/radius_tx/health.json').snapshot(), indent=2)
        )
    except (OSError, ValueError) as error:
        print(f'No active health report: {error}')


async def stop(args):
    current = ROOT / '.live/current.json'
    if not current.exists():
        print('No Autolux session to stop.')
        return
    run_dir = Path(read_config(current)['run_dir']).resolve()
    if not run_dir.is_relative_to((ROOT / '.live').resolve()):
        raise RuntimeError('Session directory is outside the runtime directory')
    (run_dir / 'stop').touch()
    print('Stop requested for the Autolux session and its Radius endpoints.')
