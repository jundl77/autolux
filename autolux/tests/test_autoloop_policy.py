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
    loudness_trend,
)
from autolux.lib.engine.effect_definitions import LightIntent

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
        for name, energy, family, movement in loops:
            meta[(intent.value, name)] = AutoloopMeta(energy, family, movement)
    for intent in LightIntent:
        if not table[intent]:
            table[intent] = ((intent.value, 'only'),)
            meta[(intent.value, 'only')] = AutoloopMeta(1 + (intent == DROP), 'cold', 'sweep')
    return table, meta


TIERED = library(
    {
        intent: [
            (f'e{energy}_{n}', energy, 'cold', 'sweep') for energy in (1, 2, 3) for n in range(2)
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
    lib = library({DROP: [(f'd{n}', 2, 'cold', 'snap') for n in range(5)]})
    for seed in range(10):
        chooser = policy(lib, seed)
        picks = [pick(chooser, DROP, seconds_since_drop=1.0) for _ in range(5)]
        assert len({choice.autoloop for choice in picks}) == 5


def test_a_long_rested_loop_is_likelier_to_return_than_a_recent_one():
    lib = library({DROP: [(f'd{n}', 2, 'cold', 'snap') for n in range(4)]})
    returns = Counter()
    for seed in range(200):
        chooser = policy(lib, seed)
        order = [pick(chooser, DROP, seconds_since_drop=1.0).autoloop for _ in range(4)]
        nxt = pick(chooser, DROP, seconds_since_drop=1.0).autoloop
        returns[order.index(nxt)] += 1
    assert returns[3] == 0
    assert returns[0] > returns[1] > returns[2] > 0


def test_the_loop_used_last_for_an_intent_never_plays_twice_running():
    lib = library({BREAKDOWN: [('a', 1, 'cold', 'still'), ('b', 1, 'cold', 'still')]})
    chooser = policy(lib, 3)
    picks = [pick(chooser, BREAKDOWN).autoloop for _ in range(20)]
    assert all(a != b for a, b in pairwise(picks))


def test_the_track_family_is_preferred_without_excluding_others():
    lib = library({ATMOSPHERIC: [('warm', 1, 'warm', 'still'), ('cold', 1, 'cold', 'still')]})
    matches = Counter()
    for seed in range(300):
        chooser = policy(lib, seed)
        choice = pick(chooser, ATMOSPHERIC, new_track=True)
        if chooser.track_family != 'accent':
            matches[choice.meta.family == chooser.track_family] += 1
    assert matches[True] > 2.5 * matches[False] > 0


def test_a_new_track_follows_the_previous_family_half_the_time():
    followed = 0
    for seed in range(400):
        chooser = policy(seed=seed)
        pick(chooser, ATMOSPHERIC, new_track=True)
        before = chooser.track_family
        pick(chooser, ATMOSPHERIC, new_track=True)
        followed += chooser.track_family == before
    assert 0.42 < followed / 400 < 0.58


def test_a_parked_rig_does_not_snap_unless_a_drop_enters():
    lib = library(
        {
            ATMOSPHERIC: [('parked', 1, 'cold', 'still')],
            BREAKDOWN: [('jump', 1, 'cold', 'snap'), ('glide', 1, 'cold', 'sweep')],
            DROP: [('hit', 3, 'cold', 'snap')],
        }
    )
    for seed in range(20):
        chooser = policy(lib, seed)
        pick(chooser, ATMOSPHERIC, new_track=True)
        assert pick(chooser, BREAKDOWN).autoloop == 'glide'
        pick(chooser, ATMOSPHERIC)
        assert pick(chooser, DROP).meta.movement == 'snap'


def test_an_empty_tier_falls_back_to_the_nearest_energy():
    lib = library({DROP: [('driving', 2, 'cold', 'snap')], BREAKDOWN: [('big', 3, 'cold', 'snap')]})
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
