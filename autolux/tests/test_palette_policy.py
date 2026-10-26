import colorsys
import itertools
import random

import pytest

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.palette_policy import FULL_BIAS_PREFERENCE, PalettePolicy
from autolux.lighting.palettes import (
    BREAKDOWN_ACCENT_SATURATION,
    BREAKDOWN_PRIMARY_LEVEL,
    DEFAULT_PALETTE,
    PALETTES,
    Palette,
    breakdown_variant,
    palette_table,
)


class FirstChoice(random.Random):
    def __init__(self):
        super().__init__(0)
        self.offers = []

    def choices(self, population, weights=None, **_):
        self.offers.append({item.name: weight for item, weight in zip(population, weights)})
        return [population[0]]


def picks(seed, tracks, bias=0.0):
    policy = PalettePolicy(PALETTES, random.Random(seed))
    return [
        policy.palette_for(LightIntent.DROP, new_track=True, temperature_bias=bias)
        for _ in range(tracks)
    ]


def test_the_same_seed_picks_the_same_palettes_and_a_track_never_repeats_the_last():
    first = picks(7, 40)
    assert first == picks(7, 40)
    assert first != picks(8, 40)
    assert all(previous != current for previous, current in itertools.pairwise(first))


def test_every_section_of_a_track_shares_its_palette_and_breakdowns_use_the_variant():
    policy = PalettePolicy(PALETTES, random.Random(3))
    intro = policy.palette_for(LightIntent.ATMOSPHERIC, new_track=True, temperature_bias=0.0)
    sections = [
        policy.palette_for(intent, new_track=False, temperature_bias=0.0)
        for intent in (LightIntent.BREAKDOWN, LightIntent.BUILDUP, LightIntent.DROP)
    ]
    assert sections == [f'{intro}_breakdown', intro, intro]


def test_least_recently_used_palettes_weigh_more():
    rng = FirstChoice()
    policy = PalettePolicy(PALETTES[:4], rng)
    for _ in range(3):
        policy.palette_for(LightIntent.DROP, new_track=True, temperature_bias=0.0)
    assert rng.offers[0] == {'techno': 1, 'house': 1, 'ice': 1, 'acid': 1}
    assert rng.offers[1] == {'house': 2, 'ice': 2, 'acid': 2}
    assert rng.offers[2] == {'techno': 2, 'ice': 3, 'acid': 3}


def test_the_temperature_bias_leans_the_pick_warm_or_cold():
    rng = FirstChoice()
    policy = PalettePolicy(PALETTES, rng)
    policy.palette_for(LightIntent.DROP, new_track=True, temperature_bias=1.0)
    policy.palette_for(LightIntent.DROP, new_track=True, temperature_bias=-0.5)
    warm, cold = rng.offers
    assert (warm['sunset'], warm['house'], warm['ice']) == (
        FULL_BIAS_PREFERENCE,
        1,
        1 / FULL_BIAS_PREFERENCE,
    )
    assert cold['ice'] == pytest.approx(FULL_BIAS_PREFERENCE**0.5 * 2)
    assert cold['sunset'] == pytest.approx(FULL_BIAS_PREFERENCE**-0.5 * 2)
    counts = {
        bias: sum(
            next(p for p in PALETTES if p.name == name).temperature == 'warm'
            for name in picks(11, 200, bias)
        )
        for bias in (-1.0, 1.0)
    }
    assert counts[1.0] > 2 * counts[-1.0]


def test_the_breakdown_variant_complements_the_accent_and_dims_the_primary():
    palette = Palette(
        'test',
        'warm',
        primary=(1.0, 0.5, 0.25),
        secondary=(0.1, 0.2, 0.3),
        accent=(1.0, 0.0, 0.0),
        cold=(0.7, 0.8, 0.9),
    )
    variant = breakdown_variant(palette)
    assert variant.name == 'test_breakdown'
    assert variant.primary == pytest.approx(
        tuple(channel * BREAKDOWN_PRIMARY_LEVEL for channel in palette.primary)
    )
    hue, saturation, value = colorsys.rgb_to_hsv(*variant.accent)
    assert (hue, saturation, value) == pytest.approx((0.5, BREAKDOWN_ACCENT_SATURATION, 1.0))
    assert (variant.secondary, variant.cold, variant.black, variant.temperature) == (
        palette.secondary,
        palette.cold,
        palette.black,
        palette.temperature,
    )


def test_the_login_table_starts_with_the_default_and_pairs_each_palette_with_its_variant():
    table = palette_table()
    assert table[0].name == DEFAULT_PALETTE == 'techno'
    assert [palette.name for palette in table[1::2]] == [
        f'{palette.name}_breakdown' for palette in PALETTES
    ]
    assert len({palette.name for palette in table}) == 2 * len(PALETTES) == 18


@pytest.mark.parametrize(
    'change',
    [{'name': 'Ice'}, {'temperature': 'hot'}, {'accent': (1.2, 0.0, 0.0)}, {'cold': (1.0, 1.0)}],
)
def test_invalid_palettes_are_rejected(change):
    fields = {
        'name': 'ice',
        'temperature': 'cold',
        'primary': (0.0, 0.0, 1.0),
        'secondary': (0.0, 1.0, 1.0),
        'accent': (1.0, 1.0, 1.0),
        'cold': (0.9, 0.9, 1.0),
    }
    with pytest.raises(ValueError):
        Palette(**(fields | change))
