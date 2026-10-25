import logging

import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import (
    STROBE_BURST_SECONDS,
    STROBE_COOLDOWN_SECONDS,
    STROBE_MAX_BURST_SECONDS,
    STROBE_RATE_HZ,
    LightIntent,
)


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


async def test_strobe_is_an_expiring_accent_over_the_drop_autoloop(
    effect_controller, recorded_lighting
):
    await effect_controller.change_effect(LightIntent.BREAKDOWN)
    await effect_controller.change_effect(LightIntent.DROP)
    commands = [event['command'] for event in recorded_lighting.events]
    assert [command['kind'] for command in commands] == ['select', 'select', 'set_override']
    accent = commands[-1]['value']
    assert accent['values'] == {'strobe_hz': 8.0}
    assert accent['lifetime'] == {'kind': 'seconds', 'duration_seconds': STROBE_BURST_SECONDS}
    assert STROBE_BURST_SECONDS == 1.0


async def test_stop_allows_the_same_autoloop_to_resume(effect_controller, recorded_lighting):
    await effect_controller.change_effect(LightIntent.DROP)
    recorded_lighting.stop()
    await effect_controller.change_effect(LightIntent.DROP)
    kinds = [event['command']['kind'] for event in recorded_lighting.events]
    # Stopping does not reset the strobe cooldown, so the resumed drop only cuts.
    assert kinds == ['select', 'set_override', 'stop', 'select']


@pytest.mark.parametrize(
    ('intent', 'transition'),
    [
        (LightIntent.ATMOSPHERIC, {'kind': 'crossfade', 'duration_beats': 1}),
        (LightIntent.BREAKDOWN, {'kind': 'crossfade', 'duration_beats': 1}),
        (LightIntent.BUILDUP, {'kind': 'crossfade', 'duration_beats': 1}),
        (LightIntent.DROP, {'kind': 'cut'}),
    ],
)
async def test_a_drop_cuts_in_and_every_other_change_crossfades_over_a_beat(
    effect_controller, recorded_lighting, intent, transition
):
    await effect_controller.change_effect(intent)
    [select] = [e['command'] for e in recorded_lighting.events if e['command']['kind'] == 'select']
    assert select['transition'] == transition


async def _drop_after(controller, lighting, clock, seconds):
    await controller.change_effect(LightIntent.BREAKDOWN)
    clock.advance(seconds)
    start = len(lighting.events)
    await controller.change_effect(LightIntent.DROP)
    return lighting.events[start:]


async def test_drop_strobe_respects_cooldown_but_the_drop_still_cuts(recorded_lighting, caplog):
    clock = VirtualClock()
    controller = EffectController(recorded_lighting, clock=clock)

    first = await _drop_after(controller, recorded_lighting, clock, 0)
    assert [e['command']['kind'] for e in first] == ['select', 'set_override']

    with caplog.at_level(logging.INFO):
        second = await _drop_after(controller, recorded_lighting, clock, 30)
    [select] = [e['command'] for e in second]
    assert select['kind'] == 'select'
    assert select['autoloop'] == 'kick'
    assert select['transition'] == {'kind': 'cut'}
    assert 'drop strobe suppressed' in caplog.text

    # Cooldown counts from the first burst, not from the suppressed drop.
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
    from autolux.lib.engine.effect_definitions import ShowSettings

    clock = VirtualClock()
    settings = ShowSettings()
    controller = EffectController(recorded_lighting, clock=clock, settings=lambda: settings)
    await _drop_after(controller, recorded_lighting, clock, 0)

    settings = ShowSettings(drop_strobe_cooldown_seconds=10)
    later = await _drop_after(controller, recorded_lighting, clock, 11)
    assert [e['command']['kind'] for e in later] == ['select', 'set_override']
