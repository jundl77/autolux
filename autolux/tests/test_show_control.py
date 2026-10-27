import json
import queue
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.effect_definitions import (
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    ShowSettings,
)
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.engine.static_mode import STATIC_GROUP_PRIORITY
from autolux.lib.main import apply_static
from autolux.lib.ui_bridge import SnapshotServer
from autolux.lighting.palettes import PALETTE_NAMES
from autolux.simulate.visualizer_app import (
    DIRECT_AUTO_JS,
    FORM_SETTINGS,
    INTENSITY_LEVELS,
    PALETTE_POLICY,
    PALETTE_SYNC_JS,
    STATIC_CHIP_IDS,
    STATIC_TARGETS_JS,
    ControlExpectations,
    SnapshotPoller,
    _with_base_palette,
    build_app,
    control_command,
    control_page_config,
    decimal_text,
    expected_control_values,
    group_master_edit,
    group_master_rows,
    setting_text,
    static_control_command,
    with_control_flags,
)
from autolux.tests.test_static_mode import overrides, static_engine


def test_group_masters_round_trip_through_show_json_and_reject_levels_outside_unit(tmp_path):
    from autolux.lib.main import load_show_settings, save_show_settings

    path = tmp_path / 'show.json'
    saved = ShowSettings(quiet_brightness=0.3, group_masters={'wash': 0.5, 'movers': 0})
    save_show_settings(path, saved)
    assert json.loads(path.read_text(encoding='utf-8'))['settings']['group_masters'] == {
        'movers': 0.0,
        'wash': 0.5,
    }
    assert load_show_settings(path) == saved
    assert ShowSettings.from_dict({}).group_masters == {}
    for masters in ({'wash': 1.5}, {'wash': -0.1}, {'wash': True}, {'': 0.5}, ['wash']):
        with pytest.raises((TypeError, ValueError)):
            ShowSettings(group_masters=masters)


def test_a_group_master_edit_sends_only_the_group_masters_with_that_group_changed():
    current = ShowSettings(crossfade_beats=4, group_masters={'movers': 0.25}).to_dict()
    state = {'settings': current}
    command = group_master_edit('wash', 0.5, state)
    assert command == {
        'action': 'settings',
        'settings': {'group_masters': {'movers': 0.25, 'wash': 0.5}},
    }
    assert group_master_edit('movers', 0.25, state) is None
    assert group_master_edit('wash', 1.0, state) is None
    assert group_master_edit('wash', 0.5, {}) is None


def test_the_settings_area_has_one_master_brightness_style_fader_per_rig_group():
    rows = group_master_rows(
        {'wash': ['colour'], 'movers': []}, {'group_masters': {'wash': 0.4}}, disabled=False
    )
    sliders = [row.children[1].children[0].children for row in rows]
    assert [slider.id for slider in sliders] == [
        {'type': 'group-master', 'group': 'movers'},
        {'type': 'group-master', 'group': 'wash'},
    ]
    assert [slider.value for slider in sliders] == [1.0, 0.4]
    assert all(row.children[1].className == 'fader' for row in rows)
    assert all(slider.min == 0 and slider.max == 1 for slider in sliders)
    assert build_app(_Recorder()).layout['group-masters'].children is None


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
        ('control-intensity', False, 0.7, (), {'action': 'intensity', 'level': 0.7}),
        (
            'control-crossfade_beats',
            False,
            None,
            (30, 0.5, 2, 20, 6, 3),
            {
                'action': 'settings',
                'settings': dict(zip(FORM_SETTINGS, (30.0, 0.5, 2.0, 0.2, 6.0, 3.0))),
            },
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
    assert commands.get_nowait()['settings'] == dict(
        zip(FORM_SETTINGS, (30.0, 0.5, 2.0, 0.155, 6.0, 3.0))
    )


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


def test_a_held_chip_or_settings_field_outlasts_a_stale_poll_of_the_settings_it_lives_in():
    now = [0.0]
    expectations = ControlExpectations(hold_seconds=2.0, clock=lambda: now[0])
    expectations.expect(fx_auto=False, settings={'crossfade_beats': 2.0})
    stale = with_control_flags({'settings': {'crossfade_beats': 1.0, 'fx_auto': True}})
    shown = expectations.apply(stale)
    assert shown['fx_auto'] is False
    assert shown['settings'] == {'crossfade_beats': 2.0, 'fx_auto': True}
    landed = with_control_flags({'settings': {'crossfade_beats': 2.0, 'fx_auto': False}})
    assert expectations.apply(landed) == landed
    assert expectations.apply(stale) == stale, 'both confirmed, the poll rules again'


def test_the_relay_holds_an_fx_click_on_the_chip_until_the_show_reports_it():
    class Lagging(_Recorder):
        def __init__(self):
            super().__init__()
            self.settings = {'fx_auto': True}

        def snapshot(self):
            return {'settings': dict(self.settings), 'master_brightness': 1.0}

    source = Lagging()
    app = build_app(source, direct_control=False)
    refresh = next(key for key in app.callback_map if key.startswith('..sync.data'))
    gate = {'tab': 'control', 'metrics': False, 'timeline': False}

    def polled():
        reply = _run_callback(app, refresh, {'gate': gate}, 'gate.data').get_json()
        return reply['response']['control-state']['data']['fx_auto']

    client = app.server.test_client()
    reply = client.post('/control-relay', json={'action': 'fx_auto', 'enabled': False})
    assert reply.get_json() == {'accepted': 'fx_auto'}
    assert polled() is False, 'the engine has not published it yet'
    source.settings = {'fx_auto': False}
    assert polled() is False
    source.settings = {'fx_auto': True}
    assert polled() is True, 'confirmed once, the show rules the chip again'


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


def test_a_settings_change_carries_only_the_form_so_a_stale_poll_cannot_undo_a_chip(show):
    poller, commands = show
    command = control_command(
        'control-crossfade_beats', False, None, ('30', '0.5', '2', '20', '6', '3', '3'), auto=True
    )
    assert poller.control(command) == {'accepted': 'settings'}
    assert set(commands.get_nowait()['settings']) == set(FORM_SETTINGS)


def test_a_settings_change_from_an_old_panel_still_carrying_warmth_is_accepted(show):
    poller, commands = show
    command = control_command(
        'control-crossfade_beats', False, None, ('30', '0.5', '2', '20', '6', '3', '3')
    )
    command['settings']['palette_temperature_bias'] = -0.5
    assert poller.control(command) == {'accepted': 'settings'}
    assert 'palette_temperature_bias' not in commands.get_nowait()['settings']


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
    ('trigger', 'target', 'colour', 'expected'),
    [
        (
            'static-preset-warm',
            'all',
            None,
            {'action': 'static', 'kind': 'preset', 'preset': 'warm'},
        ),
        (
            'static-preset-warm',
            'wash',
            None,
            {
                'action': 'static',
                'kind': 'look',
                'target': 'wash',
                'colour': (1.0, 0.54902, 0.2),
                'dimmer': None,
            },
        ),
        # Off must not carry the picker's colour.
        (
            'static-preset-off',
            'all',
            '#ffffff',
            {'action': 'static', 'kind': 'preset', 'preset': 'off'},
        ),
        (
            'static-preset-off',
            'wash',
            '#ffffff',
            {'action': 'static', 'kind': 'look', 'target': 'wash', 'colour': None, 'dimmer': 0.0},
        ),
        ('static-preset-uv', 'all', None, {'action': 'static', 'kind': 'preset', 'preset': 'uv'}),
        (
            'static-preset-uv',
            'wash',
            None,
            {
                'action': 'static',
                'kind': 'look',
                'target': 'wash',
                'colour': (0.0, 0.0, 0.0),
                'dimmer': 1.0,
                'uv': True,
            },
        ),
        (
            'static-colour',
            'wash',
            ' FF8000',
            {
                'action': 'static',
                'kind': 'look',
                'target': 'wash',
                'colour': (1.0, 0.501961, 0.0),
                'dimmer': None,
            },
        ),
    ],
)
def test_each_static_control_reaches_the_controller_queue_as_parsed(
    static_show, trigger, target, colour, expected
):
    poller, commands = static_show
    command = static_control_command(trigger, target, colour)
    assert poller.control(command) == {'accepted': expected['action']}
    assert commands.get_nowait() == expected


async def test_red_with_target_wash_lights_only_the_wash(static_show):
    poller, commands = static_show
    app = build_app(poller, direct_control=False)
    output = 'static-status.children'
    state = {'mode': 'static', 'static': {'targets': ['all', 'wash', 'strip'], 'blackout': False}}
    _run_callback(
        app,
        output,
        {'static-preset-red': 1, 'static-target': 'wash', 'control-state': state},
        'static-preset-red.n_clicks',
    )
    command = commands.get_nowait()
    assert command['target'] == 'wash'

    light, _, _, lighting = static_engine()
    apply_static(light, command)
    await light.sync_static()
    assert light.mode == 'static'
    assert light.static_looks.state()['looks'] == {
        'wash': {'colour': '#ff0000', 'dimmer': 1.0, 'uv': False}
    }
    looks = {
        o['value']['id']: o['value'] for o in overrides(lighting) if o['kind'] == 'set_override'
    }
    assert looks['static_wash']['target'] == {'group': 'wash'}
    assert 'static_floor' not in looks


@pytest.mark.parametrize(
    ('payload', 'reason'),
    [
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
        ({'action': 'static', 'kind': 'strobe'}, 'unknown static command'),
    ],
)
def test_malformed_static_commands_return_the_reason_and_queue_nothing(
    static_show, payload, reason
):
    poller, commands = static_show
    assert reason in poller.control(payload)['error']
    assert commands.empty()


def test_static_commands_need_no_mode_and_skipping_waits_for_the_overlay_to_clear():
    buffer = EventBuffer()
    buffer.set_intent('breakdown')
    commands = queue.Queue()
    server = SnapshotServer(buffer, 0, commands)
    server.start()
    try:
        poller = SnapshotPoller(port=server.port)
        buffer.set_control_state(paused=False, master_brightness=1.0, settings={})
        accepted = poller.control({'action': 'static', 'kind': 'clear'})
        buffer.set_control_state(paused=False, master_brightness=1.0, settings={}, mode='static')
        skipped = poller.control({'action': 'next_autoloop'})
        retired = poller.control({'action': 'mode', 'mode': 'static'})
    finally:
        server.stop()
    assert accepted == {'accepted': 'static'}
    assert skipped == {'error': 'a static overlay is active'}
    assert 'unknown control action' in retired['error']
    assert commands.get_nowait() == {'action': 'static', 'kind': 'clear'}
    assert commands.empty()


def test_a_static_colour_that_is_not_hex_never_reaches_the_controller():
    with pytest.raises(ValueError, match='is not a #rrggbb colour'):
        static_control_command('static-colour', 'all', '#12345')


def test_the_static_special_command_reaches_the_controller_queue_as_parsed(static_show):
    poller, commands = static_show
    command = {'action': 'static_fx', 'row': 'drift', 'target': 'fx'}
    assert poller.control(command) == {'accepted': 'static_fx'}
    assert commands.get_nowait() == command
    assert (
        'needs a row and a target'
        in poller.control({'action': 'static_fx', 'row': 'drift'})['error']
    )
    assert poller.control({'action': 'static', 'kind': 'special_fx_clear'}) == {
        'accepted': 'static'
    }
    assert commands.get_nowait() == {'action': 'static', 'kind': 'special_fx_clear'}


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
    if not output.startswith('..'):
        outputs = outputs[0]
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
    app = build_app(poller, direct_control=False)
    assert not [key for key in app.callback_map if 'control-mode.value' in key]
    loader = 'control-mode-synced.data'

    first = _run_callback(app, loader, {'control-state': {'mode': 'static'}}, 'control-state.data')
    assert first.get_json()['sideUpdate'] == {'control-mode': {'value': 'static'}}

    stale = _run_callback(
        app,
        loader,
        {'control-state': {'mode': 'auto'}, 'control-mode-synced': True},
        'control-state.data',
    )
    assert stale.status_code == 204


@pytest.mark.parametrize('direct', [True, False])
def test_switching_tabs_only_changes_the_view(direct):
    app = build_app(_Recorder(), direct_control=direct)
    listeners = [
        key
        for key, callback in app.callback_map.items()
        if any(item['id'] == 'control-mode' for item in callback['inputs'])
    ]
    assert listeners == ['..auto-mode-panel.style...static-mode-panel.style..']


def _send_control(app, trigger, value, state):
    output = next(key for key in app.callback_map if key.startswith('..control-status.children'))
    return _run_callback(
        app, output, {trigger.split('.')[0]: value, 'control-state': state}, trigger
    )


def test_each_intensity_segment_sends_its_mapped_level(show):
    poller, commands = show
    app = build_app(poller, direct_control=False)
    options = app.layout['control-intensity'].options
    assert [(o['label'], o['value']) for o in options] == [
        ('calm', INTENSITY_CALM),
        ('balanced', INTENSITY_BALANCED),
        ('wild', INTENSITY_WILD),
    ]
    state = {'settings': {'intensity': INTENSITY_BALANCED}}
    for word, level in INTENSITY_LEVELS:
        _send_control(app, 'control-intensity.value', level, state)
        if level != INTENSITY_BALANCED:
            assert commands.get_nowait() == {'action': 'intensity', 'level': level}, word
    assert commands.empty()


def test_a_palette_pick_sends_the_palette_command(show):
    poller, commands = show
    app = build_app(poller, direct_control=False)
    choices = [o['value'] for o in app.layout['control-palette'].options]
    assert choices == [PALETTE_POLICY, *PALETTE_NAMES]
    _send_control(app, 'control-palette.value', 'ice', {'palette': {'name': 'techno'}})
    assert commands.get_nowait() == control_command(
        'control-palette', False, None, (), palette='ice'
    )


NODE = shutil.which('node')


def _run_js(function_source, calls):
    script = (
        'const written = [];\n'
        "const window = {dash_clientside: {set_props: (id, props) => written.push([id, props])}};\n"
        f'const fn = {function_source};\n'
        f'{json.dumps(calls)}.forEach((args) => fn(...args));\n'
        'console.log(JSON.stringify(written));'
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason='needs node to run the clientside callback')
def test_a_palette_pick_survives_the_next_poll_and_sends_the_pin(show):
    poller, commands = show
    app = build_app(poller, direct_control=False)
    unpinned = {'palette': {'name': 'techno', 'pinned': False}}
    _send_control(app, 'control-palette.value', 'ice', unpinned)
    assert commands.get_nowait() == {'action': 'palette', 'name': 'ice'}

    expectations = ControlExpectations(clock=lambda: 0.0)
    expectations.expect(
        **expected_control_values({'action': 'palette', 'name': 'ice'}, {'accepted': 'palette'})
    )
    # In a breakdown the show reports the variant; the pin must still read as confirmed.
    landed = expectations.apply(
        _with_base_palette({'palette': {'name': 'ice_breakdown', 'pinned': True}})
    )
    assert landed['palette'] == {'name': 'ice', 'pinned': True}

    writes = _run_js(PALETTE_SYNC_JS, [[unpinned], [unpinned], [landed], [landed]])
    assert writes == [
        ['control-palette', {'value': PALETTE_POLICY}],
        ['control-palette', {'value': 'ice'}],
    ]


def _static_targets(state):
    script = f'console.log(JSON.stringify(({STATIC_TARGETS_JS})({json.dumps(state)})));'
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason='needs node to run the clientside callback')
def test_static_targets_and_uv_chips_follow_the_published_rig():
    # Group names a venue might use that the room rig does not have.
    venue = {'targets': ['all', 'truss_a', 'floor_pars'], 'blacklight': False}
    options, uv_off = _static_targets({'mode': 'static', 'static': venue})
    assert [option['value'] for option in options] == venue['targets']
    assert options[0]['label'] == 'All'
    assert uv_off
    uv_rig = {'targets': ['all', 'uv_bar'], 'blacklight': True}
    assert _static_targets({'mode': 'static', 'static': uv_rig})[1:] == [False]


CONTROL_JS = Path(__file__).parents[1] / 'simulate' / 'assets' / 'control.js'


def _run_control_js(body: str):
    script = (
        'globalThis.window = {};\n'
        + CONTROL_JS.read_text(encoding='utf8')
        + '\nconst control = window.autoluxControl;\n'
        + body
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class _Recorder:
    def __init__(self):
        self.commands = []

    def snapshot(self):
        return {}

    def control(self, command):
        self.commands.append(command)
        return {'accepted': command['action']}


AUTO_STATE = {
    'paused': False,
    'master_brightness': 0.8,
    'settings': {'intensity': 0.7},
    'intent': 'drop',
    'palette': {'name': 'ice', 'pinned': True},
    'mode': 'auto',
    'static': {'targets': ['all', 'wash'], 'blackout': False},
}
STATIC_STATE = {**AUTO_STATE, 'mode': 'static'}
# (trigger, value, control-state, static target, the helper call that must build the same JSON)
OPERATOR_CASES = [
    ('control-toggle.n_clicks', 1, AUTO_STATE, 'all', 'build.toggle(false)'),
    ('control-toggle.n_clicks', 1, {**AUTO_STATE, 'paused': True}, 'all', 'build.toggle(true)'),
    ('control-next.n_clicks', 1, AUTO_STATE, 'all', 'build.next()'),
    ('control-palette.value', 'techno', AUTO_STATE, 'all', "build.palette('techno')"),
    (
        'control-palette.value',
        PALETTE_POLICY,
        AUTO_STATE,
        'all',
        f"build.palette('{PALETTE_POLICY}')",
    ),
    ('control-brightness.value', 0.42, AUTO_STATE, 'all', 'build.master(0.42)'),
    ('control-brightness-live.data', 0.35, AUTO_STATE, 'all', 'build.master(0.35)'),
    ('control-intensity.value', 0.3, AUTO_STATE, 'all', 'build.intensity(0.3)'),
    ('control-intensity-auto.n_clicks', 1, AUTO_STATE, 'all', 'build.intensityAuto(true)'),
    (
        'control-intensity-auto.n_clicks',
        1,
        {**AUTO_STATE, 'intensity_auto': True},
        'all',
        'build.intensityAuto(false)',
    ),
    ('control-fx-off.n_clicks', 1, AUTO_STATE, 'all', 'build.fxAuto(false)'),
    ('control-fx-auto.n_clicks', 1, {**AUTO_STATE, 'fx_auto': False}, 'all', 'build.fxAuto(true)'),
    *[
        (f'{chip}.n_clicks', 1, STATIC_STATE, target, f"build.static('{chip}', '{target}')")
        for chip in STATIC_CHIP_IDS
        for target in ('all', 'wash')
    ],
]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_browser_helper_builds_the_payload_the_python_callback_sends():
    source = _Recorder()
    app = build_app(source, direct_control=False)
    control_output = next(k for k in app.callback_map if k.startswith('..control-status.children'))
    static_output = 'static-status.children'
    for trigger, value, state, target, _ in OPERATOR_CASES:
        component = trigger.split('.')[0]
        is_static = component.startswith('static-') or component == 'control-mode'
        _run_callback(
            app,
            static_output if is_static else control_output,
            {component: value, 'control-state': state, 'static-target': target},
            trigger,
        )
    config = json.dumps(control_page_config(source, True))
    browser = _run_control_js(
        f'window.autoluxControlConfig = {config};\n'
        'console.log(JSON.stringify(['
        + ', '.join(f'control.{case[4]}' for case in OPERATOR_CASES)
        + ']));'
    )
    assert source.commands == browser


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_master_drags_coalesce_to_one_request_in_flight_and_fall_back_to_the_relay():
    calls = _run_control_js(
        """
window.autoluxControlConfig = {direct: true, bridgePort: 8766};
globalThis.location = {protocol: 'http:', hostname: 'autolux.local'};
const calls = [];
const pending = [];
globalThis.fetch = (url, options) => {
    calls.push([url, JSON.parse(options.body).level]);
    if (url.includes(':8766')) {
        return Promise.reject(new TypeError('no CORS yet'));
    }
    return new Promise((resolve) => pending.push(
        () => resolve({ok: true, status: 202, json: async () => ({accepted: 'master'})})));
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
(async () => {
    control.operatorActs();
    const done = control.sendLatest('master', control.build.master(0.1));
    await settle();
    control.sendLatest('master', control.build.master(0.2));
    control.sendLatest('master', control.build.master(0.3));
    pending.shift()();
    await settle();
    pending.shift()();
    await done;
    console.log(JSON.stringify(calls));
})();
"""
    )
    assert calls == [
        ['http://autolux.local:8766/control', 0.1],
        ['/control-relay', 0.1],
        ['/control-relay', 0.3],
    ]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_a_refused_or_stalled_command_reverts_its_hold_and_is_never_relayed():
    result = _run_control_js(
        """
window.autoluxControlConfig = {direct: true, bridgePort: 8766};
globalThis.location = {protocol: 'http:', hostname: 'autolux.local'};
const urls = [];
const answers = [
    {ok: false, status: 409, json: async () => ({error: 'the show is paused'})},
    {ok: false, status: 504, json: async () => { throw new SyntaxError('empty'); }},
];
globalThis.fetch = async (url) => { urls.push(url); return answers.shift(); };
(async () => {
    control.operatorActs();
    control.hold({paused: true});
    const refused = await control.dispatch(control.build.toggle(false), null, ['paused']);
    const stalled = await control.dispatch(control.build.next(), null);
    console.log(JSON.stringify({refused, stalled, urls, shown: control.view({paused: false})}));
})();
"""
    )
    assert result == {
        'refused': {'error': 'the show is paused'},
        'stalled': {'error': 'the show answered 504'},
        'urls': ['http://autolux.local:8766/control'] * 2,
        'shown': {'paused': False},
    }


def test_the_relay_forwards_a_browser_command_and_refuses_what_the_show_refuses(show):
    poller, commands = show
    client = build_app(poller).server.test_client()
    reply = client.post('/control-relay', json={'action': 'master', 'level': 0.42})
    assert reply.get_json() == {'accepted': 'master'}
    assert commands.get_nowait() == {'action': 'master', 'level': 0.42}
    refused = client.post('/control-relay', json={'action': 'master', 'level': 4})
    assert 'error' in refused.get_json()
    assert commands.empty()


def test_no_poll_output_writes_an_operator_control():
    # An Output on these would make the operator's own callback wait for each in-flight poll.
    app = build_app(_Recorder())
    owned = ('control-brightness.value', 'control-intensity.value', 'control-palette.value')
    assert not [key for key in app.callback_map for prop in owned if prop in key]


def test_the_telemetry_charts_are_in_the_page_and_filled_by_the_first_control_poll():
    from autolux.simulate import system_metrics

    app = build_app(_Recorder())
    graphs = [f'metric-{panel}' for panel, *_ in system_metrics.PANELS]
    assert all(app.layout[graph] is not None for graph in graphs)
    refresh = next(key for key in app.callback_map if key.startswith('..sync.data'))
    gate = {'tab': 'control', 'metrics': True, 'timeline': False}
    reply = _run_callback(app, refresh, {'gate': gate}, 'gate.data').get_json()
    figures = reply['response']['telemetry-figures']['data']
    assert len(figures) == len(graphs)
    assert all('data' in figure and 'layout' in figure for figure in figures)


def test_the_colors_status_shows_the_active_palette_whether_pinned_or_chosen_by_the_show():
    from autolux.lighting.palettes import PALETTES
    from autolux.simulate.visualizer_app import PALETTE_SWATCH_SLOTS, _build_show_status, _rgb_css

    house = next(palette for palette in PALETTES if palette.name == 'house')
    swatches = [_rgb_css(getattr(house, slot)) for slot in PALETTE_SWATCH_SLOTS]
    for pinned, tag in ((False, ['auto']), (True, [])):
        status = _build_show_status({'palette': {'name': 'house', 'pinned': pinned}}, now=0.0)
        colors = next(item for item in status if item.children[0].children == 'Colors')
        value = colors.children[1].children
        dots = [part for part in value if getattr(part, 'className', None) == 'dot']
        assert [dot.style['background'] for dot in dots] == swatches
        assert 'house' in value
        assert [
            part.children for part in value if getattr(part, 'className', '') == 'status-tag'
        ] == tag
        assert not [part for part in value if 'dot-auto' in (getattr(part, 'className', '') or '')]


FX_ROWS = {'all': ['cold_drift', 'drift', 'spin'], 'fx': ['cold_drift', 'drift'], 'wash': []}
FX_STATE = {
    **STATIC_STATE,
    'static': {
        **STATIC_STATE['static'],
        'fx_rows': FX_ROWS,
        'special_fx': {'name': 'drift', 'group': 'fx'},
    },
}
# (picked row, static target, the command the show must receive or None)
FX_PICKS = [
    ('cold_drift', 'fx', {'action': 'static_fx', 'row': 'cold_drift', 'target': 'fx'}),
    ('drift', 'all', {'action': 'static_fx', 'row': 'drift', 'target': 'all'}),
    # The row already held there: a poll syncing the picker must not re-fire it.
    ('drift', 'fx', None),
    (None, 'fx', None),
]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_fx_picker_sends_the_row_from_python_and_the_browser_alike(static_show):
    source = _Recorder()
    app = build_app(source, direct_control=False)
    static_output = 'static-status.children'
    for row, target, _ in FX_PICKS:
        values = {'static-fx-row': row, 'control-state': FX_STATE, 'static-target': target}
        _run_callback(app, static_output, values, 'static-fx-row.value')
    _run_callback(app, static_output, {'control-state': FX_STATE}, 'static-fx-clear.n_clicks')
    expected = [command for *_, command in FX_PICKS if command]
    clear = {'action': 'static', 'kind': 'special_fx_clear'}
    assert source.commands == [*expected, clear]

    calls = [
        f'control.build.staticFx({json.dumps(row)}, {json.dumps(target)}, {json.dumps(FX_STATE)})'
        for row, target, _ in FX_PICKS
    ]
    built = _run_control_js(
        f'console.log(JSON.stringify([{", ".join(calls)}, control.build.staticFxClear()]));'
    )
    assert built == [*(command for *_, command in FX_PICKS), clear]

    poller, commands = static_show
    for command in (*expected, clear):
        assert poller.control(command) == {'accepted': command['action']}
        assert commands.get_nowait() == command


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_fx_picker_lists_only_the_rows_that_fit_the_target():
    from autolux.simulate.visualizer_app import FX_ROWS_JS

    shown = _run_control_js(
        f'const rows = {FX_ROWS_JS};\n'
        f'const state = {json.dumps(FX_STATE)};\n'
        "console.log(JSON.stringify([rows('fx', state), rows('all', state), rows('wash', state),"
        " rows('all', {static: {}})]));"
    )
    assert shown == [
        [
            [{'label': 'Cold drift', 'value': 'cold_drift'}, {'label': 'Drift', 'value': 'drift'}],
            'drift',
            '',
        ],
        [
            [
                {'label': 'Cold drift', 'value': 'cold_drift'},
                {'label': 'Drift', 'value': 'drift'},
                {'label': 'Spin', 'value': 'spin'},
            ],
            None,
            '',
        ],
        [[], None, 'wash has no FX that fits'],
        [[], None, ''],
    ]


def test_the_status_line_shows_the_active_fx_and_the_strobe_cooldown():
    from autolux.simulate.visualizer_app import _build_show_status

    def items(snapshot):
        return {
            item.children[0].children: item.children[1].children
            for item in _build_show_status(snapshot, now=0.0)
        }

    clock = VirtualClock()
    buffer = EventBuffer(clock=clock)
    buffer.start()
    buffer.set_control_state(
        paused=False,
        master_brightness=1.0,
        settings={'drop_strobe_cooldown_seconds': 60.0, 'intensity': 0.7},
    )
    assert items(buffer.snapshot())['FX'] == '-'
    assert items(buffer.snapshot())['Strobe'] == 'ready'
    buffer.set_special('cold_spin', 'fx')
    buffer.mark_intense()
    clock.advance(19.0)
    shown = items(buffer.snapshot())
    assert shown['FX'] == 'Cold spin · fx'
    assert shown['Strobe'] == 'in 41 s'
    labels = [item.children[0].children for item in _build_show_status(buffer.snapshot(), 0.0)]
    assert labels.index('FX') == labels.index('Colors') + 1
    static = {'static': {'special_fx': {'name': 'cold_drift', 'group': 'wash'}}}
    assert items(static)['FX'] == 'Static · Cold drift · wash'


def test_the_config_rig_area_lists_each_groups_fx_capabilities():
    from autolux.simulate.visualizer_app import special_groups_view

    line = special_groups_view(
        {'wash': ['colour'], 'fx': ['colour', 'pulse', 'speed']}, {'fx': 'accent'}
    )
    assert [getattr(part, 'children', part) for part in line] == [
        'FX',
        '  ',
        'fx',
        ' ',
        'accent · colour · pulse · speed',
        '  ',
        'wash',
        ' ',
        'loop · colour',
    ]
    assert special_groups_view({}) == ['FX: Starfire reported no groups']


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_a_picked_fx_row_is_sent_once_and_clear_fx_empties_the_picker_in_the_browser():
    from autolux.simulate.visualizer_app import FX_DIRECT_JS

    sent, picked, synced, cleared = _run_control_js(
        'const sent = [];\n'
        'control.dispatch = (command) => { sent.push(command); return Promise.resolve({}); };\n'
        'window.dash_clientside = {no_update: "same", set_props: () => {},'
        ' callback_context: {triggered: []}};\n'
        f'const fx = {FX_DIRECT_JS};\n'
        f'const state = {json.dumps(FX_STATE)};\n'
        'const run = (trigger, row) => {\n'
        '  window.dash_clientside.callback_context.triggered = [{prop_id: trigger}];\n'
        '  return fx(row, 1, "fx", state);\n'
        '};\n'
        'const a = run("static-fx-row.value", "cold_drift");\n'
        # The poll has not caught up, so the held pick is what the picker syncs back to.
        'const b = run("static-fx-row.value", "cold_drift");\n'
        'const c = run("static-fx-clear.n_clicks", "cold_drift");\n'
        'console.log(JSON.stringify([sent, a, b, c]));'
    )
    assert sent == [
        {'action': 'static_fx', 'row': 'cold_drift', 'target': 'fx'},
        {'action': 'static', 'kind': 'special_fx_clear'},
    ]
    assert (picked, synced, cleared) == ('same', 'same', None)


@pytest.mark.parametrize('reported', ['techno', 'techno_breakdown', 'techno_drop'])
def test_every_intent_variant_reads_as_its_base_palette_with_its_swatches(reported):
    from autolux.lighting.palettes import PALETTES
    from autolux.simulate.visualizer_app import PALETTE_SWATCH_SLOTS, _build_show_status, _rgb_css

    snapshot = _with_base_palette({'palette': {'name': reported, 'pinned': True}})
    assert snapshot['palette'] == {'name': 'techno', 'pinned': True}
    techno = next(palette for palette in PALETTES if palette.name == 'techno')
    colors = next(
        item
        for item in _build_show_status(snapshot, now=0.0)
        if item.children[0].children == 'Colors'
    )
    value = colors.children[1].children
    assert [p.style['background'] for p in value if getattr(p, 'className', None) == 'dot'] == [
        _rgb_css(getattr(techno, slot)) for slot in PALETTE_SWATCH_SLOTS
    ]
    assert 'techno' in value


def test_the_status_line_reads_health_track_autoloop_colors_bpm_render_fps():
    from autolux.simulate.visualizer_app import MEDIA_SESSION_SOURCE, _build_show_status

    track = {'source': MEDIA_SESSION_SOURCE, 'title': 'Windowlicker', 'artist': 'Aphex Twin'}
    captions = [
        [item.children[0].children for item in _build_show_status(snapshot, now=0.0)]
        for snapshot in ({'track': track, 'special': {'name': 'sparkle', 'group': 'fx'}}, {})
    ]
    assert captions == [
        ['Health', 'Track', 'Autoloop', 'Colors', 'FX', 'BPM', 'Render FPS'],
        ['Health', 'Autoloop', 'Colors', 'FX', 'BPM', 'Render FPS'],
    ]
    values = {
        item.children[0].children: item.children[1].children
        for item in _build_show_status({}, now=0.0)
    }
    assert (values['BPM'], values['Render FPS']) == ('—', '—')


def test_the_render_rate_is_the_measured_rate_over_the_last_three_seconds():
    from autolux.simulate.visualizer_app import render_rate_text, render_window

    def sample(at, rendered, missed):
        return {
            'at': at,
            'starfire_measured_render_frames_per_second': rendered,
            'starfire_missed_render_deadlines_per_second': missed,
        }

    steady = [sample(at, 40.0, 0.0) for at in range(100, 106)]
    assert render_rate_text(render_window(steady, now=105.5)[0]) == '40.0'
    # A late frame loop renders fewer than 40 per second; the slow sample at 101 has aged out.
    rough = [sample(100, 40.0, 0.0), sample(101, 10.0, 30.0), sample(103, 40.0, 0.0)]
    rough += [sample(104, 38.6, 1.4), sample(105, 38.6, 1.4)]
    fps, missed = render_window(rough, now=105.2)
    assert (render_rate_text(fps), missed) == ('38.6', 3)
    assert render_window([], now=0.0) == (None, 0)
    assert render_rate_text(render_window(steady, now=200.0)[0]) == '—'


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_overlay_banner_counts_the_looks_and_a_clear_hides_it_at_once():
    from autolux.simulate.visualizer_app import OVERLAY_BANNER_JS

    overlay = {'mode': 'static', 'static': {'looks': {'wash': {}, 'strip': {}}}}
    shown = _run_control_js(
        f'const banner = {OVERLAY_BANNER_JS};\n'
        f'const overlay = {json.dumps(overlay)};\n'
        'const before = banner(overlay);\n'
        'control.hold({mode: "auto"});\n'
        "console.log(JSON.stringify([before, banner(overlay), banner({mode: 'auto'})]));"
    )
    assert shown == [
        [{}, 'Static overlay active · 2 looks'],
        [{'display': 'none'}, ''],
        [{'display': 'none'}, ''],
    ]


@pytest.mark.parametrize('trigger', ['static-clear', 'static-banner-clear'])
def test_both_clear_buttons_send_one_clear_all_command(static_show, trigger):
    poller, commands = static_show
    app = build_app(poller, direct_control=False)
    state = {'mode': 'static', 'static': {'targets': ['all'], 'looks': {'all': {}}}}
    _run_callback(
        app,
        'static-status.children',
        {trigger: 1, 'static-target': 'all', 'control-state': state},
        f'{trigger}.n_clicks',
    )
    assert commands.get_nowait() == {'action': 'static', 'kind': 'clear'}
    assert commands.empty()


def test_the_autoloop_item_names_the_overlay_targets_while_it_is_active():
    from autolux.simulate.visualizer_app import _build_show_status

    snapshot = {'mode': 'static', 'static': {'looks': {'wash': {}, 'strip': {}}}}
    items = {item.children[0].children: item for item in _build_show_status(snapshot, now=0.0)}
    assert items['Autoloop'].children[1].children == 'Static · wash, strip'


def test_a_remapped_loop_shows_what_plays_and_keeps_the_real_intent_in_its_tag():
    from autolux.simulate.visualizer_app import _build_show_status

    def autoloop(selection):
        snapshot = {
            'intent': 'drop',
            'current_effect': {'name': 'breakdown/wide_space', 'selection': selection},
        }
        items = {item.children[0].children: item for item in _build_show_status(snapshot, 0.0)}
        _, name, tag = items['Autoloop'].children[1].children
        return name, tag.children

    calm = {'intent': 'drop', 'look': 'breakdown', 'level': 'calm'}
    assert autoloop(calm) == ('Wide Space', 'drop · calm')
    assert autoloop({'intent': 'drop', 'look': 'drop', 'level': 'balanced'}) == (
        'Wide Space',
        'drop',
    )


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_nothing_reaches_the_show_before_the_operator_touches_the_page():
    sent = _run_control_js(
        """
window.autoluxControlConfig = {direct: true, bridgePort: 8766};
globalThis.location = {protocol: 'http:', hostname: 'autolux.local'};
console.warn = () => {};
const urls = [];
globalThis.fetch = async (url) => {
    urls.push(url);
    return {ok: true, status: 202, json: async () => ({accepted: 'static'})};
};
(async () => {
    const early = await control.dispatch(control.build.static('static-colour', 'all', '#00ff00'));
    control.operatorActs();
    const late = await control.dispatch(control.build.static('static-colour', 'all', '#00ff00'));
    console.log(JSON.stringify({early, late, urls}));
})();
"""
    )
    assert sent == {
        'early': {'dropped': 'no operator input yet'},
        'late': {'accepted': 'static'},
        'urls': ['http://autolux.local:8766/control'],
    }


def test_loading_the_page_and_its_first_poll_command_nothing():
    source = _Recorder()
    app = build_app(source)
    assert app.server.test_client().get('/').status_code == 200
    refresh = next(key for key in app.callback_map if key.startswith('..sync.data'))
    gate = {'tab': 'control', 'metrics': True, 'timeline': True}
    _run_callback(app, refresh, {'gate': gate}, 'gate.data')
    _run_callback(
        app, 'control-mode-synced.data', {'control-state': {'mode': 'static'}}, 'control-state.data'
    )
    assert source.commands == []
    # In the browser path the Static tab's controls only reach the show through control.js.
    assert 'static-status.children' not in app.callback_map


def test_clear_all_leads_the_static_action_row_in_the_quiet_style():
    app = build_app(_Recorder())
    row = app.layout['static-control'].children[0]
    clear = row.children[0]
    assert (clear.id, clear.className, clear.disabled) == ('static-clear', 'ghost-button', True)
    assert 'static-clear.disabled' in app.callback_map


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_clear_all_is_dimmed_while_the_overlay_holds_nothing():
    from autolux.simulate.visualizer_app import CLEAR_GATE_JS

    rendered = _run_control_js(
        f'const gate = {CLEAR_GATE_JS};\n'
        "const states = [{mode: 'static'}, {mode: 'auto'}, null];\n"
        'console.log(JSON.stringify(states.map((state) => gate(state))));'
    )
    assert rendered == [False, True, True]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_banner_counts_operator_looks_not_the_dark_under_the_blacklight():
    from autolux.simulate.visualizer_app import OVERLAY_BANNER_JS

    looks = {'all': {'colour': None, 'dimmer': 0.0, 'uv': False}, 'wash': {'colour': '#ff0000'}}
    shown = _run_control_js(
        f'const banner = {OVERLAY_BANNER_JS};\n'
        f"console.log(JSON.stringify(banner({{mode: 'static', static: {{looks: {json.dumps(looks)}}}}})));"
    )
    assert shown == [{}, 'Static overlay active · 1 look']


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_following_the_music_greys_the_segments_but_keeps_the_chosen_level_lit():
    from autolux.simulate.visualizer_app import FOLLOW_SYNC_JS

    rendered = _run_control_js(
        f'const follow = {FOLLOW_SYNC_JS};\n'
        'const states = [{intensity_auto: true}, {intensity_auto: false}, null];\n'
        'console.log(JSON.stringify(states.map((state) => follow(state))));'
    )
    assert rendered == [['active', 'following'], ['', ''], ['', '']]
    app = build_app(_Recorder())
    # The segments stay clickable (a pick turns auto off) and are still fed the show's level.
    writers = [key for key in app.callback_map if 'control-intensity-set.disabled' in key]
    assert len(writers) == 1
    assert 'control-intensity-set.className' in ' '.join(app.callback_map)


def test_restored_or_polled_settings_send_nothing_and_one_edit_sends_one_command():
    from autolux.simulate.visualizer_app import settings_text

    source = _Recorder()
    app = build_app(source)
    output = next(key for key in app.callback_map if key.startswith('..control-status.children'))
    # More digits than the form shows: a resend of the rounded text would change the show.
    settings = ShowSettings(quiet_brightness=0.1234567, crossfade_beats=1.0).to_dict()
    text = settings_text(settings)
    state = {'settings': settings, 'settings_text': text, 'master_brightness': 1.0}
    shown = {f'control-{name}': text[name] for name in FORM_SETTINGS}
    for name in FORM_SETTINGS:
        _run_callback(app, output, {**shown, 'control-state': state}, f'control-{name}.value')
    assert source.commands == []

    edited = {**shown, 'control-crossfade_beats': '2'}
    _run_callback(app, output, {**edited, 'control-state': state}, 'control-crossfade_beats.value')
    assert source.commands == [{'action': 'settings', 'settings': {'crossfade_beats': 2.0}}]


RED = {'red': 1.0, 'green': 0.0, 'blue': 0.0}
BLACK = {'red': 0.0, 'green': 0.0, 'blue': 0.0}


@pytest.mark.skipif(NODE is None, reason='needs node to run the overlay banner')
@pytest.mark.parametrize(
    ('chip', 'target', 'values'),
    [
        ('static-preset-off', 'fx', {'intensity': 0.0}),
        ('static-preset-off', 'movers', {'intensity': 0.0}),
        ('static-preset-red', 'strip', {'colour': RED, 'intensity': 1.0}),
        (
            'static-preset-uv',
            'wash',
            {'colour': BLACK, 'intensity': 1.0, 'attributes': {'ColorAdd_UV': 255.0}},
        ),
    ],
)
async def test_a_preset_on_a_group_target_changes_only_that_group(
    static_show, chip, target, values
):
    from autolux.simulate.visualizer_app import OVERLAY_BANNER_JS

    poller, commands = static_show
    assert poller.control(static_control_command(chip, target, None)) == {'accepted': 'static'}
    light, _, _, lighting = static_engine()
    apply_static(light, commands.get_nowait())
    await light.sync_static()
    assert light.mode == 'static'
    # Every other group keeps what it showed: no dark floor, no whole-rig look, no blackout.
    assert overrides(lighting) == [
        {
            'kind': 'set_override',
            'value': {
                'id': f'static_{target}',
                'target': {'group': target},
                'values': values,
                'priority': STATIC_GROUP_PRIORITY,
            },
        }
    ]
    assert not light.static_looks.blackout
    state = {'mode': 'static', 'static': light.static_looks.state()}
    banner = _run_control_js(
        f'const banner = {OVERLAY_BANNER_JS};\n'
        f'console.log(JSON.stringify(banner({json.dumps(state)})));'
    )
    assert banner == [{}, 'Static overlay active · 1 look']


def test_the_auto_tab_has_fx_auto_and_off_chips_and_fx_defaults_to_auto():
    from autolux.simulate.visualizer_app import FX_AUTO_IDS, FX_SYNC_JS

    app = build_app(_Recorder())
    assert [app.layout[name].children for name in FX_AUTO_IDS] == ['Auto', 'Off']
    states = [{'settings': {}}, {'settings': {}, 'fx_auto': False}, None]
    if NODE is not None:
        synced = _run_control_js(
            f'const sync = {FX_SYNC_JS};\n'
            f'console.log(JSON.stringify({json.dumps(states)}.map((s) => sync(s))));'
        )
        assert synced == [['active', ''], ['', 'active'], ['', '']]
    off = control_command('control-fx-off', False, None, ())
    assert off == {'action': 'fx_auto', 'enabled': False}
    # Sent even when the last poll says the show has it: that poll may be stale.
    assert control_command('control-fx-auto', False, None, ()) == {
        'action': 'fx_auto',
        'enabled': True,
    }


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_an_fx_chip_click_dispatches_whatever_the_page_last_polled():
    # No poll yet, a state from before the chips, and one that already reports the value.
    states = [None, {'settings': {}}, {'settings': {'fx_auto': True}, 'fx_auto': True}]
    clicks = [(state, chip) for state in states for chip in ('control-fx-off', 'control-fx-auto')]
    result = _run_control_js(
        f'''
window.autoluxControlConfig = {{direct: true, bridgePort: 8766}};
globalThis.location = {{protocol: 'http:', hostname: 'autolux.local'}};
const sent = [];
const classes = [];
globalThis.fetch = async (url, options) => {{
    sent.push(JSON.parse(options.body));
    return {{ok: true, status: 202, json: async () => ({{accepted: 'fx_auto'}})}};
}};
const clicked = {DIRECT_AUTO_JS};
window.dash_clientside = {{
    no_update: null,
    set_props: (id, props) => classes.push([id, props.className]),
}};
(async () => {{
    control.operatorActs();
    for (const [state, chip] of {json.dumps(clicks)}) {{
        control.press('fx_auto', chip === 'control-fx-auto');
        window.dash_clientside.callback_context = {{triggered: [{{prop_id: chip + '.n_clicks'}}]}};
        clicked(null, null, null, null, null, 1, 1, state);
        await new Promise((resolve) => setTimeout(resolve, 0));
    }}
    console.log(JSON.stringify({{sent, classes, shown: control.view({{fx_auto: false}})}}));
}})();
'''
    )
    assert result['sent'] == [{'action': 'fx_auto', 'enabled': on} for on in (False, True)] * 3
    assert result['classes'][:2] == [['control-fx-auto', ''], ['control-fx-off', 'active']]
    assert result['shown'] == {'fx_auto': True}, 'the last click holds until the show reports it'


def test_the_fx_effects_block_shows_one_bar_per_group_readiest_first():
    from autolux.simulate.visualizer_app import _build_cooldowns

    app = build_app(_Recorder())
    with pytest.raises(KeyError):
        app.layout['command-diagnostics']
    assert app.layout['special-cooldowns'] is not None

    def cooldown(group, last, total, remaining):
        return {
            'group': group,
            'last': last,
            'cooldown_seconds': total,
            'remaining_seconds': remaining,
            'ready': remaining == 0,
        }

    rows = _build_cooldowns(
        {
            'special_cooldowns': [
                cooldown('fx', 'cold_drift', 240, 179.5),
                cooldown('all', 'strobe_burst', 60, 0.0),
                cooldown('movers', 'spin', 90, 45.0),
                cooldown('wash', None, 0, 0.0),
            ]
        }
    )

    def shown(row):
        head, track = row.children
        return (
            head.children[0].children,
            head.children[1].children,
            track.children.style['width'],
            'ready' in row.className,
        )

    assert [shown(row) for row in rows] == [
        ('all · Strobe burst', 'ready', '100.0%', True),
        ('wash', 'ready', '100.0%', True),
        ('movers · Spin', '45 s', '50.0%', False),
        ('fx · Cold drift', '180 s', '25.2%', False),
    ]
    # The ticker in control.js reads these to count down between polls.
    props = rows[3].to_plotly_json()['props']
    assert (props['data-total'], props['data-remaining']) == (240, 179.5)
    assert (props['data-group'], props['data-last'], props['data-ready']) == (
        'fx',
        'cold_drift',
        'false',
    )
    # Every bar paints in the same page colour: no per-group tint on any row.
    assert all('style' not in row.to_plotly_json()['props'] for row in rows)


def test_an_fx_effects_bar_fills_to_full_in_a_colour_the_page_defines():
    from autolux.simulate.visualizer_app import _build_cooldowns

    def fill(total, remaining):
        entry = {
            'group': 'fx',
            'last': 'drift',
            'cooldown_seconds': total,
            'remaining_seconds': remaining,
            'ready': remaining <= 0,
        }
        [row] = _build_cooldowns({'special_cooldowns': [entry]})
        return row.children[1].children.style['width']

    # Empty as the group fires, 1 - remaining/cooldown on the way, full once ready.
    assert [fill(240, r) for r in (240, 180, 60, 0)] == ['0.0%', '25.0%', '75.0%', '100.0%']

    # The block sits outside #show-control, so every token the fill and the fire flash paint
    # with must be set on .cooldowns or the row itself; an undefined var() would leave the bar
    # transparent.
    css = (CONTROL_JS.parent / 'control.css').read_text(encoding='utf8')
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
    rules = dict(re.findall(r'([^{}]+?)\s*\{([^{}]*)\}', css))
    scopes = [
        body
        for selector, body in rules.items()
        if {'.cooldowns', '.cooldown-row'} & {part.strip() for part in selector.split(',')}
    ]
    defined = {name for body in scopes for name in re.findall(r'(--[\w-]+)\s*:', body)}
    [paint] = [body for selector, body in rules.items() if selector.strip() == '.cooldown-fill']
    background = re.search(r'background:\s*([^;]+);', paint).group(1)
    assert 'transparent' not in background
    assert 'var(' in background
    assert set(re.findall(r'var\((--[\w-]+)\)', background)) <= defined
    # The fire pulse paints only with tokens the block itself sets.
    pulse = re.search(r'@keyframes cooldown-fire\s*\{(.*?)\n\}', css, re.DOTALL).group(1)
    assert set(re.findall(r'var\((--[\w-]+)\)', pulse)) <= defined


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_fx_effects_ticker_counts_a_bar_down_from_the_last_poll():
    from autolux.simulate.visualizer_app import _build_cooldowns

    def rendered(total, remaining):
        entry = {
            'group': 'fx',
            'last': 'drift',
            'cooldown_seconds': total,
            'remaining_seconds': remaining,
            'ready': remaining <= 0,
        }
        [row] = _build_cooldowns({'special_cooldowns': [entry]})
        head, track = row.children
        return {
            'fill': track.children.style['width'],
            'caption': head.children[1].children,
            'ready': 'ready' in row.className,
        }

    # (total, remaining at the poll, seconds since the poll)
    cases = [(240, 180.0, 0.0), (240, 180.0, 59.5), (90, 10.0, 10.0), (90, 10.0, 30.0), (0, 0, 0)]
    calls = ', '.join(f'control.cooldownView({t}, {r}, {e})' for t, r, e in cases)
    ticked = _run_control_js('console.log(JSON.stringify([' + calls + ']));')
    # Ticking e seconds after a poll shows what a poll e seconds later would have rendered.
    assert ticked == [rendered(t, max(0.0, r - e)) for t, r, e in cases]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_an_fx_effects_bar_flashes_once_when_its_group_fires_and_not_on_a_routine_poll():
    def poll(last, remaining, ready=False):
        return {'last': last, 'remaining': remaining, 'ready': ready}

    # (previous poll of the group, this poll)
    cases = [
        (None, poll('drift', 240)),  # first sight of the group: no history, no flash
        (poll('drift', 180), poll('drift', 179)),  # counting down
        (poll('drift', 0, True), poll('drift', 0, True)),  # staying ready
        (poll('drift', 0, True), poll('drift', 240)),  # ready group fires the same special
        (poll('drift', 30), poll('drift', 240)),  # fired again before it was ready
        (poll('drift', 0, True), poll('spin', 90)),  # a different special fired
    ]
    calls = ', '.join(
        f'control.cooldownFired({json.dumps(previous)}, {json.dumps(polled)})'
        for previous, polled in cases
    )
    fired = _run_control_js('console.log(JSON.stringify([' + calls + ']));')
    assert fired == [False, False, False, True, True, True]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_fx_effects_ticker_holds_the_flash_class_through_the_pulse_after_a_fire():
    # A fake document with one bar and a hand-driven clock and setInterval.
    body = """
const row = {
    dataset: {group: 'fx', last: 'drift', total: '240', remaining: '0', ready: 'true'},
    classes: new Set(['cooldown-row', 'ready']),
    querySelector: () => null,
};
row.classList = {toggle: (name, on) => (on ? row.classes.add(name) : row.classes.delete(name))};
let now = 0;
let tick = null;
Object.defineProperty(globalThis, 'performance', {value: {now: () => now}});
globalThis.setInterval = (fn) => { tick = fn; };
globalThis.document = {querySelectorAll: () => [row], addEventListener: () => {}};
"""
    script = (
        body
        + 'globalThis.window = {};\n'
        + CONTROL_JS.read_text(encoding='utf8')
        + """
const seen = [];
const at = (ms) => { now = ms; tick(); seen.push(row.classes.has('flash')); };
at(0); at(100);
Object.assign(row.dataset, {remaining: '240', ready: 'false'});  // the poll after a fire
at(200); at(600); at(900);
row.dataset.remaining = '239';  // the next routine poll
at(1000);
console.log(JSON.stringify(seen));
"""
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [False, False, True, True, False, False]


# Just enough DOM for control.js's press listeners: elements answer closest() by id or as a
# level's label, events reach the capture listeners as the browser hands them over, and the
# clock only moves by hand.
FAKE_DOM = """
const listeners = [];
let now = 0;
Object.defineProperty(globalThis, 'performance', {value: {now: () => now}});
globalThis.setInterval = () => {};
globalThis.Element = class {};
globalThis.document = {
    addEventListener: (type, listener) => listeners.push([type, listener]),
    querySelectorAll: () => [],
};
const element = (id, props) => Object.assign(new Element(), {id, ...props});
const button = (id) => element(id, {
    classList: {contains: () => false},
    closest(selector) {
        return selector.split(',').some((s) => s.trim() === '#' + this.id) ? this : null;
    },
});
const level = (value, chosen, disabled = false) => element('', {
    getAttribute: (name) => (name === 'aria-selected' ? String(chosen) : null),
    querySelector: () => ({value, matches: (s) => s === ':disabled' && disabled}),
    closest(selector) {
        return selector === '#control-intensity label' ? this : null;
    },
});
const deliver = (type, target, extra = {}) => listeners
    .filter(([listened]) => listened === type)
    .forEach(([, listener]) => listener({type, isTrusted: true, target, ...extra}));
const clickOn = (target, isTrusted = true) => deliver('click', target, {isTrusted});
const enterOn = (target) => deliver('keyup', target, {key: 'Enter'});
"""


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_only_a_recorded_click_sends_and_reruns_polls_and_stale_presses_send_nothing():
    body = f"""
window.autoluxControlConfig = {{direct: true, bridgePort: 8766}};
globalThis.location = {{protocol: 'http:', hostname: 'autolux.local'}};
const sent = [];
globalThis.fetch = async (url, options) => {{
    sent.push(JSON.parse(options.body));
    return {{ok: true, status: 202, json: async () => ({{}})}};
}};
const run = {DIRECT_AUTO_JS};
window.dash_clientside = {{no_update: null, set_props: () => {{}}}};
const fire = (prop, intensity, state) => {{
    window.dash_clientside.callback_context = {{triggered: [{{prop_id: prop}}]}};
    run(null, null, null, intensity, 1, 1, 1, state);
    return new Promise((resolve) => setTimeout(resolve, 0));
}};
const log = [];
const step = async (name, act) => {{
    const before = sent.length;
    await act();
    log.push([name, sent.slice(before)]);
}};
(async () => {{
    control.operatorActs();
    // The poll from before the clicks, and the one that reports them.
    const stale = {{settings: {{intensity: 1.0}}, intensity_auto: false, fx_auto: true}};
    const echoed = {{settings: {{intensity: 0.7}}, intensity_auto: false, fx_auto: false}};
    await step('click Off', () => {{
        clickOn(button('control-fx-off'));
        return fire('control-fx-off.n_clicks', 1.0, stale);
    }});
    await step('click balanced', () => {{
        clickOn(level('0.7', false));
        return fire('control-intensity.value', 0.7, stale);
    }});
    // The sync wrote the radio from the older poll; the run already sees the newer one.
    await step('poll writes the old level', () => fire('control-intensity.value', 1.0, echoed));
    await step('poll echoes the new level', () => fire('control-intensity.value', 0.7, echoed));
    // Dash running the callback with chip inputs no click changed.
    await step('re-run Auto', () => fire('control-fx-auto.n_clicks', 0.7, echoed));
    await step('re-run Off', () => fire('control-fx-off.n_clicks', 0.7, echoed));
    await step('re-run Intensity Auto', () => fire('control-intensity-auto.n_clicks', 0.7, echoed));
    await step('click the level already chosen', () => {{
        clickOn(level('0.7', true));
        return fire('control-intensity.value', 0.7, echoed);
    }});
    await step('untrusted click', () => {{
        clickOn(button('control-fx-auto'), false);
        return fire('control-fx-auto.n_clicks', 0.7, echoed);
    }});
    // Levels still in the disabled fieldset before the first settings poll.
    await step('click a disabled level, then the first poll', () => {{
        clickOn(level('0.3', false, true));
        return fire('control-intensity.value', 0.3, echoed);
    }});
    // A press whose own run never came, then a poll's run over a second later.
    await step('stuck press, then a poll', () => {{
        clickOn(level('1', false));
        now += 1001;
        return fire('control-intensity.value', 1.0, echoed);
    }});
    await step('Enter on a level', () => {{
        enterOn(level('0.3', false));
        return fire('control-intensity.value', 0.3, echoed);
    }});
    await step('click Intensity Auto', () => {{
        clickOn(button('control-intensity-auto'));
        return fire('control-intensity-auto.n_clicks', 0.7, echoed);
    }});
    console.log(JSON.stringify(log));
}})();
"""
    script = (
        'globalThis.window = {};\n'
        + FAKE_DOM
        + CONTROL_JS.read_text(encoding='utf8')
        + '\nconst control = window.autoluxControl;\n'
        + body
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [
        ['click Off', [{'action': 'fx_auto', 'enabled': False}]],
        ['click balanced', [{'action': 'intensity', 'level': 0.7}]],
        ['poll writes the old level', []],
        ['poll echoes the new level', []],
        ['re-run Auto', []],
        ['re-run Off', []],
        ['re-run Intensity Auto', []],
        ['click the level already chosen', []],
        ['untrusted click', []],
        ['click a disabled level, then the first poll', []],
        ['stuck press, then a poll', []],
        ['Enter on a level', [{'action': 'intensity', 'level': 0.3}]],
        ['click Intensity Auto', [{'action': 'intensity_auto', 'enabled': True}]],
    ]
