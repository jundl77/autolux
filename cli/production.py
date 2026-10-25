"""Production topology: controller here; audio/DMX receivers on the output PC."""

import argparse
import asyncio
from datetime import datetime
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from cli.native_runtime import NativeRuntime, NativeSpec, validate_native
from cli.starfire_runtime import (
    StarfireRuntime,
    add_arguments as add_starfire_arguments,
    selected_rig,
    validate_starfire,
    fixture_setup,
)
from autolux.lib.ui_bridge import DEFAULT_DASHBOARD_PORT

ROOT = Path(__file__).resolve().parents[1]
READINESS_TIMEOUT_SECONDS = 45
READINESS_WORKER_EXIT_TIMEOUT_SECONDS = 2


def add_arguments(parser):
    parser.add_argument(
        '--verbose', action='store_true', help='Include beat, note and protocol diagnostics'
    )
    parser.add_argument('--radius-dir', type=Path, default=ROOT / 'radius')
    parser.add_argument('--tx-config', type=Path)
    parser.add_argument('--rx-config', type=Path)
    add_starfire_arguments(parser)
    parser.add_argument('--input-device', default='CABLE Output')
    parser.add_argument('--receiver-host')
    parser.add_argument('--receiver-user', default='User')
    parser.add_argument('--ssh-key', type=Path, default=Path.home() / '.ssh/autolux_radius_ed25519')
    parser.add_argument(
        '--known-hosts', type=Path, default=Path.home() / '.ssh/known_hosts_autolux'
    )
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument(
        '--check',
        action='store_true',
        help='Check devices, models, binaries and SSH in under 60 seconds without starting a show',
    )
    checks.add_argument(
        '--check-full',
        action='store_true',
        help='Run all tests, builds, backtest, simulation and readiness checks without starting a show',
    )
    parser.add_argument('--no-ui', action='store_true')
    parser.add_argument('--ui-port', type=int, default=DEFAULT_DASHBOARD_PORT)
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
    script = "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('radius_tx.exe','radius_rx.exe','starfire.exe') } | Select-Object Name,ProcessId,ExecutablePath | ConvertTo-Json -Compress"
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


def resolve_devices(input_prefix, config):
    import pyaudio

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
    if len(inputs) != 1:
        raise RuntimeError(f'Need one matching audio input; found {inputs}. Run autolux list.')
    source = endpoint_name(config['audio_device_id'], 'Capture')
    if not inputs[0]['name'].casefold().startswith(source.casefold()):
        raise RuntimeError(
            f'Capture mismatch: Python uses {inputs[0]["name"]}, native sender uses {source}.'
        )
    return int(inputs[0]['index'])


async def run(args):
    if os.name != 'nt':
        raise RuntimeError('Production mode currently supports Windows.')
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        raise ValueError('duration must be finite and positive')
    if not 1024 <= args.ui_port < 65535:
        raise ValueError('ui-port must be 1024..65534')
    if args.check_full:
        validation_script = ROOT / 'tools/validate.py'
        if not validation_script.is_file():
            raise RuntimeError(
                'Full validation requires the source checkout and development dependencies.'
            )
        validation = subprocess.run(
            [sys.executable, '-X', 'utf8', str(validation_script)], cwd=ROOT
        )
        if validation.returncode:
            raise RuntimeError('Full validation failed; see .live/validation for results.')
    if args.check or args.check_full:
        run_readiness_check(args)
        return
    await _run_session(args)


def _readiness_worker(args, owned):
    from autolux.lib.logging_config import configure_logging

    owned.wait()
    configure_logging(verbose=args.verbose)
    try:
        asyncio.run(_run_session(args, checking=True))
    except (RuntimeError, OSError, ValueError) as error:
        logging.error('[prod] readiness failed: %s', error)
        raise SystemExit(1) from error


def run_readiness_check(args):
    from cli.windows_runtime import Job

    context = multiprocessing.get_context('spawn')
    owned = context.Event()
    worker = context.Process(target=_readiness_worker, args=(args, owned))
    job = Job()
    try:
        worker.start()
        job.add(worker.pid)
        owned.set()
        worker.join(READINESS_TIMEOUT_SECONDS)
        if worker.is_alive():
            raise RuntimeError(f'Readiness check timed out after {READINESS_TIMEOUT_SECONDS}s.')
        if worker.exitcode:
            raise RuntimeError('Readiness check failed; see the reported problem above.')
    finally:
        job.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join(READINESS_WORKER_EXIT_TIMEOUT_SECONDS)
            worker.close()


async def _run_session(args, *, checking=False):
    from cli import main
    from cli.windows_runtime import RoleLock
    from cli.remote_runtime import RemoteRuntime
    from cli.health_monitor import HealthMonitor, HealthLogger
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
        renderer_spec = NativeSpec('starfire', args.starfire_binary, {})
        check_conflicts([tx, renderer_spec], processes)
        from autolux.lib.section_chain import artifacts_present

        if not artifacts_present():
            raise RuntimeError(
                'The shipped neural model is missing; refusing a degraded production show.'
            )
        input_index = resolve_devices(args.input_device, tx.config)
        rig = selected_rig(args.rig)
        validate_starfire(
            args.starfire_binary,
            rig,
            args.sequences,
            expected_universe=tx.config['dmx_universe'],
        )
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
        if checking:
            logging.info('[prod] Radius, Starfire, fixtures, model, capture and SSH checks passed')
            return
        logging.info('[prod] session logs: %s', run_dir)
        renderer = StarfireRuntime(
            args.starfire_binary,
            rig,
            args.sequences,
            f"{tx.config['artnet_address']}:{tx.config.get('artnet_port', 6454)}",
            run_dir / 'local/starfire',
        )

        def send_lighting_command(command):
            if command['kind'] == 'stop' and renderer.process is None:
                return
            renderer.send_command(command)

        from autolux.lib.clients.starfire_client import StarfireClient

        logging.getLogger(__name__).info('[prod] preparing analysis models before starting audio')
        app = main.AutoluxController(
            lighting_client=StarfireClient(send_lighting_command),
            input_device_index=input_index,
            enable_ui=not args.no_ui,
            ui_port=args.ui_port,
            report_path=args.report,
            enable_monitoring=True,
            fixture_setup=fixture_setup(args),
        )
        with ExitStack() as stack:
            stack.enter_context(remote)
            native = stack.enter_context(NativeRuntime([tx], run_dir / 'local'))
            stack.enter_context(renderer)
            monitor = HealthMonitor(run_dir / 'local/radius_tx/health.json', tx.config['session'])
            health_log = HealthLogger()
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
                renderer.check_health()
                state = monitor.snapshot()
                state['starfire'] = renderer.snapshot()
                app.event_buffer.set_system_health(state)
                health_log.update(
                    state, fault=getattr(getattr(app, 'drift_watchdog', None), 'fault', None)
                )
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
