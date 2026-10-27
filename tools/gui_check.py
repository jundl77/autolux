"""Drive the Dash control panel in headless Chrome against a stub show that echoes commands.

    uv run --no-sync python tools/gui_check.py [--only NAME] [--shots DIR]

The stub is the real SnapshotServer over a fake engine: each command it takes changes the
snapshot ECHO_SECONDS later, as the engine does. tools/gui_check.mjs clicks every control
over the Chrome DevTools protocol and checks one bridge command per operator action, no
command from polls, and no flip-back. Needs node (>= 22, for its WebSocket) and Chrome.
"""

import argparse
import copy
import logging
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import jsonify, request
from werkzeug.serving import make_server

from autolux.lib.engine.effect_definitions import ShowSettings
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.engine.static_mode import ALL_FIXTURES
from autolux.lib.ui_bridge import SnapshotServer
from autolux.lighting.fixture_setup import FixtureStore
from autolux.lighting.palettes import PALETTES
from autolux.simulate.visualizer_app import SnapshotPoller, build_app

ECHO_SECONDS = 1.0
CHROME = os.environ.get('CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
GROUPS = {'pars': ['colour', 'dimmer'], 'spots': ['pan', 'tilt']}
FX_ROWS = {ALL_FIXTURES: ['strobe_chase'], 'pars': ['colour_chase', 'strobe_chase'], 'spots': []}
# group -> (cooldown total, seconds since it last fired at start, last row)
COOLDOWNS = {'pars': (8.0, 3.0, 'colour_chase'), 'spots': (6.0, 60.0, 'pan_sweep')}


class StubShow:
    """The engine's side of the bridge: fixed data, a monotonic clock for cooldowns."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._base = EventBuffer().snapshot()
        self.reset()

    def reset(self) -> None:
        now = self._clock()
        with self._lock:
            self.commands = []
            self.blank = False
            self.down = False
            self.state = {
                'paused': False,
                'master_brightness': 0.8,
                'settings': ShowSettings().to_dict(),
                'intent': 'drop',
                'palette': {'name': PALETTES[0].name, 'pinned': False},
                'special': None,
                'special_groups': copy.deepcopy(GROUPS),
                'group_roles': {},
                'strobe_cooldown_seconds': 12.0,
                'bpm': 124.0,
                'decoder': {'classes': ['intro', 'build', 'drop'], 'posterior': [0.1, 0.3, 0.6]},
                'static': {
                    'targets': [ALL_FIXTURES, *GROUPS],
                    'blacklight': True,
                    'blackout': False,
                    'special_fx': None,
                    'fx_rows': copy.deepcopy(FX_ROWS),
                    'looks': {},
                },
            }
            self.cooldowns = {
                group: {'total': total, 'fired': now - ago, 'last': last}
                for group, (total, ago, last) in COOLDOWNS.items()
            }

    def snapshot(self) -> dict:
        with self._lock:
            if self.blank:
                return dict(self._base)
            state = copy.deepcopy(self.state)
            now = self._clock()
            rows = []
            for group, row in self.cooldowns.items():
                remaining = max(0.0, row['total'] - (now - row['fired']))
                rows.append(
                    {
                        'group': group,
                        'label': group,
                        'last': row['last'],
                        'cooldown_seconds': row['total'],
                        'remaining_seconds': round(remaining, 2),
                        'ready': remaining <= 0,
                    }
                )
        static = state['static']
        state['mode'] = (
            'static' if static['looks'] or static['special_fx'] or static['blackout'] else 'auto'
        )
        return {**self._base, **state, 'special_cooldowns': rows}

    def operator_state(self):
        with self._lock:
            static = self.state['static']
            mode = 'static' if static['looks'] or static['special_fx'] else 'auto'
            return self.state['intent'], self.state['paused'], mode

    def take(self, command: dict) -> None:
        with self._lock:
            self.commands.append({'t': time.time(), 'cmd': command})
        threading.Timer(ECHO_SECONDS, self._apply, args=(command,)).start()

    def _apply(self, c: dict) -> None:
        with self._lock:
            s, a = self.state, c['action']
            settings, static = s['settings'], s['static']
            if a in ('pause', 'play'):
                s['paused'] = a == 'pause'
            elif a == 'master':
                s['master_brightness'] = c['level']
            elif a == 'intensity':
                settings.update(intensity=c['level'], intensity_auto=False)
            elif a in ('intensity_auto', 'fx_auto'):
                settings[a] = c['enabled']
            elif a == 'settings':
                settings.update(c['settings'])
            elif a == 'palette':
                s['palette'] = {'name': c['name'], 'pinned': c['name'] != 'auto'}
            elif a == 'static_fx':
                static['special_fx'] = {'name': c['row'], 'group': c['target']}
            elif a == 'static':
                self._apply_static(static, c)

    @staticmethod
    def _apply_static(static: dict, c: dict) -> None:
        kind = c['kind']
        if kind == 'clear':
            static.update(looks={}, special_fx=None, blackout=False)
        elif kind == 'special_fx_clear':
            static['special_fx'] = None
        elif kind == 'preset':
            dark = c['preset'] in ('off', 'blackout')
            look = {'colour': None if dark else c['preset'], 'dimmer': 0.0 if dark else None}
            static['looks'] = {ALL_FIXTURES: {**look, 'uv': c['preset'] == 'uv'}}
        elif kind == 'look':
            colour = c.get('colour')
            if colour is not None:
                colour = '#' + ''.join(f'{round(v * 255):02x}' for v in colour)
            static['looks'][c['target']] = {
                'colour': colour,
                'dimmer': c.get('dimmer'),
                'uv': c.get('uv', False),
            }

    def control(self, payload: dict) -> None:
        """Harness-only changes the engine would make on its own."""
        now = self._clock()
        with self._lock:
            if 'blank' in payload:
                self.blank = bool(payload['blank'])
            if 'fire' in payload:
                row = self.cooldowns[payload['fire']]
                row.update(fired=now, last=payload.get('last', row['last']))
            if 'lengthen' in payload:
                group, total = payload['lengthen']
                self.cooldowns[group]['total'] = float(total)
            if 'special' in payload:
                self.state['special'] = payload['special']


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def engine(show: StubShow, commands: queue.Queue) -> None:
    while True:
        command = commands.get()
        commands.task_done()
        show.take(command)


def add_harness_routes(app, show: StubShow) -> None:
    @app.server.route('/_harness/commands')
    def harness_commands():
        with show._lock:
            return jsonify(list(show.commands))

    @app.server.route('/_harness/state')
    def harness_state():
        return jsonify(show.snapshot())

    @app.server.route('/_harness/reset', methods=['POST'])
    def harness_reset():
        show.reset()
        return jsonify({})

    @app.server.route('/_harness/control', methods=['POST'])
    def harness_control():
        show.control(request.get_json())
        return jsonify({})


def run_idle() -> None:
    # Chrome and node inherit the class, so a live show on this machine keeps the CPU.
    if os.name == 'nt':
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x40)  # IDLE_PRIORITY_CLASS


def main(argv=None) -> int:
    run_idle()
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--only', help='run only the checks whose name contains this')
    parser.add_argument('--shots', help='directory for one PNG per tab (none when omitted)')
    args = parser.parse_args(argv)
    node = shutil.which('node')
    if node is None or not Path(CHROME).exists():
        print(f'gui_check needs node and Chrome ({CHROME})')
        return 2
    dash_port, bridge_port = free_port(), free_port()
    show = StubShow()
    commands = queue.Queue(maxsize=8)
    bridge = SnapshotServer(
        show, bridge_port, commands, dashboard_port=dash_port, handoff_timeout=0.5
    )
    bridge.start()
    threading.Thread(target=engine, args=(show, commands), daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='autolux-gui-', ignore_cleanup_errors=True) as scratch:
        rig = Path(scratch) / 'room.mvr'
        rig.write_bytes((ROOT / 'rig/room.mvr').read_bytes())
        store = FixtureStore(rig, ROOT / 'rig/library', lambda _: None)
        app = build_app(
            SnapshotPoller(host='127.0.0.1', port=bridge_port),
            fixture_store=store,
            direct_control=True,
        )
        add_harness_routes(app, show)
        server = make_server('127.0.0.1', dash_port, app.server, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {
            **os.environ,
            'GUI_URL': f'http://127.0.0.1:{dash_port}/',
            'GUI_BRIDGE_PORT': str(bridge_port),
            'GUI_CHROME': CHROME,
            'GUI_PROFILE': str(Path(scratch) / 'chrome'),
            'GUI_ONLY': args.only or '',
            'GUI_SHOTS': args.shots or '',
        }
        try:
            result = subprocess.run([node, str(ROOT / 'tools/gui_check.mjs')], check=False, env=env)
        finally:
            server.shutdown()
            bridge.stop()
            # Chrome can hold its profile for a moment after exit.
            time.sleep(1)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
