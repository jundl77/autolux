"""The show's only UI surface: the event buffer over HTTP, and the viewer process."""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import contextmanager
import socket

DEFAULT_DASHBOARD_PORT = 8765
DASHBOARD_MDNS_HOSTNAME = 'autolux.local.'
DASHBOARD_BIND_ADDRESS = '0.0.0.0'
DEFAULT_ROUTE_PROBE = ('192.0.2.1', 9)
SNAPSHOT_HOST = '127.0.0.1'
SNAPSHOT_PATH = '/snapshot'
CONTROL_PATH = '/control'
MAX_CONTROL_BYTES = 4096
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
            logging.warning('[dashboard] local hostname unavailable; use the PC IP: %s', error)
        yield
    finally:
        if discovery is not None:
            discovery.close()


def snapshot_port(ui_port: int) -> int:
    return ui_port + 1


def parse_control_command(payload) -> dict:
    """Validate here so the operator sees a rejection instead of a silent drop."""
    from autolux.lib.engine.effect_definitions import ShowSettings

    if not isinstance(payload, dict):
        raise TypeError('a control command must be an object')
    action = payload.get('action')
    if action in ('pause', 'play'):
        return {'action': action}
    if action == 'master':
        level = payload.get('level')
        if type(level) not in (int, float) or not 0.0 <= level <= 1.0:
            raise ValueError('master brightness must be within 0..1')
        return {'action': action, 'level': float(level)}
    if action == 'settings':
        return {'action': action, 'settings': ShowSettings.from_dict(payload.get('settings'))}
    raise ValueError(f'unknown control action {action!r}')


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # A viewer can close its connection during a poll or shutdown.

    def do_GET(self) -> None:
        if self.path == '/metrics':
            from prometheus_client import CONTENT_TYPE_LATEST
            from autolux.lib.system_health import metrics

            body = metrics(self.server.event_buffer)
            self.send_response(200)
            self.send_header('Content-Type', CONTENT_TYPE_LATEST)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != SNAPSHOT_PATH:
            self.send_error(404)
            return
        body = json.dumps(self.server.event_buffer.snapshot()).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        commands = self.server.commands
        if self.path != CONTROL_PATH or commands is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_CONTROL_BYTES:
            self.send_error(413 if length > MAX_CONTROL_BYTES else 400)
            return
        try:
            command = parse_control_command(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError) as error:
            self._reply(400, {'error': str(error)})
            return
        try:
            # The audio loop owns the engine, so this thread only hands the command over.
            commands.put_nowait(command)
        except queue.Full:
            self._reply(503, {'error': 'the show is busy; try again'})
            return
        self._reply(202, {'accepted': command['action']})

    def _reply(self, status: int, body: dict) -> None:
        content = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
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
    def __init__(self, event_buffer, port: int, commands: queue.Queue | None = None):
        self._http = _Http((SNAPSHOT_HOST, port), _Handler)
        self._http.event_buffer = event_buffer
        self._http.commands = commands
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
    command = [
        sys.executable,
        '-m',
        VIEWER_MODULE,
    ]
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
        if fixture_setup.get('profiles') is not None:
            command.extend(['--profiles', str(fixture_setup['profiles'])])
    return command + [
        '--port',
        str(ui_port),
        '--snapshot-port',
        str(snapshot_port(ui_port)),
    ]


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
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError as error:
        logging.warning(f'[ui] could not signal the viewer ({error!r})')
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

    @property
    def viewer(self) -> subprocess.Popen:
        return self._viewer

    def _watch(self) -> None:
        code = self._viewer.wait()
        if not self._stopping:
            logging.warning(
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
        server = SnapshotServer(event_buffer, snapshot_port(ui_port), commands)
        server.start()
    except OSError as error:
        logging.warning(
            f'[ui] no snapshot endpoint on port '
            f'{snapshot_port(ui_port)} ({error!r}) — the show runs '
            f'without a viewer'
        )
        return None
    try:
        viewer = _spawn(viewer_command(ui_port, fixture_setup))
    except OSError as error:
        logging.warning(
            f'[ui] the viewer process did not start ({error!r}) — the show runs without it'
        )
        server.stop()
        return None
    logging.info(
        f'[ui] dashboard → http://{DASHBOARD_MDNS_HOSTNAME.rstrip(".")}:{ui_port} '
        f'(pid {viewer.pid}, reading :{server.port}{SNAPSHOT_PATH})'
    )
    return UiBridge(server, viewer, ui_port)
