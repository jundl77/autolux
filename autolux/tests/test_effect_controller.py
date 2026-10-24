import logging
from unittest.mock import AsyncMock

import pytest

from autolux.lib.engine.effect_controller import STROBE_DURATION_SEC
from autolux.lib.engine.effect_definitions import LightIntent


async def test_applied_lighting_change_has_one_useful_info_line(effect_controller, caplog):
    with caplog.at_level(logging.INFO):
        await effect_controller.change_effect(LightIntent.BUILDUP)
    assert len(caplog.records) == 1
    assert 'intent=BUILDUP | effect=AUTOLOOP_BANK_1A' in caplog.text


@pytest.mark.parametrize(
    ('intent', 'channel'),
    [
        (LightIntent.ATMOSPHERIC, 'AUTOLOOP_BANK_2A'),
        (LightIntent.BREAKDOWN, 'AUTOLOOP_BANK_2F'),
        (LightIntent.BUILDUP, 'AUTOLOOP_BANK_1A'),
        (LightIntent.DROP, 'AUTOLOOP_BANK_1F'),
    ],
)
async def test_section_uses_its_authored_loop_every_time(
    effect_controller, stub_midi, monkeypatch, intent, channel
):
    monkeypatch.setattr('autolux.lib.engine.effect_controller.random.random', lambda: 1.0)
    for _ in range(3):
        await effect_controller.change_effect(intent)
    assert [event['channel'] for event in stub_midi.events] == [channel] * 3


async def test_strobe_plays_over_the_drop_loop(effect_controller, stub_midi, monkeypatch):
    monkeypatch.setattr('autolux.lib.engine.effect_controller.random.random', lambda: 0.0)
    await effect_controller.change_effect(LightIntent.BREAKDOWN)
    await effect_controller.change_effect(LightIntent.DROP)
    assert [(event['label'], event['channel']) for event in stub_midi.events] == [
        ('set_autoloop', 'AUTOLOOP_BANK_2F'),
        ('set_autoloop', 'AUTOLOOP_BANK_1F'),
        ('set_special_effect', 'SPECIAL_EFFECT_STROBE'),
    ]
    assert stub_midi.events[-1]['duration_sec'] == STROBE_DURATION_SEC


@pytest.mark.parametrize('intent', list(LightIntent))
async def test_authored_palette_is_not_replaced(effect_controller, stub_midi, intent):
    stub_midi.clear_color_overrides = AsyncMock()
    await effect_controller.change_effect(intent)
    stub_midi.clear_color_overrides.assert_awaited_once()
    assert not any(event['label'] == 'set_color_override' for event in stub_midi.events)
