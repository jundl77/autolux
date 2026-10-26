import queue

import pytest

from autolux.lib.engine.effect_definitions import ShowSettings
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.ui_bridge import SnapshotServer
from autolux.simulate.visualizer_app import SnapshotPoller, control_command


@pytest.fixture
def show():
    commands = queue.Queue(maxsize=2)
    server = SnapshotServer(EventBuffer(), 0, commands)
    server.start()
    try:
        yield SnapshotPoller(port=server.port), commands
    finally:
        server.stop()


@pytest.mark.parametrize(
    ('trigger', 'paused', 'level', 'settings', 'expected'),
    [
        ('control-toggle', False, None, (), {'action': 'pause'}),
        ('control-toggle', True, None, (), {'action': 'play'}),
        ('control-brightness', False, 0.42, (), {'action': 'master', 'level': 0.42}),
        (
            'control-apply',
            False,
            None,
            (30, 0.5, 2, 0.2),
            {'action': 'settings', 'settings': ShowSettings(30.0, 0.5, 2.0, 0.2)},
        ),
    ],
)
def test_each_dashboard_control_reaches_the_controller_queue_as_sent(
    show, trigger, paused, level, settings, expected
):
    poller, commands = show
    reply = poller.control(control_command(trigger, paused, level, settings))
    assert reply == {'accepted': expected['action']}
    assert commands.get_nowait() == expected


@pytest.mark.parametrize(
    ('settings', 'reason'),
    [
        ((30, 0, 2, 0.2), 'strobe_burst_seconds must be within 0.1..4'),
        # A browser number input reports an out-of-range entry as null.
        ((30, 0.5, None, 0.2), 'crossfade_beats must be a number'),
    ],
)
def test_rejected_settings_return_the_reason_and_queue_nothing(show, settings, reason):
    poller, commands = show
    reply = poller.control(control_command('control-apply', False, None, settings))
    assert reply == {'error': reason}
    assert commands.empty()


def test_a_busy_show_is_reported_instead_of_raising(show):
    poller, commands = show
    for _ in range(commands.maxsize):
        commands.put_nowait({'action': 'pause'})
    assert poller.control({'action': 'play'}) == {'error': 'the show is busy; try again'}


def test_an_unreachable_show_is_reported_instead_of_raising():
    server = SnapshotServer(EventBuffer(), 0, queue.Queue())
    server.start()
    port = server.port
    server.stop()
    reply = SnapshotPoller(port=port, timeout=0.5).control({'action': 'pause'})
    assert reply == {'error': 'the show is not answering'}
