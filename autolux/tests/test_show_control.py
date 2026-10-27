import json
import queue
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autolux.lib.engine.effect_definitions import (
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    ShowSettings,
)
from autolux.lib.clock import VirtualClock
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.main import apply_static
from autolux.lib.ui_bridge import SnapshotServer
from autolux.lighting.palettes import PALETTE_NAMES
from autolux.simulate.visualizer_app import (
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
    setting_text,
    static_control_command,
)
from autolux.tests.test_static_mode import overrides, static_engine


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


def test_a_settings_change_from_an_old_panel_still_carrying_warmth_is_accepted(show):
    poller, commands = show
    command = control_command(
        'control-crossfade_beats', False, None, ('30', '0.5', '2', '20', '6', '3', '3')
    )
    command['settings']['palette_temperature_bias'] = -0.5
    assert poller.control(command) == {'accepted': 'settings'}
    assert 'palette_temperature_bias' not in commands.get_nowait()['settings'].to_dict()


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
        ('static-preset-uv', 'wash', None, {'action': 'static', 'kind': 'preset', 'preset': 'uv'}),
        ('static-blacklight', 'wash', None, {'action': 'static', 'kind': 'blacklight'}),
        ('static-strobe', 'wash', None, {'action': 'static', 'kind': 'strobe'}),
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
    assert looks['static_floor']['target'] == 'all'
    assert looks['static_floor']['values'] == {'intensity': 0.0}


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
        ({'action': 'static', 'kind': 'strobe', 'enabled': 'on'}, 'true or false'),
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
    command = {
        'action': 'static',
        'kind': 'special_fx',
        'group': 'fx',
        'colour_slot': 'cold',
        'energy': 0.5,
        'speed': 0.25,
        'pulse_beats': 2,
    }
    assert poller.control(command) == {'accepted': 'static'}
    assert commands.get_nowait() == command
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
    options, blacklight_off, uv_off = _static_targets({'mode': 'static', 'static': venue})
    assert [option['value'] for option in options] == venue['targets']
    assert options[0]['label'] == 'All'
    assert blacklight_off and uv_off
    uv_rig = {'targets': ['all', 'uv_bar'], 'blacklight': True}
    assert _static_targets({'mode': 'static', 'static': uv_rig})[1:] == [False, False]


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


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_page_sends_the_strobe_state_it_wants_so_a_retry_cannot_flip_it(static_show):
    poller, commands = static_show
    start, stop = _run_control_js(
        "console.log(JSON.stringify([control.build.static('static-strobe', 'all', false),"
        " control.build.static('static-strobe', 'all', true)]));"
    )
    for command, enabled in ((start, True), (stop, False)):
        assert poller.control(command) == {'accepted': 'static'}
        assert commands.get_nowait() == {'action': 'static', 'kind': 'strobe', 'enabled': enabled}


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


FX_STATE = {
    **STATIC_STATE,
    'special_groups': {'fx': ['colour', 'pulse', 'speed'], 'wash': ['colour'], 'par': []},
}
FX_SEGMENTS = {'static-fx-energy': 0.9, 'static-fx-speed': 0.2, 'static-fx-pulse': 1}
# (trigger, static target, control-state, the payload the show must receive)
FX_CASES = [
    (
        'static-fx-cold',
        'fx',
        FX_STATE,
        {'group': 'fx', 'colour_slot': 'cold', 'energy': 0.9, 'speed': 0.2, 'pulse_beats': 1},
    ),
    (
        'static-fx-primary',
        'wash',
        FX_STATE,
        {
            'group': 'wash',
            'colour_slot': 'primary',
            'energy': 0.9,
            'speed': 0.0,
            'pulse_beats': None,
        },
    ),
    (
        'static-fx-speed',
        'all',
        {**FX_STATE, 'static': {**FX_STATE['static'], 'special_fx': {'colour_slot': 'accent'}}},
        {'group': 'all', 'colour_slot': 'accent', 'energy': 0.9, 'speed': 0.2, 'pulse_beats': 1},
    ),
]


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_static_fx_chips_send_the_special_dict_from_python_and_the_browser_alike(static_show):
    from autolux.simulate.visualizer_app import fx_axes

    source = _Recorder()
    app = build_app(source, direct_control=False)
    static_output = 'static-status.children'
    for trigger, target, state, _ in FX_CASES:
        values = {**FX_SEGMENTS, 'control-state': state, 'static-target': target}
        kind = 'value' if trigger == 'static-fx-speed' else 'n_clicks'
        _run_callback(app, static_output, values, f'{trigger}.{kind}')
    _run_callback(app, static_output, {'control-state': FX_STATE}, 'static-fx-clear.n_clicks')
    expected = [{'action': 'static', 'kind': 'special_fx', **payload} for *_, payload in FX_CASES]
    expected.append({'action': 'static', 'kind': 'special_fx_clear'})
    assert source.commands == expected

    calls = [
        f'control.build.fx({json.dumps(trigger)}, {json.dumps(target)}, 0.9, 0.2, 1, '
        f'{json.dumps(fx_axes(state["special_groups"], target))}, '
        f'{json.dumps((state["static"].get("special_fx") or {}).get("colour_slot"))})'
        for trigger, target, state, _ in FX_CASES
    ]
    calls.append("control.build.fx('static-fx-clear', 'all', 0.6, 0.5, 0, [], null)")
    assert _run_control_js(f'console.log(JSON.stringify([{", ".join(calls)}]));') == expected

    poller, commands = static_show
    for command in expected:
        assert poller.control(command) == {'accepted': 'static'}
        assert commands.get_nowait() == command


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_fx_row_is_disabled_with_a_note_for_a_target_that_cannot_render_one():
    from autolux.simulate.visualizer_app import FX_GATE_JS

    gates = _run_control_js(
        f'const gate = {FX_GATE_JS};\n'
        f'const state = {json.dumps(FX_STATE)};\n'
        "console.log(JSON.stringify([gate('fx', state), gate('wash', state), gate('par', state),"
        " gate('all', state), gate('all', {special_groups: {}})]));"
    )
    assert gates == [
        [False, False, False, ''],
        [False, True, True, ''],
        [True, True, True, 'par cannot render FX'],
        [False, False, False, ''],
        [True, True, True, 'No group cannot render FX'],
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
    assert 'FX' not in items(buffer.snapshot())
    assert items(buffer.snapshot())['Strobe'] == 'ready'
    buffer.set_special('cold_spin', 'fx')
    buffer.mark_intense()
    clock.advance(19.0)
    shown = items(buffer.snapshot())
    assert shown['FX'] == 'Cold spin · fx'
    assert shown['Strobe'] == 'in 41 s'
    labels = [item.children[0].children for item in _build_show_status(buffer.snapshot(), 0.0)]
    assert labels.index('FX') == labels.index('Colors') + 1
    static = {'static': {'special_fx': {'group': 'wash', 'colour_slot': 'cold'}}}
    assert items(static)['FX'] == 'Static · wash'


def test_the_config_rig_area_lists_each_groups_fx_capabilities():
    from autolux.simulate.visualizer_app import special_groups_view

    line = special_groups_view({'wash': ['colour'], 'fx': ['colour', 'pulse', 'speed']})
    assert [getattr(part, 'children', part) for part in line] == [
        'FX',
        '  ',
        'fx',
        ' ',
        'colour · pulse · speed',
        '  ',
        'wash',
        ' ',
        'colour',
    ]
    assert special_groups_view({}) == ['FX: Starfire reported no groups']


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_a_clicked_fx_chip_lights_and_a_later_segment_change_refires_it_in_the_browser():
    from autolux.simulate.visualizer_app import FX_DIRECT_JS, FX_SLOT_IDS

    chips = list(FX_SLOT_IDS)
    blank = [''] * len(chips)
    lit = ['active' if chip == 'static-fx-cold' else '' for chip in chips]
    clicks = [None] * (len(chips) + 3)
    sent, cold_lit, refired_lit, idle = _run_control_js(
        'const sent = [];\n'
        'control.dispatch = (command) => { sent.push(command); return Promise.resolve({}); };\n'
        'window.dash_clientside = {no_update: null, set_props: () => {},'
        ' callback_context: {triggered: []}};\n'
        f'const fx = {FX_DIRECT_JS};\n'
        f'const state = {json.dumps(FX_STATE)};\n'
        'const run = (trigger, segments, classes) => {\n'
        '  window.dash_clientside.callback_context.triggered = [{prop_id: trigger}];\n'
        f'  return fx(...{json.dumps(clicks)}, ...segments, "fx", ...classes, state);\n'
        '};\n'
        f'const a = run("static-fx-cold.n_clicks", [0.6, 0.5, 0], {json.dumps(blank)});\n'
        f'const b = run("static-fx-energy.value", [0.9, 0.5, 2], {json.dumps(lit)});\n'
        f'const c = run("static-fx-speed.value", [0.9, 0.9, 2], {json.dumps(blank)});\n'
        'console.log(JSON.stringify([sent, a, b, c]));'
    )
    special = {'action': 'static', 'kind': 'special_fx', 'group': 'fx', 'colour_slot': 'cold'}
    assert sent == [
        {**special, 'energy': 0.6, 'speed': 0.5, 'pulse_beats': None},
        {**special, 'energy': 0.9, 'speed': 0.5, 'pulse_beats': 2},
    ]
    assert cold_lit == refired_lit == lit
    assert idle == [None] * len(chips), 'a segment change with no FX lit sends nothing'


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
        ['Health', 'Autoloop', 'Colors', 'BPM', 'Render FPS'],
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
    const early = await control.dispatch(control.build.static('static-colour', 'all', undefined, '#00ff00'));
    control.operatorActs();
    const late = await control.dispatch(control.build.static('static-colour', 'all', undefined, '#00ff00'));
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


@pytest.mark.skipif(NODE is None, reason='needs node to run control.js')
def test_the_banner_counts_operator_looks_not_the_dark_under_the_blacklight():
    from autolux.simulate.visualizer_app import OVERLAY_BANNER_JS

    looks = {'all': {'colour': None, 'dimmer': 0.0, 'uv': False}, 'wash': {'colour': '#ff0000'}}
    shown = _run_control_js(
        f'const banner = {OVERLAY_BANNER_JS};\n'
        f"console.log(JSON.stringify(banner({{mode: 'static', static: {{looks: {json.dumps(looks)}}}}})));"
    )
    assert shown == [{}, 'Static overlay active · 1 look']
