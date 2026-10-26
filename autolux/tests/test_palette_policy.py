import colorsys
import itertools
import random

import pytest

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.palette_policy import PalettePolicy
from autolux.lighting.palettes import (
    BREAKDOWN_PALETTES,
    DEFAULT_PALETTE,
    PALETTES,
    Palette,
    palette_table,
)

SAME_FAMILY_HUE_DEGREES = 45.0
CHROMATIC_SATURATION = 0.3


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


def test_a_recently_used_palette_is_offered_less_than_unused_ones():
    rng = FirstChoice()
    policy = PalettePolicy(PALETTES[:4], rng)
    for _ in range(3):
        policy.palette_for(LightIntent.DROP, new_track=True, temperature_bias=0.0)
    first, second, third = rng.offers
    assert len(set(first.values())) == 1
    assert 'techno' not in second and len(set(second.values())) == 1
    assert 'house' not in third
    assert third['techno'] < third['ice'] == third['acid']


def by_temperature(offer, temperature):
    return {
        offer[palette.name]
        for palette in PALETTES
        if palette.temperature == temperature and palette.name in offer
    }


@pytest.mark.parametrize(
    ('bias', 'order'),
    [(1.0, ('warm', 'neutral', 'cold')), (-1.0, ('cold', 'neutral', 'warm'))],
)
def test_the_temperature_bias_orders_warm_and_cold_palettes(bias, order):
    rng = FirstChoice()
    PalettePolicy(PALETTES, rng).palette_for(
        LightIntent.DROP, new_track=True, temperature_bias=bias
    )
    [offer] = rng.offers
    favoured, middle, disfavoured = (by_temperature(offer, name) for name in order)
    assert min(favoured) > max(middle) > max(disfavoured)


def test_a_warm_bias_picks_warm_palettes_more_often_than_a_cold_one():
    counts = {
        bias: sum(
            next(p for p in PALETTES if p.name == name).temperature == 'warm'
            for name in picks(11, 200, bias)
        )
        for bias in (-1.0, 1.0)
    }
    assert counts[1.0] > 2 * counts[-1.0]


def hsv(colour):
    hue, saturation, value = colorsys.rgb_to_hsv(*colour)
    return hue * 360.0, saturation, value


def hue_distance(first, second):
    return min(abs(first - second), 360.0 - abs(first - second))


@pytest.mark.parametrize('base', PALETTES, ids=lambda palette: palette.name)
def test_each_breakdown_variant_keeps_its_palette_identity(base):
    breakdown = next(p for p in BREAKDOWN_PALETTES if p.name == f'{base.name}_breakdown')
    assert (breakdown.temperature, breakdown.secondary, breakdown.cold, breakdown.black) == (
        base.temperature,
        base.secondary,
        base.cold,
        base.black,
    )
    _, base_saturation, base_value = hsv(base.primary)
    _, saturation, value = hsv(breakdown.primary)
    assert saturation < base_saturation
    assert value == pytest.approx(base_value)
    base_hue, base_accent_saturation, _ = hsv(base.accent)
    hue, accent_saturation, _ = hsv(breakdown.accent)
    assert breakdown.accent != base.accent
    assert accent_saturation <= max(base_accent_saturation, CHROMATIC_SATURATION)
    if base_accent_saturation >= CHROMATIC_SATURATION:
        assert hue_distance(hue, base_hue) <= SAME_FAMILY_HUE_DEGREES


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
