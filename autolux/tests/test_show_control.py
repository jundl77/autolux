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
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.main import apply_static
from autolux.lib.ui_bridge import SnapshotServer
from autolux.lighting.palettes import PALETTE_NAMES
from autolux.simulate.visualizer_app import (
    INTENSITY_LEVELS,
    PALETTE_POLICY,
    PALETTE_SYNC_JS,
    STATIC_CHIP_IDS,
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
    output = next(key for key in app.callback_map if key.startswith('..static-status.children'))
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
    light.enter_static()
    apply_static(light, command)
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
        ({'action': 'static', 'kind': 'strobe', 'enabled': 'on'}, 'true or false'),
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
        static_control_command('static-colour', 'all', '#12345')


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


@pytest.mark.parametrize(('shown', 'picked'), [('auto', 'static'), ('static', 'auto')])
def test_picking_a_mode_tab_sends_that_mode_and_shows_it_at_once(show, shown, picked):
    poller, commands = show
    app = build_app(poller, direct_control=False)
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
    ('control-mode.value', 'static', AUTO_STATE, 'all', "build.mode('static')"),
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
    static_output = next(k for k in app.callback_map if k.startswith('..static-status.children'))
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
