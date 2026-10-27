"""Production topology: controller here; audio/DMX receivers on the output PC."""

import asyncio
import json
import logging
import math
import multiprocessing
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from autolux.lib.audio_config import SAMPLE_RATE
from autolux.lib.main import PLAYBACK_DELAY_SEC
from autolux.lib.ui_bridge import DEFAULT_DASHBOARD_PORT
from cli.remote_runtime import ReceiverSpec
from cli.starfire_assets import static_rig
from cli.starfire_runtime import (
    StarfireRuntime,
    fixture_setup,
    selected_rig,
    validate_starfire,
)
from cli.starfire_runtime import (
    add_arguments as add_starfire_arguments,
)
from proto.starfire import starfire_pb2 as wire

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
READINESS_TIMEOUT_SECONDS = 45
READINESS_WORKER_EXIT_TIMEOUT_SECONDS = 2
DEFAULT_RECEIVER_BUFFER_MS = 500


def add_arguments(parser):
    parser.add_argument(
        '--verbose', action='store_true', help='Include beat, note and protocol diagnostics'
    )
    parser.add_argument(
        '--receiver-binary', type=Path, default=ROOT / 'target/release/radius_rx.exe'
    )
    parser.add_argument('--tx-config', type=Path, default=ROOT / 'radius/transmitter.json')
    parser.add_argument('--rx-config', type=Path, default=ROOT / 'radius/receiver.json')
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
        raise ValueError(f'Expected a JSON object in {path}')  # noqa: TRY004
    return value


def transport_settings(args, delay):
    import secrets
    import socket

    run_id = secrets.randbits(64) or 1
    configs = [read_config(getattr(args, f'{role}_config')) for role in ('tx', 'rx')]
    tx, rx = configs
    if args.receiver_host:
        tx['peer_address'] = args.receiver_host
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((tx['peer_address'], int(tx['bus_port'])))
        controller_address = route.getsockname()[0]
    rx['peer_address'] = controller_address
    rx['bus_port'] = tx['bus_port']
    tx['playback_delay_seconds'] = delay
    tx['receiver'] = {
        'audio_enabled': rx.get('audio_enabled', True),
        'lighting_enabled': rx.get('dmx_enabled', True),
        'jitter_buffer_ms': rx.get('jitter_buffer_ms', DEFAULT_RECEIVER_BUFFER_MS),
    }
    for config in configs:
        config['run_id'] = run_id
        if config.get('sample_rate') != SAMPLE_RATE:
            raise RuntimeError('Production requires matching 44.1 kHz stereo audio.')
    executable = args.receiver_binary.resolve()
    if not executable.is_file():
        raise RuntimeError(f'Missing {executable}. Run build-radius.ps1 first.')
    return tx, ReceiverSpec(executable, rx)


def radius_output(config):
    receiver = config['receiver']
    output = wire.Login.RadiusOutput(
        receiver_address=f"{config['peer_address']}:{config['bus_port']}",
        capture_device_id=config['audio_device_id'],
        playback_delay_seconds=config['playback_delay_seconds'],
        receiver_buffer_ms=receiver['jitter_buffer_ms'],
        universe=config['dmx_universe'],
        receiver_audio_enabled=receiver['audio_enabled'],
        receiver_lighting_enabled=receiver['lighting_enabled'],
    )
    if config.get('network_core', -1) >= 0:
        output.network_core = config['network_core']
    return output


def running_processes():
    # Fixed script: no user paths or arguments are interpolated into shell code.
    script = "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('radius_rx.exe','starfire.exe') } | Select-Object Name,ProcessId,ExecutablePath | ConvertTo-Json -Compress"
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


def check_conflicts(executables, processes):
    names = {executable.name.casefold() for executable in executables}
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
        validation = subprocess.run(  # noqa: ASYNC221
            [sys.executable, '-X', 'utf8', str(validation_script)], cwd=ROOT, check=False
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
        LOGGER.error('[prod] readiness failed: %s', error)
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
    from contextlib import ExitStack

    from cli import main
    from cli.health_monitor import HealthLogger, HealthMonitor
    from cli.remote_runtime import RemoteRuntime
    from cli.windows_runtime import RoleLock

    tx, rx = transport_settings(args, PLAYBACK_DELAY_SEC)
    lock = RoleLock('controller')
    app = None
    app_ran = False
    try:
        processes = running_processes()
        check_conflicts([args.starfire_binary], processes)
        from autolux.lib.section_chain import artifacts_present

        if not artifacts_present():
            raise RuntimeError(
                'The shipped neural model is missing; refusing a degraded production show.'
            )
        input_index = resolve_devices(args.input_device, tx)
        rig = selected_rig(args.rig, args.library)
        validate_starfire(
            args.starfire_binary,
            rig,
            args.autoloops,
            expected_universe=tx['dmx_universe'],
        )
        run_dir = (
            ROOT / '.live' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        )
        remote = RemoteRuntime(
            rx,
            tx['peer_address'],
            args.receiver_user,
            args.ssh_key,
            args.known_hosts,
            run_dir / 'remote',
        )
        remote.check_connection()
        if checking:
            remote.prepare()
            LOGGER.info('[prod] Radius, Starfire, fixtures, model, capture and SSH checks passed')
            return
        LOGGER.info('[prod] session logs: %s', run_dir)
        renderer = StarfireRuntime(
            args.starfire_binary,
            rig,
            args.autoloops,
            None,
            run_dir / 'local/starfire',
            run_id=tx['run_id'],
            radius=radius_output(tx),
            render_core=tx.get('render_core'),
        )

        def send_lighting_command(command):
            if command['kind'] == 'stop' and renderer.process is None:
                return
            renderer.send_command(command)

        from autolux.lib.clients.starfire_client import StarfireClient
        from autolux.lighting.autoloop_catalogue import discover_autoloops, read_autoloop_meta

        LOGGER.info('[prod] preparing analysis models before starting audio')
        app = main.AutoluxController(
            lighting_client=StarfireClient(send_lighting_command, groups=lambda: renderer.groups),
            input_device_index=input_index,
            enable_ui=not args.no_ui,
            ui_port=args.ui_port,
            report_path=args.report,
            enable_monitoring=True,
            fixture_setup=fixture_setup(args),
            autoloops=discover_autoloops(args.autoloops),
            autoloop_meta=read_autoloop_meta(args.autoloops),
            selection_seed=tx['run_id'],
            static_rig=static_rig(rig),
        )
        with ExitStack() as stack:
            stack.enter_context(remote)
            stack.enter_context(renderer)
            monitor = HealthMonitor(run_dir / 'local/starfire/transport-health.json', tx['run_id'])
            health_log = HealthLogger()
            if args.no_ui:
                from autolux.lib.ui_bridge import SnapshotServer

                server = SnapshotServer(app.event_buffer, args.ui_port + 1, app.control.commands)
                server.start()
                stack.callback(server.stop)
            (ROOT / '.live/current.json').write_text(
                json.dumps({'run_dir': str(run_dir), 'pid': os.getpid()})
            )
            started = time.monotonic()

            def check_health():
                remote.check_health()
                renderer.check_health()
                state = monitor.snapshot()
                state['starfire'] = renderer.snapshot()
                app.light_engine.note_capture_discontinuities(
                    (state.get('tx') or {}).get('capture_discontinuities_total')
                )
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
            json.dumps(
                HealthMonitor(run_dir / 'local/starfire/transport-health.json').snapshot(), indent=2
            )
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
