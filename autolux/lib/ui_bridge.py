"""The show's only UI surface: the event buffer over HTTP, and the viewer process."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_DASHBOARD_PORT = 8765
DASHBOARD_MDNS_HOSTNAME = 'autolux.local.'
DASHBOARD_BIND_ADDRESS = '0.0.0.0'
DEFAULT_ROUTE_PROBE = ('192.0.2.1', 9)
SNAPSHOT_HOST = '127.0.0.1'
SNAPSHOT_PATH = '/snapshot'
CONTROL_PATH = '/control'
MAX_CONTROL_BYTES = 4096
# A refused body up to this size is read and discarded so the refusal arrives intact.
MAX_DRAINED_BYTES = 16 * MAX_CONTROL_BYTES
DRAIN_TIMEOUT_SEC = 1.0
# The engine takes commands every Starfire frame (25 ms); ten frames means its loop is stalled.
HANDOFF_TIMEOUT_SEC = 0.25
CORS_MAX_AGE_SEC = 600
HEX_COLOUR = re.compile(r'#[0-9a-fA-F]{6}')
VIEWER_MODULE = 'autolux.simulate.visualizer_app'
_REPO_ROOT = Path(__file__).resolve().parents[2]
_KILL_GRACE_SEC = 5.0
_VIEWER_POLL_SEC = 0.2


@contextmanager
def advertise_dashboard_mdns(port: int):
    from zeroconf import IPVersion, NonUniqueNameException, ServiceInfo, Zeroconf

    discovery = None
    try:
        try:
            # UDP connect selects the default route without sending any traffic.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(DEFAULT_ROUTE_PROBE)
                address = route.getsockname()[0]
            discovery = Zeroconf(interfaces=[address], ip_version=IPVersion.V4Only)
            discovery.register_service(
                ServiceInfo(
                    '_http._tcp.local.',
                    'Autolux._http._tcp.local.',
                    parsed_addresses=[address],
                    port=port,
                    server=DASHBOARD_MDNS_HOSTNAME,
                    properties={'path': '/'},
                )
            )
        except (OSError, NonUniqueNameException) as error:
            LOGGER.warning('[dashboard] local hostname unavailable; use the PC IP: %s', error)
        yield
    finally:
        if discovery is not None:
            discovery.close()


def snapshot_port(ui_port: int) -> int:
    return ui_port + 1


PALETTE_POLICY = 'auto'


def parse_control_command(payload) -> dict:
    from autolux.lib.engine.effect_definitions import ShowSettings
    from autolux.lighting.palettes import PALETTE_NAMES

    if not isinstance(payload, dict):
        raise TypeError('a control command must be an object')
    action = payload.get('action')
    if action in ('pause', 'play', 'next_autoloop'):
        return {'action': action}
    if action == 'master':
        level = payload.get('level')
        if type(level) not in (int, float) or not 0.0 <= level <= 1.0:
            raise ValueError('master brightness must be within 0..1')
        return {'action': action, 'level': float(level)}
    if action == 'intensity':
        level = payload.get('level')
        if type(level) not in (int, float) or not 0.0 <= level <= 1.0:
            raise ValueError('intensity must be within 0..1')
        return {'action': action, 'level': float(level)}
    if action == 'settings':
        return {'action': action, 'settings': ShowSettings.from_dict(payload.get('settings'))}
    if action == 'palette':
        name = payload.get('name')
        if name != PALETTE_POLICY and name not in PALETTE_NAMES:
            raise ValueError(
                f'palette must be {PALETTE_POLICY!r} or one of {", ".join(PALETTE_NAMES)}'
            )
        return {'action': action, 'name': name}
    if action in ('intensity_auto', 'fx_auto'):
        if type(payload.get('enabled')) is not bool:
            raise ValueError(f'{action} must be true or false')
        return {'action': action, 'enabled': payload['enabled']}
    if action == 'static':
        return {'action': action, **_static_command(payload)}
    raise ValueError(f'unknown control action {action!r}')


def _static_command(payload: dict) -> dict:
    from autolux.lib.engine.static_mode import PRESETS

    kind = payload.get('kind')
    if kind in ('clear', 'special_fx_clear'):
        return {'kind': kind}
    if kind == 'special_fx':
        fields = ('group', 'colour_slot', 'energy', 'speed', 'pulse_beats')
        return {'kind': kind, **{name: payload.get(name) for name in fields}}
    if kind == 'blackout':
        if type(payload.get('enabled')) is not bool:
            raise ValueError('blackout must be true or false')
        return {'kind': kind, 'enabled': payload['enabled']}
    if kind == 'preset':
        if payload.get('preset') not in PRESETS:
            raise ValueError(f'unknown static preset {payload.get("preset")!r}')
        return {'kind': kind, 'preset': payload['preset']}
    if kind != 'look':
        raise ValueError(f'unknown static command {kind!r}')
    target, colour, dimmer = payload.get('target'), payload.get('colour'), payload.get('dimmer')
    if not isinstance(target, str) or not target:
        raise ValueError('a static look needs a target')
    if colour is not None:
        if not isinstance(colour, str) or not HEX_COLOUR.fullmatch(colour):
            raise ValueError('colour must be #rrggbb')
        colour = tuple(round(int(colour[i : i + 2], 16) / 255, 6) for i in (1, 3, 5))
    if dimmer is not None and (type(dimmer) not in (int, float) or not 0.0 <= dimmer <= 1.0):
        raise ValueError('a static dimmer must be within 0..1')
    if colour is None and dimmer is None:
        raise ValueError('a static look needs a colour or a dimmer')
    look = {
        'kind': kind,
        'target': target,
        'colour': colour,
        'dimmer': None if dimmer is None else float(dimmer),
    }
    if 'uv' in payload:
        if type(payload['uv']) is not bool:
            raise ValueError('uv must be true or false')
        look['uv'] = payload['uv']
    return look


def command_refusal(
    command: dict, intent: str | None, paused: bool | None, mode: str | None
) -> str | None:
    from autolux.lib.engine.event_buffer import STANDBY_INTENT

    if command['action'] != 'next_autoloop':
        return None
    if paused:
        return 'the show is paused'
    if mode == 'static':
        return 'a static overlay is active'
    if intent in (None, STANDBY_INTENT):
        return 'no section is playing yet'
    return None


def _dashboard_origin(origin: str | None, dashboard_port: int | None) -> bool:
    # Any host name: phones reach the dashboard by IP or by the mDNS name.
    if origin is None or dashboard_port is None:
        return False
    return re.fullmatch(rf'http://[^/:\s]+:{dashboard_port}', origin) is not None


def _await_handoff(commands: queue.Queue, timeout: float) -> bool:
    # The engine calls task_done once it has applied each command; it drains them all at once.
    with commands.all_tasks_done:
        return commands.all_tasks_done.wait_for(lambda: commands.unfinished_tasks == 0, timeout)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    # Headers and body go out as two writes; Nagle would hold the body for the client's delayed ACK.
    disable_nagle_algorithm = True

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # A viewer can close its connection during a poll or shutdown.

    def do_GET(self) -> None:
        if self.path == '/metrics':
            from prometheus_client import CONTENT_TYPE_LATEST

            from autolux.lib.system_health import metrics

            self._send(200, CONTENT_TYPE_LATEST, metrics(self.server.event_buffer))
            return
        if self.path != SNAPSHOT_PATH:
            self.send_error(404)
            return
        self._reply(200, self.server.event_buffer.snapshot())

    def do_OPTIONS(self) -> None:
        if self.path != CONTROL_PATH or self.server.commands is None or not self._cross_origin():
            self.send_error(404 if self.path != CONTROL_PATH else 403)
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', str(CORS_MAX_AGE_SEC))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self) -> None:
        commands = self.server.commands
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = -1
        if self.path != CONTROL_PATH or commands is None:
            self._refuse(404, length)
            return
        if 'Origin' in self.headers and not self._cross_origin():
            self._refuse(403, length)
            return
        if self.headers.get_content_type() != 'application/json':
            # Forces a preflight for any cross-site page, so the origin check above cannot be skipped.
            self._refuse(415, length)
            return
        if not 0 < length <= MAX_CONTROL_BYTES:
            self._refuse(413 if length > MAX_CONTROL_BYTES else 400, length)
            return
        try:
            command = parse_control_command(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError) as error:
            self._reply(400, {'error': str(error)})
            return
        refusal = command_refusal(command, *self.server.event_buffer.operator_state())
        if refusal:
            self._reply(409, {'error': refusal})
            return
        try:
            commands.put_nowait(command)
        except queue.Full:
            self._reply(503, {'error': 'the show is busy; try again'})
            return
        timeout = self.server.handoff_timeout
        if timeout is not None and not _await_handoff(commands, timeout):
            self._reply(504, {'error': 'the show has not taken the command yet'})
            return
        self._reply(202, {'accepted': command['action']})

    def _refuse(self, status: int, length: int) -> None:
        # Closing with the body unread resets the socket, and the client can lose the reply.
        if 0 < length <= MAX_DRAINED_BYTES:
            self.connection.settimeout(DRAIN_TIMEOUT_SEC)
            try:
                self.rfile.read(length)
            except OSError:
                pass
            finally:
                self.connection.settimeout(None)
        self.send_error(status)

    def _cross_origin(self) -> bool:
        return _dashboard_origin(self.headers.get('Origin'), self.server.dashboard_port)

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        # Every reply, errors included: without these the page sees a network error, not the status.
        if getattr(self, 'headers', None) is not None and self._cross_origin():
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Vary', 'Origin')

    def _reply(self, status: int, body: dict) -> None:
        self._send(status, 'application/json', json.dumps(body).encode())

    def _send(self, status: int, content_type: str, content: bytes) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args) -> None:
        pass


class _Http(ThreadingHTTPServer):
    daemon_threads = True
    # A second show must fail to bind rather than quietly share the port.
    allow_reuse_address = False


class SnapshotServer:
    def __init__(
        self,
        event_buffer,
        port: int,
        commands: queue.Queue | None = None,
        *,
        host: str = SNAPSHOT_HOST,
        dashboard_port: int | None = None,
        handoff_timeout: float | None = None,
    ):
        self._http = _Http((host, port), _Handler)
        self._http.event_buffer = event_buffer
        self._http.commands = commands
        self._http.dashboard_port = dashboard_port
        self._http.handoff_timeout = handoff_timeout
        self._thread = threading.Thread(
            target=self._http.serve_forever, name='snapshot-server', daemon=True
        )

    @property
    def port(self) -> int:
        return self._http.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()


def viewer_command(ui_port: int, fixture_setup: dict | None = None) -> list:
    command = [sys.executable, '-m', VIEWER_MODULE]
    if fixture_setup is not None:
        command = [
            sys.executable,
            '-m',
            'autolux',
            'starfire',
            '--binary',
            str(fixture_setup['binary']),
            'ui',
            '--rig-store',
            str(fixture_setup['rig_store']),
        ]
        if fixture_setup.get('library') is not None:
            command.extend(['--library', str(fixture_setup['library'])])
    return command + ['--port', str(ui_port), '--snapshot-port', str(snapshot_port(ui_port))]


def _spawn(command: list) -> subprocess.Popen:
    detach = (
        {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == 'nt'
        else {'start_new_session': True}
    )
    return subprocess.Popen(command, cwd=str(_REPO_ROOT), **detach)


def kill_tree(process: subprocess.Popen) -> None:
    """The venv's python.exe re-execs the real interpreter, so the pid is a parent."""
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError as error:
        LOGGER.warning(f'[ui] could not signal the viewer ({error!r})')
    try:
        process.wait(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        process.kill()


class UiBridge:
    def __init__(self, server: SnapshotServer, viewer: subprocess.Popen, ui_port: int):
        self._server = server
        self._viewer = viewer
        self._ui_port = ui_port
        self._stopping = False
        self._watcher = threading.Thread(target=self._watch, name='viewer-watch', daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        code = self._viewer.wait()
        if not self._stopping:
            LOGGER.warning(
                f'[ui] the viewer process exited ({code}) — the '
                f'show is unaffected; relaunch it with '
                f'`python -m {VIEWER_MODULE} --port {self._ui_port}`'
            )

    def wait(self) -> None:
        while self._viewer.poll() is None:
            time.sleep(_VIEWER_POLL_SEC)

    def stop(self) -> None:
        self._stopping = True
        kill_tree(self._viewer)
        self._server.stop()


def start(
    event_buffer,
    ui_port: int,
    *,
    fixture_setup: dict | None = None,
    commands: queue.Queue | None = None,
) -> UiBridge | None:
    try:
        # Bound like the dashboard so its page can POST controls here directly.
        server = SnapshotServer(
            event_buffer,
            snapshot_port(ui_port),
            commands,
            host=DASHBOARD_BIND_ADDRESS,
            dashboard_port=ui_port,
            handoff_timeout=HANDOFF_TIMEOUT_SEC,
        )
        server.start()
    except OSError as error:
        LOGGER.warning(
            f'[ui] no snapshot endpoint on port '
            f'{snapshot_port(ui_port)} ({error!r}) — the show runs '
            f'without a viewer'
        )
        return None
    try:
        viewer = _spawn(viewer_command(ui_port, fixture_setup))
    except OSError as error:
        LOGGER.warning(
            f'[ui] the viewer process did not start ({error!r}) — the show runs without it'
        )
        server.stop()
        return None
    LOGGER.info(
        f'[ui] dashboard → http://{DASHBOARD_MDNS_HOSTNAME.rstrip(".")}:{ui_port} '
        f'(pid {viewer.pid}, reading :{server.port}{SNAPSHOT_PATH})'
    )
    return UiBridge(server, viewer, ui_port)
