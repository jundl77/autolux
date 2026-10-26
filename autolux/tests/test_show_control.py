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
    build_app,
    control_command,
    decimal_text,
    expected_control_values,
    setting_text,
    settings_group_summary,
    static_control_command,
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
        ('control-intensity-live', False, 0.35, (), {'action': 'intensity', 'level': 0.35}),
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
        'palette_temperature_bias': -0.5,
        'intensity': 0.7,
    }
    assert {
        group.key: settings_group_summary(group.names, settings) for group in SETTINGS_GROUPS
    } == {
        'transitions': '1 beats · quiet 15 % · standby 2 s',
        'strobe': '8 Hz · burst 4 s · drop burst 1 s · cooldown 60 s',
        'palette': 'warmth -0.5 · intensity 70 %',
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
    assert expected_control_values(
        {'action': 'palette', 'name': 'ice'}, {'accepted': 'palette'}
    ) == {'palette': {'name': 'ice', 'pinned': True}}
    assert (
        expected_control_values({'action': 'palette', 'name': 'auto'}, {'accepted': 'palette'})
        == {}
    )


@pytest.mark.parametrize('name', ['ice', 'auto'])
def test_a_palette_choice_reaches_the_controller_queue(show, name):
    poller, commands = show
    reply = poller.control(control_command('control-palette', False, None, (), palette=name))
    assert reply == {'accepted': 'palette'}
    assert commands.get_nowait() == {'action': 'palette', 'name': name}


@pytest.mark.parametrize('name', ['neon', 'ice_breakdown', None, 3])
def test_unknown_palettes_are_refused_before_the_controller(show, name):
    poller, commands = show
    reply = poller.control({'action': 'palette', 'name': name})
    assert reply['error'].startswith("palette must be 'auto' or one of techno, house, ice")
    assert commands.empty()


def test_a_settings_change_keeps_the_intensity_the_fader_set(show):
    poller, commands = show
    command = control_command(
        'control-crossfade_beats',
        False,
        None,
        ('30', '0.5', '2', '20', '6', '3', '3', '0'),
        intensity=0.4,
    )
    assert poller.control(command) == {'accepted': 'settings'}
    assert commands.get_nowait()['settings'].intensity == 0.4


@pytest.mark.parametrize('level', [1.2, -0.1, '0.5', None])
def test_an_intensity_outside_zero_to_one_is_refused_and_queues_nothing(show, level):
    poller, commands = show
    reply = poller.control({'action': 'intensity', 'level': level})
    assert reply == {'error': 'intensity must be within 0..1'}
    assert commands.empty()


@pytest.fixture
def static_show():
    commands = queue.Queue(maxsize=4)
    buffer = EventBuffer()
    buffer.set_control_state(paused=False, master_brightness=1.0, settings={}, mode='static')
    server = SnapshotServer(buffer, 0, commands)
    server.start()
    try:
        yield SnapshotPoller(port=server.port), commands
    finally:
        server.stop()


@pytest.mark.parametrize(
    ('trigger', 'colour', 'dimmer', 'blackout', 'expected'),
    [
        (
            'static-preset-warm',
            None,
            None,
            False,
            {'action': 'static', 'kind': 'preset', 'preset': 'warm'},
        ),
        # Off must not carry the picker's colour.
        (
            'static-preset-off',
            '#ffffff',
            1.0,
            False,
            {'action': 'static', 'kind': 'preset', 'preset': 'off'},
        ),
        ('static-blacklight', None, None, False, {'action': 'static', 'kind': 'blacklight'}),
        ('static-strobe', None, None, False, {'action': 'static', 'kind': 'strobe'}),
        (
            'static-blackout',
            None,
            None,
            True,
            {'action': 'static', 'kind': 'blackout', 'enabled': False},
        ),
        (
            'static-colour',
            ' FF8000',
            None,
            False,
            {
                'action': 'static',
                'kind': 'look',
                'target': 'wash',
                'colour': (1.0, 0.501961, 0.0),
                'dimmer': None,
            },
        ),
        (
            'static-dimmer-live',
            None,
            0.35,
            False,
            {'action': 'static', 'kind': 'look', 'target': 'wash', 'colour': None, 'dimmer': 0.35},
        ),
    ],
)
def test_each_static_control_reaches_the_controller_queue_as_parsed(
    static_show, trigger, colour, dimmer, blackout, expected
):
    poller, commands = static_show
    command = static_control_command(trigger, 'wash', colour, dimmer, blackout)
    assert poller.control(command) == {'accepted': expected['action']}
    assert commands.get_nowait() == expected


@pytest.mark.parametrize(
    ('payload', 'reason'),
    [
        ({'action': 'mode', 'mode': 'manual'}, 'mode must be auto or static'),
        ({'action': 'static', 'kind': 'look', 'target': 'wash'}, 'needs a colour or a dimmer'),
        (
            {'action': 'static', 'kind': 'look', 'target': 'wash', 'dimmer': 1.5},
            'within 0..1',
        ),
        (
            {'action': 'static', 'kind': 'look', 'target': 'wash', 'colour': 'red'},
            'colour must be #rrggbb',
        ),
        ({'action': 'static', 'kind': 'preset', 'preset': 'party'}, 'unknown static preset'),
        ({'action': 'static', 'kind': 'blackout', 'enabled': 1}, 'true or false'),
    ],
)
def test_malformed_mode_and_static_commands_return_the_reason_and_queue_nothing(
    static_show, payload, reason
):
    poller, commands = static_show
    assert reason in poller.control(payload)['error']
    assert commands.empty()


def test_static_commands_are_refused_in_auto_mode_and_skipping_in_static_mode():
    buffer = EventBuffer()
    buffer.set_intent('breakdown')
    commands = queue.Queue()
    server = SnapshotServer(buffer, 0, commands)
    server.start()
    try:
        poller = SnapshotPoller(port=server.port)
        buffer.set_control_state(paused=False, master_brightness=1.0, settings={})
        refused = poller.control({'action': 'static', 'kind': 'strobe'})
        buffer.set_control_state(paused=False, master_brightness=1.0, settings={}, mode='static')
        skipped = poller.control({'action': 'next_autoloop'})
        entered = poller.control({'action': 'mode', 'mode': 'static'})
    finally:
        server.stop()
    assert refused == {'error': 'the show is in auto mode'}
    assert skipped == {'error': 'the show is in static mode'}
    assert entered == {'accepted': 'mode'}
    assert commands.get_nowait() == {'action': 'mode', 'mode': 'static'}
    assert commands.empty()


def test_a_static_colour_that_is_not_hex_never_reaches_the_controller():
    with pytest.raises(ValueError, match='is not a #rrggbb colour'):
        static_control_command('static-colour', 'all', '#12345', None, False)


def _run_callback(app, output, values, triggered):
    callback = app.callback_map[output]
    inputs, state = (
        [
            {'id': item['id'], 'property': item['property'], 'value': values.get(item['id'])}
            for item in callback[kind]
        ]
        for kind in ('inputs', 'state')
    )
    outputs = [
        {'id': part.split('.')[0], 'property': part.split('.')[1].split('@')[0]}
        for part in output.strip('.').split('...')
    ]
    return app.server.test_client().post(
        '/_dash-update-component',
        json={
            'output': output,
            'outputs': outputs,
            'inputs': inputs,
            'state': state,
            'changedPropIds': [triggered],
        },
    )


def test_a_snapshot_sets_the_mode_tab_once_and_never_overwrites_a_click(show):
    poller, _ = show
    app = build_app(poller)
    writers = [key for key in app.callback_map if 'control-mode.value' in key]
    assert writers == ['..control-mode.value...control-mode-synced.data..']

    first = _run_callback(
        app, writers[0], {'control-state': {'mode': 'static'}}, 'control-state.data'
    )
    assert first.get_json()['response']['control-mode']['value'] == 'static'

    stale = _run_callback(
        app,
        writers[0],
        {'control-state': {'mode': 'auto'}, 'control-mode-synced': True},
        'control-state.data',
    )
    assert stale.status_code == 204


@pytest.mark.parametrize(('shown', 'picked'), [('auto', 'static'), ('static', 'auto')])
def test_picking_a_mode_tab_sends_that_mode_and_shows_it_at_once(show, shown, picked):
    poller, commands = show
    app = build_app(poller)
    output = next(key for key in app.callback_map if key.startswith('..static-status.children'))
    state = {'mode': shown, 'static': {'targets': ['all'], 'blackout': False}}
    response = _run_callback(
        app,
        output,
        {'control-mode': picked, 'static-target': 'all', 'control-state': state},
        'control-mode.value',
    )
    assert commands.get_nowait() == {'action': 'mode', 'mode': picked}
    assert response.get_json()['response']['control-state']['data']['mode'] == picked
