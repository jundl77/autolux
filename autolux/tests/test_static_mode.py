import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.effect_controller import (
    DROP_ACCENT_PRIORITY,
    EffectController,
)
from autolux.lib.engine.effect_definitions import (
    PREFERRED_AUTOLOOPS,
    LightIntent,
)
from autolux.lib.engine.special_policy import Special, SpecialCapabilities
from autolux.lib.engine.static_mode import (
    HIGHLIGHT_ID,
    STATIC_FLOOR_PRIORITY,
    STATIC_GROUP_PRIORITY,
    STATIC_PRIORITY,
    STATIC_SPECIAL_ID,
    STATIC_SPECIAL_RELEASE_BEATS,
    StaticLooks,
    StaticRig,
)
from autolux.lighting.special_catalogue import discover_specials
from autolux.simulate.lighting_recorder import RecordingStarfireClient
from autolux.tests.test_light_engine import FakeDecoder, beat_at_phase, engine, selections, sent

ROOM = StaticRig(groups=('fx', 'movers', 'strip', 'wash'), uv_groups=('wash',))
CATALOGUE = discover_specials()


def static_engine(**options):
    light, queue, clock, lighting = engine(**options)
    light.static_looks = StaticLooks(
        lighting, ROOM, CATALOGUE, light.effect_controller.special_capabilities
    )
    return light, queue, clock, lighting


def overrides(lighting):
    return [
        e['command']
        for e in lighting.events
        if e['command']['kind'] in ('set_override', 'clear_override')
    ]


async def test_static_holds_the_running_loop_without_pausing_dimming_or_following_the_model():
    decoder = FakeDecoder()
    light, _, _, lighting = static_engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    lighting.events.clear()

    light.enter_static()
    assert light.mode == 'static'
    await beat_at_phase(light, decoder, 1, decision='drop')
    await beat_at_phase(light, decoder, 0)
    assert selections(lighting) == [], 'static mode must not follow the model'
    assert sent(lighting, 'set_paused', 'set_master', 'stop') == []
    assert [e['command']['kind'] for e in lighting.events].count('beat') == 2
    assert light.current_intent is LightIntent.DROP
    with pytest.raises(ValueError, match='static overlay is active'):
        light.next_autoloop()


async def test_a_static_look_reaches_starfire_at_once_above_automatic_accents():
    light, _, clock, lighting = static_engine()
    light.enter_static()
    clock.advance(3.0)
    light.static_looks.look('wash', colour=(1.0, 0.0, 0.0), dimmer=0.5)
    light.static_looks.look('wash', dimmer=0.2)
    assert sent(lighting, 'set_override')[-1] == (
        clock.monotonic(),
        {
            'kind': 'set_override',
            'value': {
                'id': 'static_wash',
                'target': {'group': 'wash'},
                'values': {'colour': {'red': 1.0, 'green': 0.0, 'blue': 0.0}, 'intensity': 0.2},
                'priority': STATIC_GROUP_PRIORITY,
            },
        },
    )
    assert STATIC_PRIORITY > DROP_ACCENT_PRIORITY


async def test_the_uv_preset_drives_the_wash_uv_full_with_rgb_off_and_a_colour_replaces_it():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.preset('uv')
    assert overrides(lighting)[-1]['value']['values'] == {
        'colour': {'red': 0.0, 'green': 0.0, 'blue': 0.0},
        'intensity': 1.0,
        'attributes': {'ColorAdd_UV': 255.0},
    }
    light.static_looks.look('all', colour=(1.0, 1.0, 1.0))
    wash, everything = overrides(lighting)[-2:]
    assert wash['value']['id'] == 'static_wash'
    assert 'attributes' not in wash['value']['values']
    assert everything['value']['target'] == 'all'


@pytest.mark.parametrize(
    ('target', 'uv', 'reason'),
    [('stage', None, 'unknown static target'), ('movers', True, 'no UV channel')],
)
async def test_looks_the_rig_cannot_render_are_refused_before_reaching_starfire(target, uv, reason):
    light, _, _, lighting = static_engine()
    light.enter_static()
    with pytest.raises(ValueError, match=reason):
        light.static_looks.look(target, dimmer=1.0, uv=uv)
    assert overrides(lighting) == []


async def test_the_overlay_state_follows_the_looks_and_runs_the_hooks_once():
    decoder = FakeDecoder()
    light, _, _, lighting = static_engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    assert (light.mode, light.static_looks.active) == ('auto', False)

    light.static_looks.look('wash', colour=(1.0, 0.0, 0.0))
    await light.sync_static()
    assert (light.mode, light.static_looks.active) == ('static', True)
    await beat_at_phase(light, decoder, 1, decision='drop')
    await beat_at_phase(light, decoder, 0)
    lighting.events.clear()
    light.static_looks.look('strip', colour=(0.0, 0.0, 1.0))
    await light.sync_static()
    assert selections(lighting) == [], 'a second look must not re-run the overlay hooks'

    light.static_looks.clear()
    await light.sync_static()
    assert (light.mode, light.static_looks.active) == ('auto', False)
    assert overrides(lighting)[-2:] == [
        {'kind': 'clear_override', 'id': 'static_wash'},
        {'kind': 'clear_override', 'id': 'static_strip'},
    ]


async def test_the_uv_preset_darkens_everything_but_the_blacklit_washes():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.preset('red')
    light.static_looks.preset('uv')
    assert [(c['kind'], c.get('id') or c['value']['id']) for c in overrides(lighting)] == [
        ('set_override', 'static_all'),
        ('clear_override', 'static_all'),
        ('set_override', 'static_all'),
        ('set_override', 'static_wash'),
    ]
    assert overrides(lighting)[-2]['value']['values'] == {'intensity': 0.0}


DARK_FLOOR = {
    'id': 'static_floor',
    'target': 'all',
    'values': {'intensity': 0.0},
    'priority': STATIC_FLOOR_PRIORITY,
}


async def test_off_darkens_the_rig_instead_of_revealing_the_held_automatic_look():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.preset('white')
    light.static_looks.preset('off')
    assert overrides(lighting)[-2:] == [
        {'kind': 'clear_override', 'id': 'static_all'},
        {'kind': 'set_override', 'value': DARK_FLOOR},
    ]
    assert STATIC_FLOOR_PRIORITY > DROP_ACCENT_PRIORITY
    lighting.events.clear()
    await light.leave_static()
    assert overrides(lighting) == [{'kind': 'clear_override', 'id': 'static_floor'}]


async def test_a_group_look_leaves_every_other_group_on_what_it_showed():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.look('wash', colour=(1.0, 0.0, 0.0))
    assert [c['value'] for c in overrides(lighting)] == [
        {
            'id': 'static_wash',
            'target': {'group': 'wash'},
            'values': {'colour': {'red': 1.0, 'green': 0.0, 'blue': 0.0}, 'intensity': 1.0},
            'priority': STATIC_GROUP_PRIORITY,
        },
    ]
    light.static_looks.look('movers', dimmer=0.5)
    assert overrides(lighting)[-1]['value']['target'] == {'group': 'movers'}
    assert 'static_floor' not in [c['value']['id'] for c in overrides(lighting)]


async def test_leaving_static_clears_every_look_and_applies_the_current_intent_on_the_downbeat():
    decoder = FakeDecoder()
    light, _, _, lighting = static_engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    light.enter_static()
    looks = light.static_looks
    light.static_looks.look('all', dimmer=0.4)
    looks.look('wash', colour=(0.0, 0.0, 0.0), dimmer=1.0, uv=True)
    looks.set_blackout(True)
    await beat_at_phase(light, decoder, 1, decision='drop')
    lighting.events.clear()

    await light.leave_static()
    assert light.mode == 'auto'
    assert sent(lighting, 'clear_override', 'set_blackout') == [
        (0.0, {'kind': 'clear_override', 'id': 'static_all'}),
        (0.0, {'kind': 'clear_override', 'id': 'static_wash'}),
        (0.0, {'kind': 'set_blackout', 'enabled': False}),
    ]
    await beat_at_phase(light, decoder, 2)
    assert selections(lighting) == [], 'the change waits for the downbeat'
    await beat_at_phase(light, decoder, 0)
    assert selections(lighting)[-1]['command']['autoloop'] == PREFERRED_AUTOLOOPS[LightIntent.DROP]


async def test_a_renderer_stop_or_new_song_keeps_the_static_look_and_blackout():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.look('all', colour=(0.0, 0.0, 1.0))
    light.static_looks.set_blackout(True)
    lighting.events.clear()
    light._show_sound_stop()
    light._show_sound_start()
    assert [c['kind'] for c in (e['command'] for e in lighting.events)] == [
        'stop',
        'set_override',
        'set_blackout',
    ]
    assert lighting.events[-1]['command'] == {'kind': 'set_blackout', 'enabled': True}


async def test_show_control_applies_static_commands_as_an_overlay_and_clears_it(tmp_path):
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lib.main import ShowControl

    buffer = EventBuffer()
    control = ShowControl(tmp_path / 'show.json', buffer)
    light, _, _, lighting = static_engine()
    for command in (
        {
            'action': 'static',
            'kind': 'look',
            'target': 'wash',
            'colour': (1.0, 0.5, 0.0),
            'dimmer': None,
        },
        {'action': 'static', 'kind': 'look', 'target': 'stage', 'colour': None, 'dimmer': 1.0},
        {'action': 'static', 'kind': 'blackout', 'enabled': True},
    ):
        control.commands.put_nowait(command)
    await control.apply_pending(light)
    snapshot = buffer.snapshot()
    assert snapshot['mode'] == 'static'
    assert snapshot['static'] == {
        'targets': ['all', 'fx', 'movers', 'strip', 'wash'],
        'blacklight': True,
        'blackout': True,
        'special_fx': None,
        # This rig reports no group capabilities, so no target takes a row.
        'fx_rows': {target: [] for target in ROOM.targets},
        'looks': {'wash': {'colour': '#ff8000', 'dimmer': 1.0, 'uv': False}},
    }

    control.commands.put_nowait({'action': 'static', 'kind': 'clear'})
    await control.apply_pending(light)
    snapshot = buffer.snapshot()
    assert (snapshot['mode'], snapshot['static']['looks'], snapshot['static']['blackout']) == (
        'auto',
        {},
        False,
    )
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': 'static_wash'}


async def test_a_group_look_outranks_all_and_a_colour_on_all_takes_the_groups_colour_back():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.look('wash', colour=(0.0, 0.0, 0.0), dimmer=1.0, uv=True)
    light.static_looks.look('all', dimmer=0.3)
    wash, everything = overrides(lighting)[-2:]
    assert (wash['value']['priority'], everything['value']['priority']) == (
        STATIC_GROUP_PRIORITY,
        STATIC_PRIORITY,
    )
    assert STATIC_GROUP_PRIORITY > STATIC_PRIORITY
    light.static_looks.look('all', colour=(1.0, 1.0, 1.0))
    wash, everything = overrides(lighting)[-2:]
    assert wash['value'] == {
        'id': 'static_wash',
        'target': {'group': 'wash'},
        'values': {'intensity': 1.0},
        'priority': STATIC_GROUP_PRIORITY,
    }
    assert everything['value']['values'] == {
        'colour': {'red': 1.0, 'green': 1.0, 'blue': 1.0},
        'intensity': 0.3,
    }


async def test_a_colour_look_carries_a_visible_dimmer_under_a_dark_loop():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.look('fx', colour=(0.0, 1.0, 0.0))
    assert overrides(lighting)[-1]['value']['values']['intensity'] == 1.0
    light.static_looks.look('fx', dimmer=0.4)
    light.static_looks.look('fx', colour=(0.0, 0.0, 1.0))
    assert overrides(lighting)[-1]['value']['values']['intensity'] == 0.4


async def test_standby_during_static_keeps_the_operator_look_and_resends_it():
    decoder = FakeDecoder()
    light, _, _, lighting = static_engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    light.enter_static()
    light.static_looks.look('all', colour=(1.0, 0.0, 0.0))
    lighting.events.clear()

    light.on_digital_silence()
    assert light.mode == 'static'
    assert overrides(lighting)[-1]['value']['id'] == 'static_all'
    light.on_signal_return()
    light.on_sound_start()
    await beat_at_phase(light, decoder, 0, decision='drop')
    standby = [s['command']['autoloop'] for s in selections(lighting)]
    assert PREFERRED_AUTOLOOPS[LightIntent.DROP] not in standby, 'static holds through standby'
    await light.leave_static()
    await beat_at_phase(light, decoder, 0)
    assert selections(lighting)[-1]['command']['autoloop'] == PREFERRED_AUTOLOOPS[LightIntent.DROP]


FX_GROUP = {
    'name': 'fx',
    'role': 'accent',
    'cell_count': 1,
    'colour': True,
    'speed': True,
    'pulse': False,
}


def special_looks():
    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock, groups=[FX_GROUP])
    capabilities = SpecialCapabilities.from_groups(lighting.groups())
    return StaticLooks(lighting, ROOM, CATALOGUE, lambda: capabilities), lighting


def test_the_fx_picker_offers_only_the_rows_that_fit_each_target():
    looks, _ = special_looks()
    # fx renders colour and speed but no pulse, so the spins fit no group, not even the
    # whole rig; no row is a strobe.
    assert looks.state()['fx_rows'] == {
        'all': ['cold_drift', 'drift'],
        'fx': ['cold_drift', 'drift'],
        'movers': [],
        'strip': [],
        'wash': [],
    }


def test_a_picked_row_is_held_at_the_static_priority_and_clear_fx_releases_it_short():
    looks, lighting = special_looks()
    looks.special('cold_drift', 'fx')
    [held] = overrides(lighting)
    assert held['value']['id'] == STATIC_SPECIAL_ID
    assert held['value']['target'] == {'group': 'fx'}
    assert held['value']['priority'] == STATIC_PRIORITY
    # The row's look, cut in at once and released short instead of its 64-beat section fade.
    assert held['value']['special_fx'] == {
        'colour_slot': 'cold',
        'energy': 0.15,
        'speed': 0.05,
        'pulse_beats': None,
        'attack_beats': 0.0,
        'release_beats': STATIC_SPECIAL_RELEASE_BEATS,
        'pattern': None,
    }
    assert looks.state()['special_fx'] == {'name': 'cold_drift', 'group': 'fx'}

    looks.clear_special()
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': STATIC_SPECIAL_ID}
    assert looks.state()['special_fx'] is None


@pytest.mark.parametrize(
    ('arguments', 'message'),
    [
        (('spin', 'fx'), "'fx' cannot render 'spin'"),
        (('drift', 'wash'), "'wash' cannot render 'drift'"),
        (('strobe_burst', 'all'), "unknown FX 'strobe_burst'"),
        (('drift', 'par'), "unknown static target 'par'"),
    ],
)
def test_a_row_the_target_cannot_render_is_refused(arguments, message):
    looks, lighting = special_looks()
    with pytest.raises(ValueError, match=message):
        looks.special(*arguments)
    assert overrides(lighting) == []


def test_clearing_static_releases_the_special_and_resend_restores_it():
    looks, lighting = special_looks()
    looks.special('drift', 'fx')
    looks.resend()
    assert [c['value']['id'] for c in overrides(lighting)] == [STATIC_SPECIAL_ID] * 2
    looks.clear()
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': STATIC_SPECIAL_ID}
    assert looks.state()['special_fx'] is None


def test_a_static_special_alone_holds_the_overlay_until_clear_all():
    from autolux.lib.main import apply_static

    looks, lighting = special_looks()
    assert not looks.active
    looks.special('drift', 'fx')
    assert looks.active

    class Engine:
        static_looks = looks

    apply_static(Engine(), {'kind': 'clear'})
    assert not looks.active
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': STATIC_SPECIAL_ID}


async def test_a_picked_row_survives_section_changes_and_fx_auto_off(tmp_path):
    from autolux.lib.main import ShowControl

    decoder = FakeDecoder()
    control = ShowControl(tmp_path / 'show.json')
    light, _, _, lighting = static_engine(decoder=decoder, queue=False)
    lighting.groups = lambda: [FX_GROUP]
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    control.commands.put_nowait(
        {'action': 'static', 'kind': 'special_fx', 'row': 'drift', 'target': 'all'}
    )
    await control.apply_pending(light)
    assert light.mode == 'static'
    assert light.static_looks.state()['special_fx'] == {'name': 'drift', 'group': 'all'}

    lighting.events.clear()
    for decision in ('drop', 'breakdown', 'drop'):
        await beat_at_phase(light, decoder, 0, decision=decision)
    # Manual FX is not the automatic show's: turning automatic FX off leaves it alone.
    control.commands.put_nowait({'action': 'fx_auto', 'enabled': False})
    await control.apply_pending(light)
    await beat_at_phase(light, decoder, 0, decision='buildup')
    assert {'kind': 'clear_override', 'id': STATIC_SPECIAL_ID} not in overrides(lighting)
    assert light.static_looks.state()['special_fx'] == {'name': 'drift', 'group': 'all'}

    control.commands.put_nowait({'action': 'static', 'kind': 'special_fx_clear'})
    await control.apply_pending(light)
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': STATIC_SPECIAL_ID}


async def test_static_releases_the_auto_special_and_auto_brings_it_back():
    decoder = FakeDecoder()
    light, _, clock, _ = static_engine(decoder=decoder, queue=False)
    lighting = RecordingStarfireClient(clock=clock, groups=[FX_GROUP])
    drift = Special(
        'drift', (LightIntent.BREAKDOWN,), 'fx', colour_slot='secondary', energy=0.2, speed=0.1
    )
    controller = EffectController(lighting, clock=clock, specials=[drift])
    light.lighting_client = lighting
    light.effect_controller = controller
    light.static_looks = StaticLooks(lighting, ROOM)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    assert [c['value']['id'] for c in overrides(lighting)] == ['special']

    lighting.events.clear()
    light.enter_static()
    # Entering static is the operator's hand, so the special cuts at once.
    assert overrides(lighting) == [
        {'kind': 'clear_override', 'id': 'special', 'instantaneous_release': True}
    ]

    lighting.events.clear()
    await light.leave_static()
    await beat_at_phase(light, decoder, 0)
    assert [c['value']['id'] for c in overrides(lighting)] == ['special']


async def test_operator_static_looks_reach_starfire_in_the_same_pass_bypassing_the_delay(
    tmp_path,
):
    from autolux.lib.main import ShowControl

    control = ShowControl(tmp_path / 'show.json')
    light, queue, clock, lighting = static_engine()
    clock.advance(5.0)
    blue = (0.0, 0.0, 1.0)
    for command in (
        {'action': 'mode', 'mode': 'static'},
        {'action': 'static', 'kind': 'look', 'target': 'all', 'colour': blue, 'dimmer': 1.0},
        {'action': 'static', 'kind': 'look', 'target': 'wash', 'colour': None, 'dimmer': 0.3},
    ):
        control.commands.put_nowait(command)
    await control.apply_pending(light)

    looks = sent(lighting, 'set_override')
    assert [at for at, _ in looks] == [clock.monotonic()] * 2
    assert [look['value']['id'] for _, look in looks] == ['static_all', 'static_wash']
    # No lifetime or transition: Starfire cuts an override in on its next 25 ms frame.
    assert all(look['value'].keys() == {'id', 'target', 'values', 'priority'} for _, look in looks)
    assert queue.pending == 0, 'operator looks must never wait behind the playback delay'
    assert control.commands.unfinished_tasks == 0, 'each command is marked handed to the engine'


HALL = StaticRig(
    groups=('wash',),
    uv_groups=('wash',),
    fixtures=(('par', 'par', ('par',)), ('bar', 'bar', ('bar/a', 'bar/b'))),
    uv_targets=('par',),
)


def highlight_looks():
    lighting = RecordingStarfireClient()
    return StaticLooks(lighting, HALL), lighting


def test_a_highlight_is_one_open_white_override_at_home_with_uv_zeroed_and_a_dead_man_expiry():
    looks, lighting = highlight_looks()
    looks.highlight({'fixture': 'par'})
    assert overrides(lighting) == [
        {
            'kind': 'set_override',
            'value': {
                'id': HIGHLIGHT_ID,
                'target': {'fixture': 'par'},
                'values': {
                    'intensity': 1.0,
                    'colour': {'red': 1.0, 'green': 1.0, 'blue': 1.0},
                    'pan': 0.5,
                    'tilt': 0.5,
                    'strobe_hz': 0.0,
                    'attributes': {'ColorAdd_UV': 0.0},
                },
                'priority': STATIC_GROUP_PRIORITY + 1,
                'lifetime': {'kind': 'seconds', 'duration_seconds': 60.0},
            },
        }
    ]
    assert looks.highlighted == {'target': {'fixture': 'par'}, 'colour': 'white'}
    assert not looks.active, 'a Locate must not hold the automatic show'


def test_testing_another_fixture_moves_the_one_highlight_and_clear_releases_it():
    looks, lighting = highlight_looks()
    looks.highlight({'fixture': 'par'})
    looks.highlight({'cell': 'bar/b'}, 'red')
    moved = overrides(lighting)[-1]['value']
    assert (moved['id'], moved['target'], 'attributes' in moved['values']) == (
        HIGHLIGHT_ID,
        {'fixture': 'bar/b'},
        False,
    )
    assert looks.highlighted == {'target': {'cell': 'bar/b'}, 'colour': 'red'}
    looks.clear_highlight()
    looks.clear_highlight()
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': HIGHLIGHT_ID}
    assert [o['kind'] for o in overrides(lighting)].count('clear_override') == 1
    assert looks.highlighted is None


def test_the_highlight_state_lapses_with_starfires_60_s_expiry_on_the_engine_clock():
    clock, lighting = VirtualClock(), RecordingStarfireClient()
    looks = StaticLooks(lighting, HALL, clock=clock)
    looks.highlight({'fixture': 'par'})
    clock.advance(59.9)
    assert looks.highlighted == {'target': {'fixture': 'par'}, 'colour': 'white'}
    clock.advance(0.1)
    assert looks.highlighted is None
    looks.clear_highlight()
    assert overrides(lighting)[-1]['kind'] == 'set_override', 'Starfire already dropped it'


@pytest.mark.parametrize(
    ('colour', 'rgb'),
    [('warm', (1.0, 0.55, 0.2)), ('blue', (0.0, 0.0, 1.0)), ('green', (0.0, 1.0, 0.0))],
)
def test_each_highlight_swatch_sends_its_colour(colour, rgb):
    looks, lighting = highlight_looks()
    looks.highlight({'fixture': 'bar'}, colour)
    sent_colour = overrides(lighting)[-1]['value']['values']['colour']
    assert tuple(sent_colour.values()) == rgb


@pytest.mark.parametrize(
    ('target', 'colour', 'paused', 'blackout', 'reason'),
    [
        ({'fixture': 'ghost'}, 'white', False, False, 'unknown fixture or cell'),
        ({'cell': 'bar'}, 'white', False, False, 'unknown fixture or cell'),
        ({'fixture': 'par'}, 'uv', False, False, 'unknown highlight colour'),
        ({'fixture': 'par'}, 'white', True, False, 'the show is paused'),
        ({'fixture': 'par'}, 'white', False, True, 'blackout is on'),
    ],
)
def test_a_highlight_the_rig_cannot_show_is_refused_and_sends_nothing(
    target, colour, paused, blackout, reason
):
    looks, lighting = highlight_looks()
    looks.set_blackout(blackout)
    lighting.events.clear()
    with pytest.raises(ValueError, match=reason):
        looks.highlight(target, colour, paused=paused)
    assert lighting.events == []


async def test_the_published_highlight_ends_at_standby_sound_stop_and_expiry_and_no_restart_revives_it(
    tmp_path,
):
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lib.main import ShowControl

    buffer = EventBuffer()
    control = ShowControl(tmp_path / 'show.json', buffer)
    light, _, clock, lighting = static_engine()
    light.static_looks = StaticLooks(lighting, HALL, clock=clock)
    locate = {'action': 'highlight', 'target': {'fixture': 'par'}, 'colour': 'white'}

    async def published(command=None):
        control.commands.put_nowait(command or {'action': 'master', 'level': 1.0})
        await control.apply_pending(light)
        return buffer.snapshot()['highlight']

    assert await published(locate) is not None
    light.on_digital_silence()
    light.on_signal_return()
    assert await published() is None, 'standby ends it and the restart from scratch keeps it off'
    assert await published(locate) is not None
    light._show_sound_stop()
    assert await published() is None
    assert await published(locate) is not None
    clock.advance(59.0)
    assert await published() is not None
    clock.advance(1.0)
    assert await published() is None, "Starfire's 60 s expiry, on the engine clock"


async def test_the_show_publishes_the_highlight_and_rig_and_clears_it_with_the_other_overrides(
    tmp_path,
):
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lib.main import ShowControl

    buffer = EventBuffer()
    control = ShowControl(tmp_path / 'show.json', buffer)
    light, _, _, lighting = static_engine()
    light.static_looks = StaticLooks(lighting, HALL)

    async def apply(command):
        control.commands.put_nowait(command)
        await control.apply_pending(light)
        return buffer.snapshot()

    snapshot = await apply({'action': 'highlight', 'target': {'cell': 'bar/a'}, 'colour': 'blue'})
    assert snapshot['highlight'] == {'target': {'cell': 'bar/a'}, 'colour': 'blue'}
    assert snapshot['rig'] == {
        'fixtures': [
            {'fixture_uuid': 'par', 'name': 'par', 'cells': ['par']},
            {'fixture_uuid': 'bar', 'name': 'bar', 'cells': ['bar/a', 'bar/b']},
        ]
    }
    assert snapshot['mode'] == 'auto'
    assert (await apply({'action': 'highlight', 'target': None}))['highlight'] is None
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': HIGHLIGHT_ID}

    clears = [
        lambda: apply({'action': 'static', 'kind': 'clear'}),
        lambda: apply({'action': 'pause'}),
        light.on_digital_silence,
        light._show_sound_stop,
    ]
    for clear in clears:
        await light.resume()
        await apply({'action': 'highlight', 'target': {'fixture': 'par'}, 'colour': 'white'})
        assert light.static_looks.highlighted is not None
        lighting.events.clear()
        result = clear()
        if result is not None:
            await result
        assert light.static_looks.highlighted is None
        assert {'kind': 'clear_override', 'id': HIGHLIGHT_ID} in overrides(lighting)

    await apply({'action': 'pause'})
    snapshot = await apply({'action': 'highlight', 'target': {'fixture': 'par'}, 'colour': 'white'})
    assert snapshot['highlight'] is None, 'paused, the engine refuses what the bridge let through'
