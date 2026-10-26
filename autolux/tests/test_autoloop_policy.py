import random
from collections import Counter
from itertools import pairwise

import pytest

from autolux.lib.engine.autoloop_policy import (
    FALLING,
    FLAT,
    LOUDNESS_TREND_BEATS,
    RISING,
    AutoloopMeta,
    AutoloopPolicy,
    SelectionContext,
    arrival_of_peak,
    loudness_trend,
    max_energy_for_intensity,
)
from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lighting.autoloop_catalogue import discover_autoloops, read_autoloop_meta

DROP, BUILDUP, BREAKDOWN, ATMOSPHERIC = (
    LightIntent.DROP,
    LightIntent.BUILDUP,
    LightIntent.BREAKDOWN,
    LightIntent.ATMOSPHERIC,
)


def library(spec):
    table = {intent: () for intent in LightIntent}
    meta = {}
    for intent, loops in spec.items():
        table[intent] = tuple((intent.value, loop[0]) for loop in loops)
        for name, energy, movement, texture in loops:
            meta[(intent.value, name)] = AutoloopMeta(energy, movement, texture)
    for intent in LightIntent:
        if not table[intent]:
            table[intent] = ((intent.value, 'only'),)
            meta[(intent.value, 'only')] = AutoloopMeta(1 + (intent == DROP), 'sweep', 'mixed')
    return table, meta


TIERED = library(
    {
        intent: [
            (f'e{energy}_{n}', energy, 'sweep', 'mixed') for energy in (1, 2, 3) for n in range(2)
        ]
        for intent in LightIntent
    }
)


def policy(lib=TIERED, seed=0):
    return AutoloopPolicy(*lib, random.Random(seed))


def pick(chooser, intent, **facts):
    context = SelectionContext(intent, **facts)
    choice = chooser.choose(context)
    chooser.record(context, choice)
    return choice


def drop_with_energy(chooser, energy):
    facts = {'seconds_since_drop': None} if energy == 3 else {'seconds_since_drop': 10.0}
    if energy == 2:
        pick(chooser, DROP)
    assert pick(chooser, DROP, **facts).meta.energy == energy


@pytest.mark.parametrize(
    ('last_drop', 'intent', 'facts', 'energies', 'reason'),
    [
        (None, DROP, {}, {3}, 'long build or long since the last drop'),
        (3, DROP, {'build_bars': 64}, {2}, 'no maximal drop twice running'),
        (2, DROP, {'seconds_since_drop': 30, 'build_bars': 16}, {2}, 'short build'),
        (2, DROP, {'seconds_since_drop': 30, 'build_bars': 32}, {3}, 'long build'),
        (2, DROP, {'seconds_since_drop': 90, 'build_bars': 8}, {3}, 'long since the last drop'),
        (None, DROP, {'arrival_of_peak': 0.9}, {3}, 'arrival 0.90 of the track peak'),
        (None, DROP, {'arrival_of_peak': 0.89}, {2}, 'arrival below 0.9 of the track peak'),
        (2, DROP, {'arrival_of_peak': 0.5, 'build_bars': 64}, {2}, 'arrival below 0.9'),
        (
            3,
            DROP,
            {'arrival_of_peak': 0.95, 'build_bars': 32, 'seconds_since_drop': 90},
            {3},
            'maximal again: 32-bar build, 90 s since the last drop, arrival at the track peak',
        ),
        (
            3,
            DROP,
            {'arrival_of_peak': 0.95, 'build_bars': 31, 'seconds_since_drop': 300},
            {2},
            'no maximal drop twice running',
        ),
        (
            3,
            DROP,
            {'arrival_of_peak': 0.95, 'build_bars': 64, 'seconds_since_drop': 89},
            {2},
            'no maximal drop twice running',
        ),
        (
            3,
            DROP,
            {'arrival_of_peak': 0.89, 'build_bars': 64, 'seconds_since_drop': 300},
            {2},
            'arrival below 0.9',
        ),
        (3, DROP, {'build_bars': 64, 'seconds_since_drop': 300}, {2}, 'no maximal drop twice'),
        (None, BUILDUP, {}, {3}, 'leads into a maximal drop'),
        (3, BUILDUP, {'build_bars': 64}, {2}, 'next drop cannot be maximal'),
        (2, BUILDUP, {'seconds_since_drop': 30, 'build_bars': 8}, {2}, 'leads into a driving'),
        (2, BUILDUP, {'seconds_since_drop': 30, 'build_bars': 40}, {3}, 'maximal drop'),
        (3, BREAKDOWN, {}, {1}, 'calm after a maximal drop'),
        (2, BREAKDOWN, {}, {1, 2}, 'breakdown'),
        (None, ATMOSPHERIC, {'loudness': FLAT}, {1}, 'calm'),
        (None, ATMOSPHERIC, {'loudness': RISING}, {1, 2}, 'loudness rising'),
    ],
)
def test_each_tier_rule_picks_its_energies(last_drop, intent, facts, energies, reason):
    seen = set()
    for seed in range(40):
        chooser = policy(seed=seed)
        if last_drop is not None:
            drop_with_energy(chooser, last_drop)
        choice = pick(chooser, intent, **facts)
        assert reason in choice.reason
        seen.add(choice.meta.energy)
    assert seen == energies


def test_a_tier_is_exhausted_before_any_loop_repeats():
    lib = library({DROP: [(f'd{n}', 2, 'snap', 'wash') for n in range(5)]})
    for seed in range(10):
        chooser = policy(lib, seed)
        picks = [pick(chooser, DROP, seconds_since_drop=1.0) for _ in range(5)]
        assert len({choice.autoloop for choice in picks}) == 5


def test_a_long_rested_loop_is_likelier_to_return_than_a_recent_one():
    lib = library({DROP: [(f'd{n}', 2, 'snap', 'wash') for n in range(4)]})
    returns = Counter()
    for seed in range(200):
        chooser = policy(lib, seed)
        order = [pick(chooser, DROP, seconds_since_drop=1.0).autoloop for _ in range(4)]
        nxt = pick(chooser, DROP, seconds_since_drop=1.0).autoloop
        returns[order.index(nxt)] += 1
    assert returns[3] == 0
    assert returns[0] > returns[1] > returns[2] > 0


def test_the_loop_used_last_for_an_intent_never_plays_twice_running():
    lib = library({BREAKDOWN: [('a', 1, 'still', 'wash'), ('b', 1, 'still', 'wash')]})
    chooser = policy(lib, 3)
    picks = [pick(chooser, BREAKDOWN).autoloop for _ in range(20)]
    assert all(a != b for a, b in pairwise(picks))


def test_the_track_movement_is_set_by_a_new_track_only():
    lib = library(
        {
            ATMOSPHERIC: [('parked', 1, 'still', 'wash')],
            DROP: [('hit', 3, 'snap', 'wash')],
        }
    )
    chooser = policy(lib)
    assert chooser.track_movement is None
    pick(chooser, DROP)
    assert chooser.track_movement is None
    pick(chooser, ATMOSPHERIC, new_track=True)
    assert chooser.track_movement == 'still'
    pick(chooser, DROP)
    assert chooser.track_movement == 'still'
    pick(chooser, DROP, new_track=True)
    assert chooser.track_movement == 'snap'


def test_a_still_track_may_still_enter_any_movement():
    lib = library(
        {
            ATMOSPHERIC: [('parked', 1, 'still', 'wash')],
            BREAKDOWN: [('jump', 1, 'snap', 'wash'), ('glide', 1, 'sweep', 'wash')],
        }
    )
    seen = set()
    for seed in range(20):
        chooser = policy(lib, seed)
        pick(chooser, ATMOSPHERIC, new_track=True)
        seen.add(pick(chooser, BREAKDOWN).autoloop)
    assert seen == {'jump', 'glide'}


def test_an_empty_tier_falls_back_to_the_nearest_energy():
    lib = library({DROP: [('driving', 2, 'snap', 'wash')], BREAKDOWN: [('big', 3, 'snap', 'wash')]})
    chooser = policy(lib)
    choice = pick(chooser, DROP)
    assert (choice.autoloop, choice.meta.energy) == ('driving', 2)
    assert 'no energy 3 loop, nearest 2' in choice.reason
    assert pick(chooser, BREAKDOWN).autoloop == 'big'


def test_a_seeded_policy_replays_the_same_show():
    def show(seed):
        chooser = policy(seed=seed)
        return [
            pick(chooser, intent, new_track=t == 0).autoloop
            for t, intent in enumerate([ATMOSPHERIC, BUILDUP, DROP, BREAKDOWN, DROP] * 4)
        ]

    assert show(11) == show(11)
    assert any(show(11) != show(seed) for seed in range(12, 20))


def test_a_loop_without_metadata_is_refused():
    table, meta = library({})
    del meta[('drop', 'only')]
    with pytest.raises(ValueError, match='without metadata: drop/only'):
        AutoloopPolicy(table, meta, random.Random(0))


@pytest.mark.parametrize(
    ('levels', 'trend'),
    [
        ([0.5] * LOUDNESS_TREND_BEATS, FLAT),
        ([0.3] * 16 + [0.5] * 16, RISING),
        ([0.6] * 16 + [0.4] * 16, FALLING),
        ([0.1] * 16 + [0.9] * 15, FLAT),
    ],
)
def test_loudness_trend_compares_the_halves_of_eight_bars(levels, trend):
    assert loudness_trend(levels) == trend


@pytest.mark.parametrize(
    ('intensity', 'max_energy'), [(0.0, 1), (0.34, 1), (0.35, 2), (0.69, 2), (0.7, 3), (1.0, 3)]
)
def test_intensity_maps_to_the_highest_allowed_energy(intensity, max_energy):
    assert max_energy_for_intensity(intensity) == max_energy


@pytest.mark.parametrize(('intensity', 'energy'), [(0.2, 1), (0.5, 2), (0.9, 3)])
def test_intensity_caps_a_maximal_drop_after_the_context_rule(intensity, energy):
    chooser = policy()
    choice = chooser.choose(SelectionContext(DROP, build_bars=64), intensity=intensity)
    assert choice.meta.energy == energy
    assert ('intensity caps energy' in choice.reason) == (energy < 3)


def test_a_capped_tier_without_loops_falls_back_to_the_nearest_energy():
    lib = library({DROP: [('driving', 2, 'snap', 'wash'), ('maximal', 3, 'snap', 'wash')]})
    choice = policy(lib).choose(SelectionContext(DROP, build_bars=64), intensity=0.1)
    assert choice.autoloop == 'driving'
    assert 'no energy 1 loop, nearest 2' in choice.reason


@pytest.mark.parametrize(
    ('beats', 'arrival_sec', 'track_started_sec', 'expected'),
    [
        ([], 10.0, 0.0, None),
        ([(1.0, 0.8), (2.0, 0.9)], 10.0, 0.0, None),
        ([(9.0, 0.5), (10.0, 0.9), (10.5, 0.9), (11.0, 0.9), (11.5, 0.9)], 10.0, 0.0, 1.0),
        ([(5.0, 1.0), (10.0, 0.8), (10.5, 0.8), (11.0, 0.8), (11.5, 0.8)], 10.0, 0.0, 0.8),
        ([(5.0, 1.0), (10.0, 0.8), (10.5, 0.8)], 10.0, 6.0, 1.0),
        (
            [(5.0, 1.0), (10.0, 0.6), (10.5, 0.6), (11.0, 0.6), (11.5, 0.6), (12.0, 1.0)],
            10.0,
            0.0,
            0.6,
        ),
        ([(10.0, 0.0)], 10.0, 0.0, None),
    ],
)
def test_arrival_is_the_first_four_drop_beats_against_the_track_peak(
    beats, arrival_sec, track_started_sec, expected
):
    result = arrival_of_peak(beats, arrival_sec, track_started_sec)
    assert result == (None if expected is None else pytest.approx(expected))


def test_the_shipped_breakdowns_calm_down_after_a_maximal_drop_only():
    autoloops, meta = discover_autoloops(), read_autoloop_meta()
    breakdowns = {meta[loop].energy for loop in autoloops[BREAKDOWN]}
    assert breakdowns == {1, 2}
    after_maximal, otherwise = set(), set()
    for seed in range(60):
        chooser = AutoloopPolicy(autoloops, meta, random.Random(seed))
        pick(chooser, DROP, seconds_since_drop=None, build_bars=64)
        after_maximal.add(pick(chooser, BREAKDOWN).meta.energy)
        chooser = AutoloopPolicy(autoloops, meta, random.Random(seed))
        pick(chooser, DROP, seconds_since_drop=10.0)
        otherwise.add(pick(chooser, BREAKDOWN).meta.energy)
    assert after_maximal == {1}
    assert otherwise == {1, 2}


def build_engine():
    from autolux.lib.clock import VirtualClock
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.lib.engine.light_engine import LightEngine
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock)
    light = LightEngine(lighting, EffectController(lighting), None, clock=clock)
    light._published_bpm['last'] = 120.0
    return light


BAR_SECONDS_AT_120 = 2.0


def test_a_short_atmospheric_interruption_keeps_the_build_clock():
    light = build_engine()
    light._note_new_look(BUILDUP, 0.0)
    light._note_new_look(ATMOSPHERIC, 10.0)
    resumed_at = 10.0 + 8 * BAR_SECONDS_AT_120
    assert light._selection_context(BUILDUP, 'buildup', resumed_at).build_bars == pytest.approx(13)
    light._note_new_look(BUILDUP, resumed_at)
    drop = light._selection_context(DROP, 'drop', 40.0)
    assert drop.build_bars == pytest.approx(20)


def test_a_long_or_repeated_interruption_restarts_the_build_clock():
    light = build_engine()
    light._note_new_look(BUILDUP, 0.0)
    light._note_new_look(ATMOSPHERIC, 10.0)
    late = 10.0 + 8 * BAR_SECONDS_AT_120 + 0.1
    assert light._selection_context(BUILDUP, 'buildup', late).build_bars == 0.0
    light._note_new_look(BUILDUP, late)
    assert light._selection_context(DROP, 'drop', late + 4.0).build_bars == pytest.approx(2)

    light._note_new_look(ATMOSPHERIC, 50.0)
    light._note_new_look(ATMOSPHERIC, 52.0)
    assert light._selection_context(BUILDUP, 'buildup', 54.0).build_bars == 0.0


async def test_a_drop_carries_its_arrival_grade_into_the_selection():
    light = build_engine()
    light._room_playing = True
    await light._apply_intent(None, DROP, None, 'drop', 0.75)
    assert light._selection_context(DROP, 'drop', 1.0).arrival_of_peak == 0.75
    assert light._selection_context(BUILDUP, 'buildup', 1.0).arrival_of_peak is None
