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
