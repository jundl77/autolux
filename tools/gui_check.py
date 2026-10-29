"""Drive the Dash control panel in headless Chrome against a stub show that echoes commands."""

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
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import jsonify, request
from werkzeug.serving import make_server

from autolux.lib.engine.effect_definitions import ShowSettings, intensity_level
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.engine.static_mode import ALL_FIXTURES
from autolux.lib.ui_bridge import SnapshotServer
from autolux.lighting import mvr
from autolux.lighting.fixture_setup import FixtureStore, rig_revision
from autolux.lighting.palettes import PALETTES
from autolux.simulate.visualizer_app import SnapshotPoller, build_app
from cli.starfire_assets import rig_bytes, static_rig

ECHO_SECONDS = 1.0
CHROME = os.environ.get('CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
GROUPS = {'pars': ['colour', 'dimmer'], 'spots': ['pan', 'tilt']}
FX_ROWS = {ALL_FIXTURES: ['strobe_chase'], 'pars': ['colour_chase', 'strobe_chase'], 'spots': []}
ROOM = ROOT / 'autolux/tests/data/room.mvr'
SCENE_XML = 'GeneralSceneDescription.xml'
IDLE_PRIORITY_CLASS = 0x40
COOLDOWNS = {'pars': (8.0, 3.0, 'colour_chase'), 'spots': (6.0, 60.0, 'pan_sweep')}


def running_rig(path: Path) -> tuple[str, dict]:
    return rig_revision(path.read_bytes()), static_rig(path).published()


def _autoloop(intensity: float) -> dict:
    level = intensity_level(intensity)
    bank, loop = ('breakdown', 'afterglow') if level == 'calm' else ('drop', 'chase')
    selection = {'intent': 'drop', 'look': bank, 'level': level}
    return {'name': f'{bank}/{loop}', 'type': 'AUTOLOOP', 't': 0.0, 'selection': selection}


class StubShow:
    def __init__(self, rig_path: Path, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._base = EventBuffer().snapshot()
        self.rig_path = Path(rig_path)
        self._due = queue.Queue()
        threading.Thread(target=self._apply_in_order, daemon=True).start()
        self.reset()

    def reset(self) -> None:
        now = self._clock()
        with self._lock:
            self.commands = []
            self.blank = False
            self.down = False
            self.refuse_paused = False
            revision, rig = running_rig(self.rig_path)
            self.state = {
                'paused': False,
                'master_brightness': 0.8,
                'settings': ShowSettings().to_dict(),
                'intent': 'drop',
                'palette': {'name': PALETTES[0].name, 'pinned': False},
                'special': None,
                'special_groups': copy.deepcopy(GROUPS),
                'group_roles': {},
                'bpm': 124.0,
                'decoder': {'classes': ['intro', 'build', 'drop'], 'posterior': [0.1, 0.3, 0.6]},
                'highlight': None,
                'current_effect': _autoloop(ShowSettings().intensity),
                'rig': rig,
                'rig_revision': revision,
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
            return (
                self.state['intent'],
                self.state['paused'] or self.refuse_paused,
                mode,
                copy.deepcopy(static),
                self.state['rig'],
            )

    def take(self, command: dict) -> None:
        with self._lock:
            self.commands.append({'t': time.time(), 'cmd': command})
        self._due.put((time.monotonic() + ECHO_SECONDS, command))

    def _apply_in_order(self) -> None:
        while True:
            due, command = self._due.get()
            time.sleep(max(0.0, due - time.monotonic()))
            self._apply(command)

    def _apply(self, c: dict) -> None:
        with self._lock:
            s, a = self.state, c['action']
            settings, static = s['settings'], s['static']
            if a in ('pause', 'play'):
                s['paused'] = a == 'pause'
                if a == 'pause':
                    s['highlight'] = None
            elif a == 'master':
                s['master_brightness'] = c['level']
            elif a == 'intensity':
                settings.update(intensity=c['level'], intensity_auto=False)
                s['current_effect'] = _autoloop(c['level'])
            elif a in ('intensity_auto', 'fx_auto'):
                settings[a] = c['enabled']
            elif a == 'settings':
                settings.update(c['settings'])
            elif a == 'palette':
                s['palette'] = {'name': c['name'], 'pinned': c['name'] != 'auto'}
            elif a == 'static':
                self._apply_static(static, c)
                if c['kind'] == 'clear':
                    s['highlight'] = None
            elif a == 'highlight':
                s['highlight'] = c['target'] and {
                    key: c[key] for key in ('target', 'colour', 'sweep', 'aim')
                }
            elif a == 'restart_show':
                s['rig_revision'], s['rig'] = running_rig(self.rig_path)
                s['session'] = uuid.uuid4().hex
                s['highlight'] = None

    @staticmethod
    def _apply_static(static: dict, c: dict) -> None:
        kind = c['kind']
        if kind == 'clear':
            static.update(looks={}, special_fx=None, blackout=False)
        elif kind == 'special_fx':
            static['special_fx'] = {'name': c['row'], 'group': c['target']}
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
            if 'shed' in payload:
                self.state['shed'] = payload['shed']
            if 'special' in payload:
                self.state['special'] = payload['special']
            if 'paused' in payload:
                self.state['paused'] = bool(payload['paused'])
                if self.state['paused']:
                    self.state['highlight'] = None
            if 'refuse_paused' in payload:
                self.refuse_paused = bool(payload['refuse_paused'])


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def engine(show: StubShow, commands: queue.Queue) -> None:
    while True:
        command = commands.get()
        commands.task_done()
        show.take(command)


class RigFiles:
    def __init__(self, scratch: Path):
        self.store = scratch / 'store.mvr'
        self.repo = scratch / 'repo/rig/room.mvr'
        self.repo_pb = self.repo.with_suffix('.rig.pb')
        self.repo.parent.mkdir(parents=True)
        self.restore()

    def restore(self) -> None:
        for target in (self.store, self.repo):
            target.write_bytes(ROOM.read_bytes())
        self.repo_pb.write_bytes(ROOM.with_suffix('.rig.pb').read_bytes())

    def bump(self) -> None:
        staged = self.store.with_suffix('.bump')
        with zipfile.ZipFile(self.store) as source, zipfile.ZipFile(staged, 'w') as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.comment = b'bumped by gui_check'
        staged.replace(self.store)

    @staticmethod
    def _entries(path: Path) -> dict:
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name).decode('utf-8')
                if name == SCENE_XML
                else rig_revision(archive.read(name))
                for name in archive.namelist()
            }

    def describe(self) -> dict:
        store = self.store.read_bytes()
        fields = (
            'fixture_uuid',
            'name',
            'gdtf_spec',
            'gdtf_mode',
            'universe',
            'address',
            'class',
            'layer',
        )
        fixtures = [
            {
                **{key: fixture.get(key) for key in fields},
                'matrix': fixture['matrix'],
                'pan': fixture.get('pan'),
                'tilt': fixture.get('tilt'),
            }
            for fixture in mvr.patched_fixtures(mvr.read(store))
        ]
        return {
            'revision': rig_revision(store),
            'store': self._entries(self.store),
            'repo': self._entries(self.repo),
            'repo_same_as_store': self.repo.read_bytes() == store,
            'repo_pb': rig_revision(self.repo_pb.read_bytes()),
            'pb_of_store': rig_revision(rig_bytes(self.store)),
            'original_pb': rig_revision(ROOM.with_suffix('.rig.pb').read_bytes()),
            'original': self._entries(ROOM),
            'fixtures': fixtures,
        }


def add_harness_routes(app, show: StubShow, files: RigFiles) -> None:
    @app.server.route('/_harness/rig', methods=['GET', 'POST'])
    def harness_rig():
        if request.method == 'GET':
            return jsonify(files.describe())
        payload = request.get_json()
        if payload.get('restore'):
            files.restore()
        if payload.get('bump'):
            files.bump()
        return jsonify({})

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
    if os.name == 'nt':
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), IDLE_PRIORITY_CLASS)


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
    with tempfile.TemporaryDirectory(prefix='autolux-gui-', ignore_cleanup_errors=True) as scratch:
        # The repo mirror is a scratch copy: a Save here must never write the tracked rig/.
        files = RigFiles(Path(scratch))
        show = StubShow(files.store)
        commands = queue.Queue(maxsize=8)
        bridge = SnapshotServer(
            show, bridge_port, commands, dashboard_port=dash_port, handoff_timeout=0.5
        )
        bridge.start()
        threading.Thread(target=engine, args=(show, commands), daemon=True).start()
        store = FixtureStore(files.store, ROOT / 'rig/library', rig_bytes, repo_rig=files.repo)
        app = build_app(SnapshotPoller(host='127.0.0.1', port=bridge_port), fixture_store=store)
        add_harness_routes(app, show, files)
        server = make_server('127.0.0.1', dash_port, app.server, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {
            **os.environ,
            'GUI_URL': f'http://127.0.0.1:{dash_port}/',
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
            time.sleep(1)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
