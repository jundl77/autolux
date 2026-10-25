import logging

import pytest

from autolux.lib.engine.effect_controller import STROBE_DURATION_SEC
from autolux.lib.engine.effect_definitions import LightIntent


async def test_applied_lighting_change_has_one_useful_info_line(effect_controller, caplog):
    with caplog.at_level(logging.INFO):
        await effect_controller.change_effect(LightIntent.BUILDUP)
    assert len(caplog.records) == 1
    assert 'intent=BUILDUP | autoloop=buildup/current' in caplog.text


@pytest.mark.parametrize(
    ('intent', 'autoloop'),
    [
        (LightIntent.ATMOSPHERIC, 'blue_tide'),
        (LightIntent.BREAKDOWN, 'violet_space'),
        (LightIntent.BUILDUP, 'current'),
        (LightIntent.DROP, 'blue_impact'),
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
    assert accent['lifetime'] == {'kind': 'seconds', 'duration_seconds': STROBE_DURATION_SEC}
    assert STROBE_DURATION_SEC == 1.0


async def test_stop_allows_the_same_autoloop_to_resume(effect_controller, recorded_lighting):
    await effect_controller.change_effect(LightIntent.DROP)
    recorded_lighting.stop()
    await effect_controller.change_effect(LightIntent.DROP)
    kinds = [event['command']['kind'] for event in recorded_lighting.events]
    assert kinds == ['select', 'set_override', 'stop', 'select', 'set_override']
