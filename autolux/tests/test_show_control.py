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
    _build_show_overview,
    control_command,
    decimal_text,
    expected_control_values,
    setting_text,
    settings_group_summary,
    special_groups_view,
    static_control_command,
    static_special_command,
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
        ('static-auto', None, None, False, {'action': 'mode', 'mode': 'auto'}),
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


def test_the_static_special_control_reaches_the_controller_queue_as_parsed(static_show):
    poller, commands = static_show
    command = static_special_command('fx', 'cold', 0.5, 0.25, 2)
    assert poller.control(command) == {'accepted': 'static'}
    assert commands.get_nowait() == {
        'action': 'static',
        'kind': 'special',
        'group': 'fx',
        'colour_slot': 'cold',
        'energy': 0.5,
        'speed': 0.25,
        'pulse_beats': 2,
    }
    assert static_special_command('fx', 'cold', 0.5, 0.25, 0)['pulse_beats'] is None
    clear = static_control_command('static-special-clear', 'all', None, None, False)
    assert poller.control(clear) == {'accepted': 'static'}
    assert commands.get_nowait() == {'action': 'static', 'kind': 'special_clear'}


@pytest.mark.parametrize(
    ('change', 'reason'),
    [
        ({'group': ''}, 'needs a group'),
        ({'colour_slot': 'pink'}, 'colour slot must be one of'),
        ({'energy': 2}, 'special energy must be within 0..1'),
        ({'speed': None}, 'special speed must be within 0..1'),
        ({'pulse_beats': 0}, 'a special pulse must be a whole number'),
    ],
)
def test_malformed_static_specials_return_the_reason(static_show, change, reason):
    poller, commands = static_show
    payload = static_special_command('fx', 'cold', 0.5, 0.5, None) | change
    assert reason in poller.control(payload)['error']
    assert commands.empty()


def test_a_special_without_a_group_never_reaches_the_controller():
    with pytest.raises(ValueError, match='choose a group'):
        static_special_command(None, 'cold', 0.5, 0.5, None)


def test_the_status_strip_names_the_active_special_and_its_group():
    cards = _build_show_overview({'special': {'name': 'cold_spin', 'group': 'fx'}})
    labels = [card.children[0].children for card in cards]
    assert labels.index('Special') == labels.index('Autoloop') + 1
    name, group = cards[labels.index('Special')].children[1].children
    assert (name.children, group.children) == ('Cold Spin', 'fx')


def test_the_config_area_lists_each_special_capable_group_with_its_axes():
    [row] = special_groups_view({'fx': ['colour', 'pulse', 'speed']})
    name, axes = row.children
    assert (name.children, axes.children) == ('fx', 'energy · colour · pulse · speed')
    [empty] = special_groups_view({})
    assert 'no group' in empty.children


def _ids(component) -> set:
    found = {getattr(component, 'id', None)}
    children = getattr(component, 'children', None)
    for child in children if isinstance(children, list) else [children]:
        if child is not None and not isinstance(child, str | int | float):
            found |= _ids(child)
    return found - {None}


def _find(component, wanted):
    if getattr(component, 'id', None) == wanted:
        return component
    children = getattr(component, 'children', None)
    for child in children if isinstance(children, list) else [children]:
        if child is not None and not isinstance(child, str | int | float):
            hit = _find(child, wanted)
            if hit is not None:
                return hit
    return None


def test_show_wide_controls_sit_above_the_mode_tabs_and_each_tab_holds_its_own():
    from autolux.simulate.visualizer_app import build_app

    buffer = EventBuffer()
    buffer.set_special_groups({'fx': ['colour', 'speed']})
    app = build_app(buffer)
    show_wide = _ids(_find(app.layout, 'show-control'))
    assert {'control-toggle', 'control-palette', 'control-brightness', 'control-intensity'} <= (
        show_wide
    )
    assert 'control-next' not in show_wide
    assert {'control-next', 'auto-special'} <= _ids(_find(app.layout, 'auto-mode-panel'))
    static = _ids(_find(app.layout, 'static-mode-panel'))
    assert {'static-special-group', 'static-special-fire', 'static-special-clear'} <= static
    assert 'special-groups' in _ids(_find(app.layout, 'config-panel'))
    client = app.server.test_client()
    assert client.get('/_dash-layout').status_code == 200
    assert client.get('/_dash-dependencies').status_code == 200
