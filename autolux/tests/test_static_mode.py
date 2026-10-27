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
from autolux.lib.engine.special_policy import Special
from autolux.lib.engine.static_mode import (
    STATIC_FLOOR_PRIORITY,
    STATIC_GROUP_PRIORITY,
    STATIC_PRIORITY,
    STATIC_SPECIAL_ID,
    STATIC_SPECIAL_RELEASE_BEATS,
    StaticLooks,
    StaticRig,
)
from autolux.simulate.lighting_recorder import RecordingStarfireClient
from autolux.tests.test_light_engine import FakeDecoder, beat_at_phase, engine, selections, sent

ROOM = StaticRig(groups=('fx', 'movers', 'strip', 'wash'), uv_groups=('wash',))


def static_engine(**options):
    light, queue, clock, lighting = engine(**options)
    light.static_looks = StaticLooks(lighting, ROOM)
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
    assert overrides(lighting)[-3:] == [
        {'kind': 'clear_override', 'id': 'static_wash'},
        {'kind': 'clear_override', 'id': 'static_strip'},
        {'kind': 'clear_override', 'id': 'static_floor'},
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


async def test_a_wash_look_lights_only_the_wash_and_darkens_the_other_groups():
    light, _, _, lighting = static_engine()
    light.enter_static()
    light.static_looks.look('wash', colour=(1.0, 0.0, 0.0))
    assert [c['value'] for c in overrides(lighting)] == [
        DARK_FLOOR,
        {
            'id': 'static_wash',
            'target': {'group': 'wash'},
            'values': {'colour': {'red': 1.0, 'green': 0.0, 'blue': 0.0}, 'intensity': 1.0},
            'priority': STATIC_GROUP_PRIORITY,
        },
    ]
    light.static_looks.look('movers', dimmer=0.5)
    assert overrides(lighting)[-1]['value']['target'] == {'group': 'movers'}
    assert [c['value']['id'] for c in overrides(lighting)].count('static_floor') == 1


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
    assert overrides(lighting)[-2:] == [
        {'kind': 'clear_override', 'id': 'static_wash'},
        {'kind': 'clear_override', 'id': 'static_floor'},
    ]


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


FX_GROUP = {'name': 'fx', 'cell_count': 1, 'colour': True, 'speed': True, 'pulse': False}


def special_looks():
    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock, groups=[FX_GROUP])
    return StaticLooks(lighting, ROOM), lighting


def test_a_static_special_is_the_same_dict_at_the_static_priority():
    looks, lighting = special_looks()
    looks.special('fx', 'cold', 0.6, 0.4)
    assert overrides(lighting) == [
        {
            'kind': 'set_override',
            'value': {
                'id': STATIC_SPECIAL_ID,
                'target': {'group': 'fx'},
                'special_fx': {
                    'colour_slot': 'cold',
                    'energy': 0.6,
                    'speed': 0.4,
                    'pulse_beats': None,
                    'attack_beats': 0.0,
                    'release_beats': STATIC_SPECIAL_RELEASE_BEATS,
                    'pattern': None,
                },
                'priority': STATIC_PRIORITY,
            },
        }
    ]
    assert looks.state()['special_fx'] == {
        'group': 'fx',
        'colour_slot': 'cold',
        'energy': 0.6,
        'speed': 0.4,
        'pulse_beats': None,
    }


@pytest.mark.parametrize(
    ('arguments', 'message'),
    [
        (('wash', 'cold', 0.5, 0.5), "'wash' cannot render this special"),
        (('fx', 'cold', 0.5, 0.5, 1), "'fx' cannot render this special"),
        (('fx', 'pink', 0.5, 0.5), "colour_slot 'pink'"),
        (('fx', 'cold', 1.5, 0.5), 'within 0..1'),
    ],
)
def test_a_static_special_the_group_cannot_render_is_refused(arguments, message):
    looks, lighting = special_looks()
    with pytest.raises(ValueError, match=message):
        looks.special(*arguments)
    assert overrides(lighting) == []


def test_clearing_static_releases_the_special_and_resend_restores_it():
    looks, lighting = special_looks()
    looks.special('fx', 'primary', 0.5, 0.5)
    looks.resend()
    assert [c['value']['id'] for c in overrides(lighting)] == [STATIC_SPECIAL_ID] * 2
    looks.clear()
    assert overrides(lighting)[-1] == {'kind': 'clear_override', 'id': STATIC_SPECIAL_ID}
    assert looks.state()['special_fx'] is None


def test_a_static_special_alone_holds_the_overlay_until_clear_all():
    from autolux.lib.main import apply_static

    looks, lighting = special_looks()
    assert not looks.active
    looks.special('fx', 'primary', 0.5, 0.5)
    assert looks.active

    class Engine:
        static_looks = looks

    apply_static(Engine(), {'kind': 'clear'})
    assert not looks.active
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
    assert overrides(lighting) == [{'kind': 'clear_override', 'id': 'special'}]

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
