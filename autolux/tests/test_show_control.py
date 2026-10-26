import queue
import re

import pytest

from autolux.lib.engine.effect_definitions import ShowSettings
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.ui_bridge import SnapshotServer
from autolux.simulate.visualizer_app import (
    SETTINGS_GROUPS,
    ControlExpectations,
    SnapshotPoller,
    control_command,
    decimal_text,
    expected_control_values,
    setting_text,
    settings_group_summary,
)


@pytest.fixture
def show():
    commands = queue.Queue(maxsize=2)
    buffer = EventBuffer()
    buffer.set_intent('breakdown')
    server = SnapshotServer(buffer, 0, commands)
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
        ('control-next', False, None, (), {'action': 'next_autoloop'}),
        (
            'control-crossfade_beats',
            False,
            None,
            (30, 0.5, 2, 20, 6, 3),
            {'action': 'settings', 'settings': ShowSettings(30.0, 0.5, 2.0, 0.2, 6.0, 3.0)},
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
        (('30', '0', '2', '20', '6', '3'), 'strobe_burst_seconds must be within 0.1..60'),
        (('30', '0.5', '20', '20', '6', '3'), 'crossfade_beats must be within 0.25..16'),
    ],
)
def test_rejected_settings_return_the_reason_and_queue_nothing(show, settings, reason):
    poller, commands = show
    reply = poller.control(control_command('control-crossfade_beats', False, None, settings))
    assert reply == {'error': reason}
    assert commands.empty()


def test_a_comma_decimal_is_sent_as_the_same_number(show):
    poller, commands = show
    command = control_command(
        'control-crossfade_beats', False, None, ('30', '0,5', '2', ' 15,5 ', '6', '3')
    )
    assert poller.control(command) == {'accepted': 'settings'}
    assert commands.get_nowait()['settings'] == ShowSettings(30.0, 0.5, 2.0, 0.155, 6.0, 3.0)


@pytest.mark.parametrize(
    ('raw', 'reason'),
    [
        ('', 'Crossfade (beats) needs a number'),
        (None, 'Crossfade (beats) needs a number'),
        ('1,5,0', 'Crossfade (beats): "1,5,0" is not a number'),
        ('nan', 'Crossfade (beats): "nan" is not a number'),
    ],
)
def test_text_that_is_not_a_plain_decimal_never_reaches_the_controller(raw, reason):
    with pytest.raises(ValueError, match=re.escape(reason)):
        control_command('control-crossfade_beats', False, None, ('30', '0.5', raw, '20', '6', '3'))


def test_settings_fill_with_a_dot_decimal_whatever_the_locale():
    assert [decimal_text(v) for v in (0.15, 60.0, 0.125, 4.0)] == ['0.15', '60', '0.125', '4']
    assert setting_text('quiet_brightness', 0.155) == '15.5'


def test_each_collapsed_settings_group_summarises_its_live_values():
    settings = {
        'crossfade_beats': 1.0,
        'quiet_brightness': 0.15,
        'digital_silence_seconds': 2.0,
        'strobe_max_rate_hz': 8.0,
        'strobe_max_burst_seconds': 4.0,
        'strobe_burst_seconds': 1.0,
        'drop_strobe_cooldown_seconds': 60.0,
    }
    assert {
        group.key: settings_group_summary(group.names, settings) for group in SETTINGS_GROUPS
    } == {
        'transitions': '1 beats · quiet 15 % · standby 2 s',
        'strobe': '8 Hz · burst 4 s · drop burst 1 s · cooldown 60 s',
    }
    assert settings_group_summary(SETTINGS_GROUPS[1].names, {}) == (
        '— Hz · burst — s · drop burst — s · cooldown — s'
    )


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


@pytest.mark.parametrize(
    ('intent', 'paused', 'reason'),
    [
        (None, False, 'no section is playing yet'),
        ('standby', False, 'no section is playing yet'),
        ('breakdown', True, 'the show is paused'),
    ],
)
def test_next_autoloop_without_a_playing_section_is_refused_and_queues_nothing(
    intent, paused, reason
):
    buffer = EventBuffer()
    if intent:
        buffer.set_intent(intent)
    buffer.set_control_state(paused=paused, master_brightness=1.0, settings={})
    commands = queue.Queue()
    server = SnapshotServer(buffer, 0, commands)
    server.start()
    try:
        reply = SnapshotPoller(port=server.port).control({'action': 'next_autoloop'})
    finally:
        server.stop()
    assert reply == {'error': reason}
    assert commands.empty()


def test_an_expected_control_value_outlasts_one_stale_snapshot_until_confirmed_or_timed_out():
    now = [0.0]
    expectations = ControlExpectations(hold_seconds=2.0, clock=lambda: now[0])
    expectations.expect(paused=True, master_brightness=0.4)
    stale = {'paused': False, 'master_brightness': 1.0}
    assert expectations.apply(stale) == {'paused': True, 'master_brightness': 0.4}
    assert expectations.apply({'paused': True, 'master_brightness': 1.0})['paused'] is True
    assert expectations.apply(stale)['paused'] is False, 'a confirmed value follows the show again'
    now[0] = 2.0
    assert expectations.apply(stale)['master_brightness'] == 1.0, 'an unconfirmed value expires'


def test_each_accepted_command_names_the_value_the_show_should_report():
    settings = {'crossfade_beats': 2.0}
    assert expected_control_values({'action': 'pause'}, {'accepted': 'pause'}) == {'paused': True}
    assert expected_control_values({'action': 'master', 'level': 0.3}, {'accepted': 'master'}) == {
        'master_brightness': 0.3
    }
    assert expected_control_values(
        {'action': 'settings', 'settings': settings}, {'accepted': 'settings'}
    ) == {'settings': settings}
    assert expected_control_values({'action': 'next_autoloop'}, {'accepted': 'next_autoloop'}) == {}
