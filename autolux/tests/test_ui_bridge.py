import asyncio
import http.client
import json
import queue
import threading
import time

import pytest

from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.ui_bridge import CONTROL_PATH, SnapshotServer

DASHBOARD_PORT = 8765
DASHBOARD = f'http://autolux.local:{DASHBOARD_PORT}'
JSON = {'Content-Type': 'application/json'}


@pytest.fixture
def bridge():
    commands = queue.Queue(maxsize=4)
    server = SnapshotServer(
        EventBuffer(), 0, commands, dashboard_port=DASHBOARD_PORT, handoff_timeout=0.2
    )
    server.start()
    try:
        yield http.client.HTTPConnection('127.0.0.1', server.port, timeout=2), commands
    finally:
        server.stop()


def _engine(commands, taken):
    def run():
        while (command := commands.get()) is not None:
            taken.append(command)
            commands.task_done()

    threading.Thread(target=run, daemon=True).start()


def _request(connection, method, body=None, headers=None):
    payload = None if body is None else json.dumps(body)
    connection.request(method, CONTROL_PATH, payload, headers or {})
    reply = connection.getresponse()
    return reply, reply.read()


def test_a_control_post_returns_once_the_engine_has_taken_it_well_inside_50_ms(bridge):
    connection, commands = bridge
    taken = []
    _engine(commands, taken)
    headers = {**JSON, 'Origin': DASHBOARD}
    _request(connection, 'POST', {'action': 'master', 'level': 0.5}, headers)  # opens the socket
    rounds = []
    for level in (0.1, 0.2, 0.3, 0.4, 0.5):
        started = time.perf_counter()
        reply, body = _request(connection, 'POST', {'action': 'master', 'level': level}, headers)
        rounds.append(time.perf_counter() - started)
        assert reply.status == 202 and json.loads(body) == {'accepted': 'master'}
        assert taken[-1] == {'action': 'master', 'level': level}, 'replied before the hand-off'
        assert reply.getheader('Access-Control-Allow-Origin') == DASHBOARD
    commands.put(None)
    assert sorted(rounds)[len(rounds) // 2] < 0.05, rounds


def test_the_frame_cadence_drain_applies_a_post_before_the_reply_well_inside_50_ms(tmp_path):
    from types import SimpleNamespace

    from autolux.lib.main import AutoluxController, ShowControl

    class Engine:
        level = None

        def set_master_level(self, level):
            self.level = level

    control = ShowControl(tmp_path / 'show.json')
    show = SimpleNamespace(control=control, light_engine=Engine())
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    drain = asyncio.run_coroutine_threadsafe(AutoluxController._drain_operator_commands(show), loop)
    server = SnapshotServer(EventBuffer(), 0, control.commands, handoff_timeout=0.25)
    server.start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)
        rounds = []
        for level in (0.1, 0.2, 0.3, 0.4, 0.5):
            started = time.perf_counter()
            reply, _ = _request(connection, 'POST', {'action': 'master', 'level': level}, JSON)
            rounds.append(time.perf_counter() - started)
            assert reply.status == 202
            assert show.light_engine.level == level, 'replied before the engine applied it'
    finally:
        server.stop()
        drain.cancel()
        loop.call_soon_threadsafe(loop.stop)
    assert sorted(rounds)[len(rounds) // 2] < 0.05, rounds


def test_each_accepted_command_logs_one_line_naming_its_sender(bridge, caplog):
    connection, commands = bridge
    _engine(commands, [])
    headers = {**JSON, 'Origin': DASHBOARD, 'User-Agent': 'Phone ' + 'x' * 200}
    with caplog.at_level('INFO', logger='autolux.lib.ui_bridge'):
        _request(connection, 'POST', {'action': 'fx_auto', 'enabled': True}, headers)
        _request(connection, 'POST', {'action': 'dance'}, headers)
    commands.put(None)
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        f"[control] {{'action': 'fx_auto', 'enabled': True}} from 127.0.0.1 "
        f'origin={DASHBOARD} agent=Phone ' + 'x' * 74
    ]


def test_a_stalled_engine_is_reported_instead_of_claiming_the_command_applied(bridge):
    connection, _ = bridge
    reply, body = _request(connection, 'POST', {'action': 'pause'}, JSON)
    assert reply.status == 504
    assert json.loads(body) == {'error': 'the show has not taken the command yet'}


def test_the_dashboard_page_may_post_cross_origin_and_other_sites_may_not(bridge):
    connection, commands = bridge
    reply, _ = _request(
        connection,
        'OPTIONS',
        headers={'Origin': DASHBOARD, 'Access-Control-Request-Method': 'POST'},
    )
    assert reply.status == 204
    assert reply.getheader('Access-Control-Allow-Origin') == DASHBOARD
    assert 'Content-Type' in reply.getheader('Access-Control-Allow-Headers')

    foreign = {**JSON, 'Origin': 'http://evil.example'}
    assert _request(connection, 'OPTIONS', headers=foreign)[0].status == 403
    assert _request(connection, 'POST', {'action': 'pause'}, foreign)[0].status == 403
    # A plain form post skips the preflight, so it is refused whatever its origin.
    plain = {'Content-Type': 'text/plain'}
    assert _request(connection, 'POST', {'action': 'pause'}, plain)[0].status == 415
    assert commands.empty()


@pytest.mark.parametrize(
    ('body', 'content_type', 'status'),
    [
        ({'action': 'dance'}, 'application/json', 400),
        ({'action': 'pause', 'pad': 'x' * 5000}, 'application/json', 413),
        ({'action': 'pause'}, 'text/plain', 415),
        ({'action': 'pause'}, 'application/json', 504),
    ],
)
def test_the_dashboard_page_can_read_every_refusal(bridge, body, content_type, status):
    connection, _ = bridge
    headers = {'Content-Type': content_type, 'Origin': DASHBOARD}
    reply, _ = _request(connection, 'POST', body, headers)
    assert reply.status == status
    assert reply.getheader('Access-Control-Allow-Origin') == DASHBOARD


def test_a_foreign_origin_gets_no_cors_grant_on_its_refusal(bridge):
    connection, _ = bridge
    reply, _ = _request(
        connection, 'POST', {'action': 'pause'}, {**JSON, 'Origin': 'http://evil.example'}
    )
    assert reply.status == 403
    assert reply.getheader('Access-Control-Allow-Origin') is None


def test_an_oversized_control_body_is_refused_before_parsing(bridge):
    connection, commands = bridge
    reply, _ = _request(connection, 'POST', {'action': 'pause', 'pad': 'x' * 5000}, JSON)
    assert reply.status == 413
    assert commands.empty()


def test_a_refusal_arrives_whole_even_when_the_body_is_still_on_its_way(bridge):
    import socket

    connection, commands = bridge
    body = json.dumps({'action': 'pause', 'pad': 'x' * 5000}).encode()
    head = (
        f'POST {CONTROL_PATH} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
        f'Origin: {DASHBOARD}\r\nContent-Length: {len(body)}\r\n\r\n'
    ).encode()
    with socket.create_connection((connection.host, connection.port), timeout=2) as client:
        client.sendall(head)
        # The bridge waits for the body it refuses; replying first would reset the socket.
        time.sleep(0.1)
        client.sendall(body)
        reply = http.client.HTTPResponse(client)
        reply.begin()
        assert (reply.status, reply.getheader('Connection')) == (413, 'close')
        assert reply.getheader('Access-Control-Allow-Origin') == DASHBOARD
        reply.read()
    assert commands.empty()


def test_a_settings_post_carries_group_masters_and_refuses_one_outside_unit():
    from autolux.lib.ui_bridge import parse_control_command

    command = parse_control_command(
        {'action': 'settings', 'settings': {'group_masters': {'wash': 0.5}}}
    )
    # A settings edit carries only the fields it changes; the engine merges them.
    assert command['settings'] == {'group_masters': {'wash': 0.5}}
    with pytest.raises(ValueError, match='group master wash'):
        parse_control_command({'action': 'settings', 'settings': {'group_masters': {'wash': 2}}})
    for owned in ({'fx_auto': False}, {'intensity': 0.3}, {'intensity_auto': True}):
        with pytest.raises(ValueError, match='their own actions'):
            parse_control_command({'action': 'settings', 'settings': owned})


RIG = {'fixtures': [{'name': 'par', 'cells': ['par']}, {'name': 'bar', 'cells': ['bar/a']}]}


def test_a_highlight_names_one_fixture_or_cell_defaults_to_white_and_null_clears():
    from autolux.lib.ui_bridge import parse_control_command

    assert parse_control_command({'action': 'highlight', 'target': {'fixture': 'par'}}) == {
        'action': 'highlight',
        'target': {'fixture': 'par'},
        'colour': 'white',
    }
    assert parse_control_command(
        {'action': 'highlight', 'target': {'cell': 'bar/a'}, 'colour': 'green'}
    ) == {'action': 'highlight', 'target': {'cell': 'bar/a'}, 'colour': 'green'}
    assert parse_control_command({'action': 'highlight', 'target': None}) == {
        'action': 'highlight',
        'target': None,
    }
    for target in ({}, {'fixture': 'par', 'cell': 'par'}, {'group': 'wash'}, {'fixture': 3}, 'par'):
        with pytest.raises(ValueError, match='highlight target'):
            parse_control_command({'action': 'highlight', 'target': target})
    with pytest.raises(ValueError, match='highlight target'):
        parse_control_command({'action': 'highlight'})
    with pytest.raises(ValueError, match='colour'):
        parse_control_command({'action': 'highlight', 'target': {'fixture': 'par'}, 'colour': 'uv'})


@pytest.mark.parametrize(
    ('target', 'paused', 'blackout', 'refusal'),
    [
        ({'fixture': 'par'}, False, False, None),
        ({'cell': 'bar/a'}, False, False, None),
        ({'fixture': 'bar/a'}, False, False, "no fixture 'bar/a' in the running rig"),
        ({'cell': 'bar'}, False, False, "no cell 'bar' in the running rig"),
        ({'fixture': 'par'}, True, False, 'the show is paused'),
        ({'fixture': 'par'}, False, True, 'blackout is on'),
        (None, True, True, None),
    ],
)
def test_a_highlight_is_refused_for_a_fixture_the_rig_lacks_or_a_dark_rig(
    target, paused, blackout, refusal
):
    from autolux.lib.ui_bridge import command_refusal

    command = {'action': 'highlight', 'target': target, 'colour': 'white'}
    static = {'blackout': blackout}
    assert command_refusal(command, 'drop', paused, 'auto', static, RIG) == refusal


def test_an_unknown_fixture_never_reaches_the_show():
    buffer = EventBuffer()
    buffer.set_control_state(
        paused=False, master_brightness=1.0, settings={}, static={'blackout': False}, rig=RIG
    )
    commands = queue.Queue(maxsize=4)
    server = SnapshotServer(buffer, 0, commands, handoff_timeout=None)
    server.start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)
        body = {'action': 'highlight', 'target': {'fixture': 'ghost'}}
        reply, text = _request(connection, 'POST', body, JSON)
        assert (reply.status, json.loads(text)) == (
            409,
            {'error': "no fixture 'ghost' in the running rig"},
        )
        reply, _ = _request(connection, 'POST', {**body, 'target': {'fixture': 'par'}}, JSON)
        assert reply.status == 202
    finally:
        server.stop()
    assert commands.get_nowait()['target'] == {'fixture': 'par'}
    assert commands.empty()


def test_the_config_viewer_gets_the_show_start_check_inputs_for_save():
    from autolux.lib.ui_bridge import viewer_command

    setup = {
        'binary': 'starfire.exe',
        'rig_store': 'rig.mvr',
        'library': 'lib',
        'autoloops': 'loops',
        'expected_universe': 0,
    }
    command = viewer_command(8765, setup)
    assert command[command.index('--autoloops') + 1] == 'loops'
    assert command[command.index('--expected-universe') + 1] == '0'
    assert '--expected-universe' not in viewer_command(8765, {**setup, 'expected_universe': None})


def test_restart_show_reaches_the_engine_as_the_bare_action(bridge):
    connection, commands = bridge
    taken = []
    _engine(commands, taken)
    headers = {**JSON, 'Origin': DASHBOARD}
    reply, body = _request(connection, 'POST', {'action': 'restart_show', 'extra': 1}, headers)
    commands.put(None)
    assert (reply.status, json.loads(body)) == (202, {'accepted': 'restart_show'})
    assert taken == [{'action': 'restart_show'}]


def test_a_restarted_session_kills_its_viewer_and_brings_up_a_fresh_one(monkeypatch, caplog):
    import socket

    from autolux.lib import ui_bridge

    events = []

    class Viewer:
        pid = 1

        def __init__(self):
            self.killed = threading.Event()

        def wait(self):
            self.killed.wait()
            return 1

    def spawn(command):
        events.append('spawn')
        return Viewer()

    def kill(viewer):
        events.append('kill')
        viewer.killed.set()

    monkeypatch.setattr(ui_bridge, '_spawn', spawn)
    monkeypatch.setattr(ui_bridge, 'kill_tree', kill)
    monkeypatch.setattr(ui_bridge, '_probe', lambda port: events.append(('port free', port)))
    monkeypatch.setattr(ui_bridge, 'DASHBOARD_BIND_ADDRESS', '127.0.0.1')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        bridge_port = probe.getsockname()[1]
    ui_port = bridge_port - 1
    with caplog.at_level('WARNING'):
        for _ in range(2):
            session = ui_bridge.start(EventBuffer(), ui_port, commands=queue.Queue())
            assert session is not None, 'the bridge port was not free again for the next session'
            session.stop()
            session._watcher.join(2)
    # Each stop waits for the old viewer's port before the next session spawns its viewer.
    assert events == ['spawn', 'kill', ('port free', ui_port)] * 2
    assert 'viewer process exited' not in caplog.text


def test_a_relaunch_retries_a_taken_port_until_its_deadline():
    from autolux.lib.ui_bridge import _within

    now = [0.0]
    attempts = []

    def attempt(fails):
        attempts.append(now[0])
        if len(attempts) <= fails:
            raise OSError('address in use')
        return 'bound'

    def run(fails):
        attempts.clear()
        now[0] = 0.0
        return _within(
            1.0,
            lambda: attempt(fails),
            clock=lambda: now[0],
            sleep=lambda s: now.__setitem__(0, now[0] + s),
        )

    assert run(2) == 'bound' and attempts == [0.0, 0.25, 0.5]
    with pytest.raises(OSError, match='address in use'):
        run(100)
    assert attempts == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_a_relaunched_viewer_waits_30_s_for_its_port_then_exits_as_port_taken(monkeypatch):
    import http.server

    from werkzeug import serving

    from autolux.lib import ui_bridge

    now = [0.0]
    served = []

    def bind(self):
        if now[0] < taken_for:
            raise OSError('address in use')

    monkeypatch.setattr(http.server.HTTPServer, 'server_bind', bind)
    monkeypatch.setattr(serving.ThreadedWSGIServer, 'server_activate', lambda self: None)
    monkeypatch.setattr(
        serving.ThreadedWSGIServer, 'serve_forever', lambda self: served.append(now[0])
    )
    fake = {'clock': lambda: now[0], 'sleep': lambda s: now.__setitem__(0, now[0] + s)}
    taken_for = 10.0
    ui_bridge.serve_viewer(object(), 8999, **fake)
    assert served == [10.0]
    now[0], taken_for = 0.0, float('inf')
    with pytest.raises(SystemExit) as exited:
        ui_bridge.serve_viewer(object(), 8999, **fake)
    assert exited.value.code == ui_bridge.VIEWER_PORT_TAKEN and now[0] == ui_bridge.VIEWER_BIND_SEC
    assert served == [10.0]


def test_only_a_viewer_that_never_got_its_port_counts_as_a_lost_dashboard():
    from types import SimpleNamespace

    from autolux.lib import ui_bridge

    def bridge(code):
        viewer = SimpleNamespace(poll=lambda: code)
        return SimpleNamespace(_viewer=viewer)

    lost = [
        ui_bridge.UiBridge.dashboard_lost(bridge(code))
        for code in (None, 1, ui_bridge.VIEWER_PORT_TAKEN)
    ]
    assert lost == [False, False, True]
