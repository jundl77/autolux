import logging

import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.autoloop_policy import SelectionContext
from autolux.lib.engine.effect_controller import (
    CUT_RELEASE_BEATS,
    CUT_RELEASE_SECONDS,
    DROP_ACCENT_ID,
    SPECIAL_ID,
    SPECIAL_PRIORITY,
    EffectController,
)
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    DEFAULT_AUTOLOOPS,
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    STROBE_BURST_SECONDS,
    STROBE_COOLDOWN_SECONDS,
    STROBE_MAX_BURST_SECONDS,
    STROBE_RATE_HZ,
    AutoloopMeta,
    LightIntent,
    ShowSettings,
)
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.engine.special_policy import DROP_STROBE, Special
from autolux.lighting.special_catalogue import discover_specials
from autolux.simulate.lighting_recorder import RecordingStarfireClient

FULL_INTENSITY = ShowSettings(intensity=1.0)
# Wild halves the intense cooldown; balanced keeps the setting as written.
BALANCED = ShowSettings(intensity=0.7)
BALANCED = ShowSettings(intensity=INTENSITY_BALANCED)
CUT = {
    'kind': 'clear_override',
    'id': SPECIAL_ID,
    'release_beats': CUT_RELEASE_BEATS,
    'release_seconds': CUT_RELEASE_SECONDS,
}


async def test_applied_lighting_change_has_one_useful_info_line(effect_controller, caplog):
    with caplog.at_level(logging.INFO):
        await effect_controller.change_effect(LightIntent.BUILDUP)
    assert len(caplog.records) == 1
    assert 'intent=BUILDUP | autoloop=buildup/ramp' in caplog.text


@pytest.mark.parametrize(
    ('intent', 'autoloop'),
    [
        (LightIntent.ATMOSPHERIC, 'cold_breathe'),
        (LightIntent.BREAKDOWN, 'wide_space'),
        (LightIntent.BUILDUP, 'ramp'),
        (LightIntent.DROP, 'kick'),
    ],
)
async def test_same_autoloop_continues_without_reselection_or_repeating_accent(
    effect_controller, recorded_lighting, intent, autoloop
):
    for _ in range(3):
        await effect_controller.change_effect(intent)
    commands = [event['command'] for event in recorded_lighting.events]
    assert [command['autoloop'] for command in commands if command['kind'] == 'select'] == [
        autoloop
    ]
    assert len([command for command in commands if command['kind'] == 'set_override']) == int(
        intent == LightIntent.DROP
    )


async def test_strobe_is_an_expiring_accent_over_the_drop_autoloop(recorded_lighting):
    effect_controller = EffectController(recorded_lighting, settings=lambda: FULL_INTENSITY)
    await effect_controller.change_effect(LightIntent.BREAKDOWN)
    await effect_controller.change_effect(LightIntent.DROP)
    commands = [event['command'] for event in recorded_lighting.events]
    assert [command['kind'] for command in commands] == ['select', 'select', 'set_override']
    accent = commands[-1]['value']
    assert accent['values'] == {'strobe_hz': 8.0}
    # Wild bursts for the operator's cap rather than the shorter burst setting.
    assert accent['lifetime'] == {'kind': 'seconds', 'duration_seconds': STROBE_MAX_BURST_SECONDS}


async def test_a_balanced_drop_bursts_for_the_burst_setting(recorded_lighting):
    controller = EffectController(recorded_lighting, settings=lambda: BALANCED)
    await controller.change_effect(LightIntent.DROP)
    accent = recorded_lighting.events[-1]['command']['value']
    assert accent['lifetime']['duration_seconds'] == pytest.approx(
        INTENSITY_BALANCED * STROBE_BURST_SECONDS
    )


async def test_stop_allows_the_same_autoloop_to_resume(effect_controller, recorded_lighting):
    await effect_controller.change_effect(LightIntent.DROP)
    recorded_lighting.stop()
    await effect_controller.change_effect(LightIntent.DROP)
    kinds = [event['command']['kind'] for event in recorded_lighting.events]
    assert kinds == ['select', 'set_override', 'stop', 'select']


@pytest.mark.parametrize(
    ('intent', 'transition'),
    [
        (LightIntent.ATMOSPHERIC, None),
        (LightIntent.BREAKDOWN, None),
        (LightIntent.BUILDUP, None),
        (LightIntent.DROP, {'kind': 'cut'}),
    ],
)
async def test_a_drop_cuts_in_and_every_other_change_takes_the_renderer_crossfade(
    effect_controller, recorded_lighting, intent, transition
):
    await effect_controller.change_effect(intent)
    [select] = [e['command'] for e in recorded_lighting.events if e['command']['kind'] == 'select']
    assert select.get('transition') == transition


async def _drop_after(controller, lighting, clock, seconds):
    await controller.change_effect(LightIntent.BREAKDOWN)
    clock.advance(seconds)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    return lighting.events[start:]


async def test_drop_strobe_respects_cooldown_but_the_drop_still_cuts(recorded_lighting, caplog):
    clock = VirtualClock()
    controller = EffectController(recorded_lighting, clock=clock, settings=lambda: BALANCED)

    first = await _drop_after(controller, recorded_lighting, clock, 0)
    assert [e['command']['kind'] for e in first] == ['select', 'set_override']

    with caplog.at_level(logging.INFO):
        second = await _drop_after(controller, recorded_lighting, clock, 30)
    [select] = [e['command'] for e in second]
    assert select['kind'] == 'select'
    assert select['autoloop'] == 'kick'
    assert select['transition'] == {'kind': 'cut'}
    assert 'intense special suppressed | cooldown' in caplog.text

    third = await _drop_after(controller, recorded_lighting, clock, 61 - 30)
    assert [e['command']['kind'] for e in third] == ['select', 'set_override']
    assert STROBE_COOLDOWN_SECONDS == 60


@pytest.mark.parametrize('bpm', [None, 60.0, 128.0, 600.0])
async def test_strobe_burst_stays_within_two_bars_four_seconds_and_eight_hz(recorded_lighting, bpm):
    controller = EffectController(recorded_lighting, clock=VirtualClock())
    await controller.change_effect(LightIntent.DROP, bpm=bpm)
    accent = recorded_lighting.events[-1]['command']['value']
    duration = accent['lifetime']['duration_seconds']
    assert accent['values']['strobe_hz'] <= STROBE_RATE_HZ <= 8.0
    assert duration <= STROBE_MAX_BURST_SECONDS
    if bpm:
        assert duration <= 8 * 60.0 / bpm


async def test_a_cooldown_change_applies_to_the_next_drop_without_a_restart(recorded_lighting):
    clock = VirtualClock()
    settings = FULL_INTENSITY
    controller = EffectController(recorded_lighting, clock=clock, settings=lambda: settings)
    await _drop_after(controller, recorded_lighting, clock, 0)

    settings = ShowSettings(drop_strobe_cooldown_seconds=10, intensity=1.0)
    later = await _drop_after(controller, recorded_lighting, clock, 11)
    assert [e['command']['kind'] for e in later] == ['select', 'set_override']


ROTATION = {
    **{intent: ((intent.value, 'only'),) for intent in LightIntent},
    LightIntent.ATMOSPHERIC: (
        ('atmospheric', 'cold_breathe'),
        ('atmospheric', 'amber_drift'),
        ('atmospheric', 'pixel_rain'),
    ),
    LightIntent.DROP: (('drop', 'kick'), ('drop', 'slam'), ('drop', 'chase')),
}
ROTATION_META = {
    **{loop: AutoloopMeta(1, 'still', 'wash') for loops in ROTATION.values() for loop in loops},
    **{loop: AutoloopMeta(3, 'snap', 'wash') for loop in ROTATION[LightIntent.DROP]},
}


def rotating(lighting, **kwargs):
    return EffectController(lighting, autoloops=ROTATION, autoloop_meta=ROTATION_META, **kwargs)


def selected(lighting):
    return [
        f'{e["command"]["bank"]}/{e["command"]["autoloop"]}'
        for e in lighting.events
        if e['command']['kind'] == 'select'
    ]


async def test_each_entry_plays_every_look_of_its_tier_before_any_repeats(
    recorded_lighting, caplog
):
    controller = rotating(recorded_lighting)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            await controller.change_effect(LightIntent.ATMOSPHERIC)
            await controller.change_effect(LightIntent.BUILDUP)
    looks = [name for name in selected(recorded_lighting) if name.startswith('atmospheric/')]
    assert sorted(looks) == sorted(f'{b}/{n}' for b, n in ROTATION[LightIntent.ATMOSPHERIC])
    assert 'energy=1 movement=still texture=wash | reason=calm' in caplog.text


async def test_repeating_the_current_intent_keeps_its_look(recorded_lighting):
    controller = rotating(recorded_lighting)
    for _ in range(3):
        await controller.change_effect(LightIntent.ATMOSPHERIC)
    recorded_lighting.stop()
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    await controller.change_effect(LightIntent.BREAKDOWN)
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    first, again, breakdown, after = selected(recorded_lighting)
    assert first == again and breakdown == 'breakdown/only' and after != first


async def test_a_new_track_chooses_again_within_the_same_intent(recorded_lighting):
    controller = rotating(recorded_lighting, clock=VirtualClock())
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    await controller.change_effect(
        LightIntent.ATMOSPHERIC,
        context=SelectionContext(LightIntent.ATMOSPHERIC, new_track=True),
    )
    first, second = selected(recorded_lighting)
    assert first != second


async def test_a_chosen_drop_still_cuts_and_keeps_the_strobe_cooldown(recorded_lighting):
    clock = VirtualClock()
    controller = rotating(recorded_lighting, clock=clock)
    first = await _drop_after(controller, recorded_lighting, clock, 0)
    second = await _drop_after(controller, recorded_lighting, clock, 30)
    assert [e['command']['kind'] for e in first + second] == ['select', 'set_override', 'select']
    assert first[0]['command']['autoloop'] != second[0]['command']['autoloop']
    assert second[0]['command']['transition'] == {'kind': 'cut'}


async def test_the_event_buffer_records_why_a_loop_was_chosen(recorded_lighting):
    from autolux.lib.engine.event_buffer import EventBuffer

    buffer = EventBuffer(window_sec=float('inf'), clock=VirtualClock())
    buffer.start()
    controller = EffectController(recorded_lighting, event_buffer=buffer, clock=VirtualClock())
    await controller.change_effect(LightIntent.DROP)
    effect = buffer.snapshot()['current_effect']
    assert effect['name'] == 'drop/kick'
    assert effect['selection'].pop('palette') == recorded_lighting.palette
    assert effect['selection'] == {
        'intent': 'drop',
        'look': 'drop',
        'level': 'balanced',
        'energy': 3,
        'texture': 'wash',
        'movement': 'snap',
        'reason': 'long build or long since the last drop',
    }


async def test_a_look_that_was_not_sent_is_not_remembered(recorded_lighting):
    controller = rotating(recorded_lighting)
    recorded_lighting.select_autoloop = lambda *_, **__: False
    assert not await controller.change_effect(LightIntent.ATMOSPHERIC)
    assert controller._entered is None


async def test_a_realistic_set_gets_tiers_that_follow_the_music(recorded_lighting):
    from autolux.lighting.autoloop_catalogue import discover_autoloops, read_autoloop_meta

    meta = read_autoloop_meta()
    clock = VirtualClock()
    controller = EffectController(
        recorded_lighting,
        clock=clock,
        autoloops=discover_autoloops(),
        autoloop_meta=meta,
        seed=7,
    )
    bar = 1.875
    set_list = [
        (LightIntent.ATMOSPHERIC, 32, 0, {1}),
        (LightIntent.BUILDUP, 16, 0, {3}),
        (LightIntent.DROP, 32, 16, {3}),
        (LightIntent.BREAKDOWN, 16, 0, {1}),
        (LightIntent.BUILDUP, 8, 16, {2}),
        (LightIntent.DROP, 32, 24, {2}),
        (LightIntent.BREAKDOWN, 8, 0, {1, 2}),
        (LightIntent.BUILDUP, 8, 8, {2}),
        (LightIntent.DROP, 16, 16, {2}),
        (LightIntent.BREAKDOWN, 24, 0, {1, 2}),
        (LightIntent.BUILDUP, 8, 24, {2}),
        (LightIntent.DROP, 16, 32, {3}),
        (LightIntent.ATMOSPHERIC, 16, 0, {1}),
    ]
    left_drop_at = None
    previous = None
    for intent, length, build_bars, _ in set_list:
        if previous == LightIntent.DROP:
            left_drop_at = clock.monotonic()
        since = None if left_drop_at is None else clock.monotonic() - left_drop_at
        context = SelectionContext(intent, seconds_since_drop=since, build_bars=build_bars)
        await controller.change_effect(intent, context=context)
        previous = intent
        clock.advance(length * bar)
    energies = [meta[tuple(name.split('/'))].energy for name in selected(recorded_lighting)]
    assert len(energies) == len(set_list)
    for energy, (intent, *_, expected) in zip(energies, set_list, strict=True):
        assert energy in expected, (intent, energy)


TWO_BREAKDOWN_LOOPS = {
    LightIntent.BREAKDOWN: (('breakdown', 'wide_space'), ('breakdown', 'deep_space'))
}
TWO_BREAKDOWN_META = {
    ('breakdown', 'wide_space'): AutoloopMeta(1, 'sweep', 'mixed'),
    ('breakdown', 'deep_space'): AutoloopMeta(1, 'sweep', 'beam'),
}


async def test_next_autoloop_switches_at_once_to_another_loop_with_the_default_crossfade(caplog):
    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock)
    controller = EffectController(
        lighting, clock=clock, autoloops=TWO_BREAKDOWN_LOOPS, autoloop_meta=TWO_BREAKDOWN_META
    )
    await controller.change_effect(LightIntent.BREAKDOWN)
    playing = lighting.selected_autoloop
    palette = lighting.palette
    clock.advance(5.0)
    with caplog.at_level(logging.INFO):
        choice = controller.skip_autoloop(SelectionContext(LightIntent.BREAKDOWN))
    select = lighting.events[-1]
    assert (choice.bank, choice.autoloop) != playing
    assert select == {
        'at_seconds': clock.monotonic(),
        'command': {
            'kind': 'select',
            'bank': choice.bank,
            'autoloop': choice.autoloop,
            'palette': palette,
        },
    }
    assert f'[lighting] next autoloop | intent=BREAKDOWN | autoloop={choice.bank}/' in caplog.text
    assert f'reason={choice.reason}' in caplog.text
    await controller.change_effect(LightIntent.BREAKDOWN)
    assert lighting.events[-1] is select, 're-entering the same section keeps the skipped loop'


async def test_low_intensity_sends_no_drop_strobe_but_still_cuts_to_the_drop(
    recorded_lighting, caplog
):
    settings = ShowSettings(intensity=INTENSITY_CALM)
    controller = EffectController(recorded_lighting, settings=lambda: settings)
    with caplog.at_level(logging.INFO):
        await controller.change_effect(LightIntent.DROP)
    assert [e['command']['kind'] for e in recorded_lighting.events] == ['select']
    assert 'intensity 0.30 at or below 0.30' in caplog.text


async def test_measured_intensity_keeps_the_drop_strobe(recorded_lighting):
    settings = ShowSettings(intensity=INTENSITY_BALANCED)
    controller = EffectController(recorded_lighting, settings=lambda: settings)
    await controller.change_effect(LightIntent.DROP)
    assert [e['command']['kind'] for e in recorded_lighting.events] == ['select', 'set_override']


async def test_drop_strobe_rate_and_burst_scale_with_intensity_within_the_caps(recorded_lighting):
    settings = ShowSettings(intensity=0.5, strobe_max_rate_hz=6.0)
    controller = EffectController(recorded_lighting, settings=lambda: settings)
    await controller.change_effect(LightIntent.DROP)
    accent = recorded_lighting.events[-1]['command']['value']
    assert accent['values'] == {'strobe_hz': pytest.approx(3.0)}
    assert accent['lifetime']['duration_seconds'] == pytest.approx(0.5 * STROBE_BURST_SECONDS)


@pytest.mark.parametrize(
    ('intensity', 'cooldown'), [(INTENSITY_WILD, 30.0), (INTENSITY_BALANCED, 60.0), (0.4, 60.0)]
)
async def test_drop_strobe_cooldown_is_as_set_and_halved_when_wild(
    recorded_lighting, intensity, cooldown
):
    clock = VirtualClock()
    settings = ShowSettings(intensity=intensity)
    controller = EffectController(recorded_lighting, clock=clock, settings=lambda: settings)
    await _drop_after(controller, recorded_lighting, clock, 0)
    early = await _drop_after(controller, recorded_lighting, clock, cooldown - 1)
    assert [e['command']['kind'] for e in early] == ['select']
    later = await _drop_after(controller, recorded_lighting, clock, 2)
    assert [e['command']['kind'] for e in later] == ['select', 'set_override']


async def test_the_intensity_setting_caps_the_drop_energy_at_selection_time(recorded_lighting):
    settings = ShowSettings(intensity=0.5)
    controller = EffectController(
        recorded_lighting,
        settings=lambda: settings,
        autoloops={**DEFAULT_AUTOLOOPS, LightIntent.DROP: (('drop', 'kick'), ('drop', 'soft'))},
        autoloop_meta={
            **DEFAULT_AUTOLOOP_META,
            ('drop', 'soft'): AutoloopMeta(2, 'snap', 'wash'),
        },
    )
    await controller.change_effect(LightIntent.DROP, context=SelectionContext(LightIntent.DROP))
    [select] = [e['command'] for e in recorded_lighting.events if e['command']['kind'] == 'select']
    assert select['autoloop'] == 'soft'


FX_GROUP = {
    'name': 'fx',
    'cell_count': 1,
    'colour': True,
    'speed': True,
    'pulse': True,
    'pattern': False,
    'role': 'accent',
}
SPIN = Special(
    'spin',
    (LightIntent.DROP,),
    'fx',
    colour_slot='primary',
    energy=0.8,
    speed=0.8,
    pulse_beats=1,
    release_beats=4,
)
DRIFT = Special(
    'drift',
    (LightIntent.ATMOSPHERIC,),
    'fx',
    colour_slot='secondary',
    energy=0.2,
    speed=0.1,
    attack_beats=32,
    release_beats=32,
)


def with_specials(
    specials, groups=(FX_GROUP,), settings=FULL_INTENSITY, event_buffer=None, **options
):
    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock, groups=groups)
    controller = EffectController(
        lighting,
        event_buffer=event_buffer,
        clock=clock,
        settings=lambda: settings,
        specials=specials,
        **options,
    )
    return controller, lighting, clock


def accents(lighting, start=0):
    return [
        e['command']
        for e in lighting.events[start:]
        if e['command']['kind'] in ('set_override', 'clear_override')
    ]


async def test_a_special_is_sent_in_the_wire_vocabulary_unscaled_by_intensity():
    controller, lighting, _ = with_specials([DRIFT], settings=ShowSettings(intensity=0.5))
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    assert accents(lighting) == [
        {
            'kind': 'set_override',
            'value': {
                'id': SPECIAL_ID,
                'target': {'group': 'fx'},
                'special_fx': {
                    'colour_slot': 'secondary',
                    'energy': 0.2,
                    'speed': 0.1,
                    'pulse_beats': None,
                    'attack_beats': 32.0,
                    'release_beats': 32.0,
                    'pattern': None,
                },
                'priority': SPECIAL_PRIORITY,
            },
        }
    ]


async def test_the_catalogue_plays_one_texture_per_fx_cooldown_then_a_drop_accent():
    controller, lighting, clock = with_specials(discover_specials())
    textures = []
    for _ in range(3):
        start = len(lighting.events)
        await controller.change_effect(LightIntent.ATMOSPHERIC)
        textures += [c['value']['special_fx']['colour_slot'] for c in accents(lighting, start)]
        await controller.change_effect(LightIntent.BUILDUP)
    # The first texture rests fx for 240 s, so neither texture plays again inside it.
    assert len(textures) == 1
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    assert [c['value']['id'] for c in accents(lighting, start)] == [DROP_ACCENT_ID]
    clock.advance(240)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.BREAKDOWN)
    assert accents(lighting, start)[0]['kind'] == 'set_override'

    clock.advance(240)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    spin, strobe = accents(lighting, start)
    assert spin['value']['id'] == SPECIAL_ID
    assert spin['value']['target'] == {'group': 'fx'}
    assert spin['value']['special_fx']['pulse_beats'] == 1
    assert strobe['value']['id'] == DROP_ACCENT_ID
    assert strobe['value']['values'] == {'strobe_hz': STROBE_RATE_HZ}


async def test_a_soft_drop_keeps_the_strobe_but_plays_no_intense_special(caplog):
    controller, lighting, _ = with_specials([SPIN, DROP_STROBE])
    soft = SelectionContext(LightIntent.DROP, arrival_of_peak=0.6)
    with caplog.at_level(logging.INFO):
        await controller.change_effect(LightIntent.DROP, context=soft)
    [strobe] = accents(lighting)
    assert strobe['value']['id'] == DROP_ACCENT_ID
    assert 'intense special suppressed | drop arrival 0.60 below 0.90' in caplog.text


async def test_a_strobe_row_releases_the_running_special_before_it_bursts():
    controller, lighting, _ = with_specials([DRIFT, DROP_STROBE])
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    clear, strobe = accents(lighting, start)
    assert clear == {'kind': 'clear_override', 'id': SPECIAL_ID}
    assert strobe['value']['id'] == DROP_ACCENT_ID


async def test_a_drop_fires_its_row_and_the_strobe_as_one_event_on_one_cooldown(caplog):
    controller, lighting, clock = with_specials([SPIN, DROP_STROBE], settings=BALANCED)
    fired = []
    for wait in (0, 30, 31):
        start = len(lighting.events)
        with caplog.at_level(logging.INFO):
            await _drop_after(controller, lighting, clock, wait)
        fired.append([c['value']['id'] for c in accents(lighting, start) if 'value' in c])
    assert fired == [[SPECIAL_ID, DROP_ACCENT_ID], [], [SPECIAL_ID, DROP_ACCENT_ID]]
    assert 'intense special suppressed | cooldown 30s of 60s left' in caplog.text


async def test_without_reported_groups_only_the_whole_rig_strobe_remains():
    controller, lighting, _ = with_specials(discover_specials(), groups=())
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    assert accents(lighting) == []
    await controller.change_effect(LightIntent.DROP)
    [strobe] = accents(lighting)
    assert strobe['value']['id'] == DROP_ACCENT_ID


async def test_capabilities_are_read_when_an_intent_starts_not_when_built():
    groups = []
    controller, lighting, _ = with_specials([DRIFT], groups=groups)
    groups.append(FX_GROUP)
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    assert [c['value']['id'] for c in accents(lighting)] == [SPECIAL_ID]


async def test_at_or_below_the_strobe_gate_no_intense_special_is_sent(caplog):
    controller, lighting, _ = with_specials([SPIN], settings=ShowSettings(intensity=0.29))
    with caplog.at_level(logging.INFO):
        await controller.change_effect(LightIntent.DROP)
    assert accents(lighting) == []
    assert 'intense special suppressed | intensity 0.29 at or below 0.30' in caplog.text


async def test_standby_releases_the_special_with_the_accent():
    controller, lighting, _ = with_specials([DRIFT])
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    start = len(lighting.events)
    controller.enter_standby(120.0)
    assert accents(lighting, start) == [
        {'kind': 'clear_override', 'id': DROP_ACCENT_ID},
        CUT,
    ]


async def test_the_event_buffer_shows_the_active_special_and_the_capable_groups():
    buffer = EventBuffer(clock=VirtualClock())
    controller, _, _ = with_specials([DRIFT], event_buffer=buffer)
    assert buffer.snapshot()['special_groups'] == {'fx': ['colour', 'pulse', 'speed']}
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    assert buffer.snapshot()['special'] == {'name': 'drift', 'group': 'fx'}
    await controller.change_effect(LightIntent.BREAKDOWN)
    assert buffer.snapshot()['special'] is None


async def test_an_intense_special_starts_the_strobe_cooldown_the_dashboard_counts_down():
    clock = VirtualClock()
    buffer = EventBuffer(clock=clock)
    buffer.start()
    settings = ShowSettings(intensity=0.7, drop_strobe_cooldown_seconds=60)
    buffer.set_control_state(paused=False, master_brightness=1.0, settings=settings.to_dict())
    controller, _, _ = with_specials([SPIN], settings=settings, event_buffer=buffer)
    assert buffer.snapshot()['strobe_cooldown_seconds'] == 0.0
    await controller.change_effect(LightIntent.DROP)
    clock.advance(19.0)
    assert buffer.snapshot()['strobe_cooldown_seconds'] == pytest.approx(41.0)


async def test_a_new_drop_replaces_the_drop_special_under_the_same_id():
    calm = Special('glow', (LightIntent.DROP,), 'fx', colour_slot='cold', energy=0.3)
    controller, lighting, _ = with_specials([calm], autoloops=ROTATION, autoloop_meta=ROTATION_META)
    await controller.change_effect(LightIntent.DROP)
    start = len(lighting.events)
    await controller.change_effect(
        LightIntent.DROP, context=SelectionContext(LightIntent.DROP, new_track=True)
    )
    [replacement] = accents(lighting, start)
    assert (replacement['kind'], replacement['value']['id']) == ('set_override', SPECIAL_ID)


async def test_an_intense_special_blocks_a_later_drop_burst_through_the_same_timestamp(caplog):
    surge = Special(
        'surge', (LightIntent.BUILDUP,), 'fx', colour_slot='primary', energy=0.6, speed=0.9
    )
    controller, lighting, clock = with_specials([surge, DROP_STROBE], settings=BALANCED)
    await controller.change_effect(LightIntent.BUILDUP)
    clock.advance(10)
    start = len(lighting.events)
    with caplog.at_level(logging.INFO):
        await controller.change_effect(LightIntent.DROP)
    assert accents(lighting, start) == [{'kind': 'clear_override', 'id': SPECIAL_ID}]
    assert 'intense special suppressed | cooldown 50s of 60s left' in caplog.text


async def test_a_held_intent_resends_a_released_special_without_choosing_or_charging_again():
    controller, lighting, clock = with_specials([SPIN, DROP_STROBE])
    await controller.change_effect(LightIntent.DROP)
    controller.release_special()
    lighting.selected_autoloop = None
    clock.advance(90)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    [resent] = accents(lighting, start)
    assert resent['value']['id'] == SPECIAL_ID
    assert controller._last_intense_at == 0


async def test_skipping_the_loop_keeps_the_running_special():
    buffer = EventBuffer(clock=VirtualClock())
    controller, lighting, _ = with_specials(
        [DRIFT], event_buffer=buffer, autoloops=ROTATION, autoloop_meta=ROTATION_META
    )
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    start = len(lighting.events)
    controller.skip_autoloop(SelectionContext(LightIntent.ATMOSPHERIC))
    assert accents(lighting, start) == []
    assert buffer.snapshot()['special'] == {'name': 'drift', 'group': 'fx'}


CAPPED_DROPS = {**DEFAULT_AUTOLOOPS, LightIntent.DROP: (('drop', 'kick'), ('drop', 'soft'))}
CAPPED_DROP_META = {**DEFAULT_AUTOLOOP_META, ('drop', 'soft'): AutoloopMeta(2, 'snap', 'wash')}


def _capped_show(tmp_path):
    from autolux.lib.main import ShowControl
    from autolux.tests.test_light_engine import FakeDecoder, engine

    control = ShowControl(tmp_path / 'show.json')
    decoder = FakeDecoder()
    light, _, clock, lighting = engine(decoder=decoder, queue=False)
    light._settings = lambda: control.settings
    light.effect_controller = EffectController(
        lighting,
        clock=clock,
        settings=lambda: control.settings,
        autoloops=CAPPED_DROPS,
        autoloop_meta=CAPPED_DROP_META,
    )
    return control, light, decoder, clock, lighting


async def _set_intensity(control, light, level):
    control.commands.put_nowait({'action': 'intensity', 'level': level})
    await control.apply_pending(light)


async def test_an_intensity_change_reaches_starfire_at_once_and_says_so(tmp_path, caplog):
    from autolux.tests.test_light_engine import sent

    control, light, _, clock, lighting = _capped_show(tmp_path)
    clock.advance(3.0)
    with caplog.at_level(logging.INFO):
        await _set_intensity(control, light, 0.5)
    assert sent(lighting, 'set_intensity') == [
        (clock.monotonic(), {'kind': 'set_intensity', 'level': 0.5})
    ]
    assert '[engine] intensity 0.50 (balanced) sent to Starfire' in caplog.text


async def test_lowering_intensity_swaps_an_over_cap_loop_at_once_and_raising_it_waits(
    tmp_path, caplog
):
    from autolux.tests.test_light_engine import beat_at_phase, selections

    control, light, decoder, clock, lighting = _capped_show(tmp_path)
    await _set_intensity(control, light, INTENSITY_WILD)
    await beat_at_phase(light, decoder, 0, decision='drop')
    assert lighting.selected_autoloop == ('drop', 'kick')
    entries = len(selections(lighting))

    clock.advance(2.0)
    with caplog.at_level(logging.INFO):
        await _set_intensity(control, light, 0.5)
    swap = selections(lighting)[-1]
    assert swap['at_seconds'] == clock.monotonic()
    assert (swap['command']['autoloop'], swap['command']['transition']) == (
        'soft',
        {'kind': 'crossfade', 'duration_beats': pytest.approx(128.0 / 60.0 * 0.1)},
    )
    assert 'reason=intensity cap;' in caplog.text
    assert [e['command']['kind'] for e in lighting.events[-3:]] == [
        'set_intensity',
        'set_default_crossfade_beats',
        'select',
    ]

    await _set_intensity(control, light, INTENSITY_WILD)
    assert len(selections(lighting)) == entries + 1, 'a raised cap waits for the next entry'
    await beat_at_phase(light, decoder, 0)
    assert lighting.selected_autoloop == ('drop', 'soft'), 'the held drop keeps the swapped loop'


@pytest.mark.parametrize(
    ('decision', 'calm_look'),
    [('drop', 'breakdown/wide_space'), ('buildup', 'atmospheric/cold_breathe')],
)
async def test_calm_swaps_a_drop_or_build_to_its_quiet_bank_at_once_and_back_on_leaving(
    tmp_path, caplog, decision, calm_look
):
    from autolux.tests.test_light_engine import beat_at_phase, selections

    control, light, decoder, _clock, lighting = _capped_show(tmp_path)
    await beat_at_phase(light, decoder, 0, decision=decision)
    entered = lighting.selected_autoloop
    assert entered[0] == decision

    with caplog.at_level(logging.INFO):
        await _set_intensity(control, light, INTENSITY_CALM)
    swap = selections(lighting)[-1]['command']
    assert f"{swap['bank']}/{swap['autoloop']}" == calm_look
    assert swap['palette'] == light.effect_controller._palette_for(
        LightIntent(calm_look.split('/')[0])
    )
    assert 'reason=intensity calm;' in caplog.text
    await beat_at_phase(light, decoder, 0)
    assert f'{lighting.selected_autoloop[0]}/{lighting.selected_autoloop[1]}' == calm_look

    await _set_intensity(control, light, INTENSITY_BALANCED)
    assert lighting.selected_autoloop[0] == decision, 'leaving calm restores the section bank'


async def test_a_calm_drop_entry_plays_a_breakdown_look_without_a_strobe(recorded_lighting):
    settings = ShowSettings(intensity=INTENSITY_CALM)
    controller = EffectController(recorded_lighting, settings=lambda: settings)
    await controller.change_effect(LightIntent.DROP)
    [select] = [e['command'] for e in recorded_lighting.events]
    assert (select['bank'], select['autoloop']) == ('breakdown', 'wide_space')
    assert 'transition' not in select, 'a calm drop takes the renderer crossfade, not a cut'


@pytest.mark.parametrize(
    ('level', 'beats'), [(INTENSITY_CALM, 2.0), (INTENSITY_BALANCED, 1.0), (INTENSITY_WILD, 0.5)]
)
async def test_the_level_scales_the_live_crossfade_and_restores_it(tmp_path, level, beats):
    from autolux.tests.test_light_engine import sent

    control, light, _, _, lighting = _capped_show(tmp_path)
    await _set_intensity(control, light, level)
    await _set_intensity(control, light, 0.5)
    crossfades = [command['beats'] for _, command in sent(lighting, 'set_default_crossfade_beats')]
    if level == INTENSITY_BALANCED:
        assert crossfades == [1.0], 'no resend while the level stays balanced'
    else:
        assert crossfades == [beats, 1.0]


async def test_static_mode_ignores_the_intensity_cap(tmp_path):
    from autolux.tests.test_light_engine import beat_at_phase, selections

    control, light, decoder, _, lighting = _capped_show(tmp_path)
    await beat_at_phase(light, decoder, 0, decision='drop')
    light.enter_static()
    entries = len(selections(lighting))
    await _set_intensity(control, light, 0.3)
    assert len(selections(lighting)) == entries


async def test_a_calm_swap_during_a_drop_reaches_the_snapshot_with_the_real_intent(tmp_path):
    from autolux.lib.main import ShowControl
    from autolux.tests.test_light_engine import FakeDecoder, beat_at_phase, engine

    control = ShowControl(tmp_path / 'show.json')
    decoder = FakeDecoder()
    light, _, clock, lighting = engine(decoder=decoder, queue=False, events=True)
    light._settings = lambda: control.settings
    light.effect_controller = EffectController(
        lighting,
        event_buffer=light.event_buffer,
        clock=clock,
        settings=lambda: control.settings,
        autoloops=CAPPED_DROPS,
        autoloop_meta=CAPPED_DROP_META,
    )
    await beat_at_phase(light, decoder, 0, decision='drop')
    before = light.event_buffer.snapshot()['current_effect']
    assert before['name'].startswith('drop/')

    clock.advance(1.0)
    await _set_intensity(control, light, INTENSITY_CALM)
    after = light.event_buffer.snapshot()
    assert after['current_effect']['name'] == 'breakdown/wide_space'
    selection = after['current_effect']['selection']
    assert (selection['intent'], selection['look'], selection['level']) == (
        'drop',
        'breakdown',
        'calm',
    )
    assert after['intent'] == 'drop'


async def test_the_snapshot_carries_each_special_cooldown_on_the_engine_clock():
    texture = Special(
        'drift',
        (LightIntent.ATMOSPHERIC,),
        'fx',
        colour_slot='cold',
        energy=0.3,
        speed=0.1,
        cooldown_seconds=240,
    )
    clock = VirtualClock()
    buffer = EventBuffer(clock=clock)
    buffer.start()
    controller = EffectController(
        RecordingStarfireClient(clock=clock, groups=(FX_GROUP,)),
        event_buffer=buffer,
        clock=clock,
        settings=lambda: BALANCED,
        specials=[texture, SPIN, DROP_STROBE],
    )

    def cooldowns():
        return {row['group']: row for row in buffer.snapshot()['special_cooldowns']}

    before = cooldowns()
    assert set(before) == {'fx', 'all', 'venue'}
    assert before['fx']['last'] is None
    assert all(row['ready'] and row['remaining_seconds'] == 0.0 for row in before.values())
    await controller.change_effect(LightIntent.DROP)
    clock.advance(10.0)
    first = cooldowns()
    clock.advance(30.0)
    later = cooldowns()
    # Spin sets no cooldown of its own, so fx is free at once; the strobe waits out the shared one.
    assert (first['fx']['last'], first['fx']['ready']) == ('spin', True)
    assert first['all']['last'] == 'strobe_burst'
    assert first['venue']['label'] == 'venue · accents 1/2'
    assert first['all']['cooldown_seconds'] == pytest.approx(60.0)
    assert first['all']['remaining_seconds'] == pytest.approx(50.0)
    assert later['all']['remaining_seconds'] == pytest.approx(20.0)
    assert not later['all']['ready']
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    clock.advance(100.0)
    rested = cooldowns()['fx']
    assert (rested['last'], rested['cooldown_seconds']) == ('drift', 240)
    assert rested['remaining_seconds'] == pytest.approx(140.0)
    clock.advance(140.0)
    assert all(row['ready'] for row in cooldowns().values())


async def test_fx_off_picks_no_automatic_fx_but_keeps_the_drop_strobe():
    import dataclasses

    from autolux.lib.engine.static_mode import STATIC_SPECIAL_ID, StaticLooks, StaticRig

    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock, groups=(FX_GROUP,))
    current = [FULL_INTENSITY]
    # Own rows, not the catalogue, so the test does not hang on which intents ship an fx row.
    controller = EffectController(
        lighting, clock=clock, settings=lambda: current[0], specials=[SPIN, DROP_STROBE]
    )
    # Every drop here is loud: no arrival is measured, and an unknown arrival is never soft.
    await controller.change_effect(LightIntent.DROP)
    assert [c['value']['id'] for c in accents(lighting)] == [SPECIAL_ID, DROP_ACCENT_ID]

    current[0] = dataclasses.replace(FULL_INTENSITY, fx_auto=False)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.BUILDUP)
    # Settings read directly, not the toggle: the section change releases it as usual.
    assert accents(lighting, start) == [{'kind': 'clear_override', 'id': SPECIAL_ID}]
    start = len(lighting.events)
    clock.advance(STROBE_COOLDOWN_SECONDS + 1)
    await controller.change_effect(LightIntent.DROP)
    # A loud drop still bursts the strobe; only the special is left out.
    [strobe] = accents(lighting, start)
    assert strobe['value']['id'] == DROP_ACCENT_ID
    assert strobe['value']['values'] == {'strobe_hz': STROBE_RATE_HZ}

    # The operator's Static FX row does not ask the policy, so it still plays.
    drift = Special('drift', (LightIntent.BREAKDOWN,), 'fx', colour_slot='primary', speed=0.5)
    looks = StaticLooks(lighting, StaticRig(groups=('fx',)), [drift])
    looks.special('drift', 'fx')
    assert accents(lighting, start)[-1]['value']['id'] == STATIC_SPECIAL_ID
    looks.clear_special()

    current[0] = FULL_INTENSITY
    await controller.change_effect(LightIntent.BUILDUP)
    start = len(lighting.events)
    clock.advance(STROBE_COOLDOWN_SECONDS + 1)
    await controller.change_effect(LightIntent.DROP)
    assert [c['value']['id'] for c in accents(lighting, start)] == [SPECIAL_ID, DROP_ACCENT_ID]


def _long_texture_on_a_drop(tmp_path):
    import dataclasses

    from autolux.lib.main import ShowControl

    control = ShowControl(tmp_path / 'show.json')
    control.settings = FULL_INTENSITY
    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock, groups=(FX_GROUP,))
    texture = dataclasses.replace(SPIN, release_beats=64)
    controller = EffectController(
        lighting, clock=clock, settings=lambda: control.settings, specials=[texture, DROP_STROBE]
    )
    return control, controller, lighting, clock


async def test_fx_off_fades_a_running_texture_out_at_once_and_spares_the_drop_strobe(tmp_path):
    from types import SimpleNamespace

    control, controller, lighting, _ = _long_texture_on_a_drop(tmp_path)
    await controller.change_effect(LightIntent.DROP)
    assert [c['value']['id'] for c in accents(lighting)] == [SPECIAL_ID, DROP_ACCENT_ID]
    show = SimpleNamespace(effect_controller=controller)

    start = len(lighting.events)
    control.commands.put_nowait({'action': 'fx_auto', 'enabled': False})
    await control.apply_pending(show)
    assert accents(lighting, start) == [CUT], 'the drop strobe keeps its own burst'

    control.commands.put_nowait({'action': 'fx_auto', 'enabled': True})
    await control.apply_pending(show)
    assert accents(lighting, start) == [CUT], 'FX on restarts nothing; the next entry picks'


async def test_standby_fades_a_running_texture_out_at_once_and_keeps_the_cooldown(tmp_path):
    _, controller, lighting, _ = _long_texture_on_a_drop(tmp_path)
    await controller.change_effect(LightIntent.DROP)
    start = len(lighting.events)
    controller.enter_standby(120.0)
    assert CUT in accents(lighting, start)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    assert accents(lighting, start) == [], 'the cooldown still holds both intense rows back'


def _delayed_show(tmp_path, delay_sec=14.0):
    from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
    from autolux.lib.engine.light_engine import LightEngine

    control, controller, lighting, clock = _long_texture_on_a_drop(tmp_path)
    command_queue = DelayedCommandQueue(delay_sec, clock=clock)
    light = LightEngine(
        lighting, controller, command_queue, playback_delay_sec=delay_sec, clock=clock
    )
    return control, light, command_queue, lighting, clock


async def _settle_audible_timeline(command_queue, clock, delay_sec=14.0):
    clock.advance(delay_sec + 1.0)
    await command_queue.drain()


@pytest.mark.parametrize('cause', ['fx_off', 'standby', 'pause'])
async def test_an_operator_cut_reaches_the_renderer_now_not_at_the_audible_position(
    tmp_path, cause
):
    control, light, command_queue, lighting, clock = _delayed_show(tmp_path)
    await light.effect_controller.change_effect(LightIntent.DROP)
    clock.advance(3.0)
    start = len(lighting.events)
    if cause == 'fx_off':
        control.commands.put_nowait({'action': 'fx_auto', 'enabled': False})
        await control.apply_pending(light)
    elif cause == 'pause':
        light.pause()
        kinds = [e['command']['kind'] for e in lighting.events[start:]]
        assert kinds.index('set_paused') < kinds.index('clear_override'), 'cut on a paused clock'
    else:
        light.on_digital_silence()
    cuts = [e for e in lighting.events[start:] if e['command'] == CUT]
    assert [e['at_seconds'] for e in cuts] == [clock.monotonic()], 'sent on the cut, undelayed'
    assert command_queue.pending == 0, 'nothing for the special waits on the delayed queue'
    after = len(lighting.events)
    await _settle_audible_timeline(command_queue, clock)
    assert not [e for e in lighting.events[after:] if e['command']['kind'] == 'clear_override']


async def test_fx_auto_round_trips_from_the_bridge_through_the_engine_to_show_json(tmp_path):
    import json

    from autolux.lib.main import ShowControl
    from autolux.lib.ui_bridge import parse_control_command

    control, light, _, _, lighting = _capped_show(tmp_path)
    assert control.settings.fx_auto is True
    sent = len(lighting.events)
    control.commands.put_nowait(parse_control_command({'action': 'fx_auto', 'enabled': False}))
    await control.apply_pending(light)
    assert light.effect_controller._settings().fx_auto is False
    assert len(lighting.events) == sent, 'switching FX off cuts nothing at once'
    # The control's own clock settles the save; saving at once stands in for that wait.
    control.save_settings()
    saved = json.loads((tmp_path / 'show.json').read_text(encoding='utf-8'))
    assert saved['settings']['fx_auto'] is False
    assert ShowControl(tmp_path / 'show.json').settings.fx_auto is False
    with pytest.raises(ValueError, match='fx_auto must be true or false'):
        parse_control_command({'action': 'fx_auto', 'enabled': 'off'})


async def test_a_settings_edit_keeps_fx_off_and_the_level_picked_since_its_page_last_polled(
    tmp_path,
):
    from autolux.lib.ui_bridge import parse_control_command

    control, light, _, _, _ = _capped_show(tmp_path)
    for command in (
        {'action': 'fx_auto', 'enabled': False},
        {'action': 'intensity', 'level': 0.3},
        {'action': 'settings', 'settings': {'crossfade_beats': 2.0}},
        {'action': 'settings', 'settings': {'quiet_brightness': 0.25}},
    ):
        control.commands.put_nowait(parse_control_command(command))
    await control.apply_pending(light)
    settings = light.effect_controller._settings()
    assert settings.fx_auto is False
    assert settings.intensity == 0.3
    assert (settings.crossfade_beats, settings.quiet_brightness) == (2.0, 0.25)
