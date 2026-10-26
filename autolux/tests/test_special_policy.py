import random
from collections import Counter

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.special_policy import (
    DROP_STROBE,
    Special,
    SpecialCapabilities,
    SpecialPolicy,
)

DROP, ATMOSPHERIC = LightIntent.DROP, LightIntent.ATMOSPHERIC
FX = {
    'name': 'fx',
    'fixture_count': 1,
    'colour': True,
    'speed': True,
    'pulse': True,
    'pattern': False,
}


def row(name, intent=DROP, *, group='fx', speed=0.3, pulse=None, pattern=None):
    return Special(
        name,
        (intent,),
        group,
        colour_slot='primary',
        energy=0.5,
        speed=speed,
        pulse_beats=pulse,
        pattern=pattern,
    )


def test_capabilities_keep_populated_groups_with_their_axes():
    capabilities = SpecialCapabilities.from_groups(
        [FX, {'name': 'empty', 'fixture_count': 0, 'colour': True}, {'fixture_count': 2}]
    )
    assert capabilities.groups == {'fx': frozenset({'colour', 'speed', 'pulse'})}


def test_a_row_needs_its_group_and_every_axis_it_sets():
    capabilities = SpecialCapabilities.from_groups([FX])
    assert capabilities.allows(row('spin', pulse=1))
    assert not capabilities.allows(row('gobo', pattern=0.5))
    assert not capabilities.allows(row('elsewhere', group='laser'))
    assert not SpecialCapabilities.from_groups([FX | {'speed': False}]).allows(row('spin'))
    assert SpecialCapabilities.from_groups([FX | {'speed': False}]).allows(row('still', speed=0))


def test_an_empty_capability_list_leaves_only_whole_rig_rows():
    policy = SpecialPolicy([row('spin', pulse=1), DROP_STROBE], random.Random(0))
    choice = policy.choose(DROP, SpecialCapabilities())
    assert (choice.special, choice.strobes) == (None, (DROP_STROBE,))


def test_the_strobe_burst_always_joins_the_chosen_row_and_is_never_selected():
    policy = SpecialPolicy([row('spin', pulse=1), DROP_STROBE], random.Random(0))
    for _ in range(5):
        choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]))
        policy.record(choice.special)
        assert (choice.special.name, choice.strobes) == ('spin', (DROP_STROBE,))
        assert choice.intense


def test_a_blocked_drop_drops_the_strobe_and_keeps_a_calm_row():
    policy = SpecialPolicy([row('calm'), row('spin', pulse=1), DROP_STROBE], random.Random(0))
    choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]), 'cooldown')
    assert (choice.special.name, choice.strobes, choice.suppressed) == ('calm', (), 'cooldown')
    assert not choice.intense


def test_every_row_plays_before_any_repeats_and_the_seed_fixes_the_order():
    def sequence(seed):
        policy = SpecialPolicy([row(f'r{n}') for n in range(4)], random.Random(seed))
        names = []
        for _ in range(8):
            special = policy.choose(DROP, SpecialCapabilities.from_groups([FX])).special
            policy.record(special)
            names.append(special.name)
        return names

    names = sequence(3)
    assert sorted(names[:4]) == ['r0', 'r1', 'r2', 'r3']
    assert names == sequence(3)


def test_least_recent_rows_are_favoured_after_the_first_round():
    policy = SpecialPolicy([row('a'), row('b'), row('c')], random.Random(1))
    capabilities = SpecialCapabilities.from_groups([FX])
    picks = Counter()
    last = None
    for _ in range(300):
        special = policy.choose(DROP, capabilities).special
        policy.record(special)
        picks[special.name == last] += 1
        last = special.name
    assert picks[True] < picks[False] / 3


def test_blocked_intense_rows_leave_the_calm_ones_and_say_why():
    policy = SpecialPolicy([row('calm'), row('spin', pulse=1)], random.Random(0))
    choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]), 'cooldown 10s of 60s left')
    assert choice.special.name == 'calm'
    assert choice.suppressed == 'cooldown 10s of 60s left'


def test_an_intent_without_rows_chooses_nothing_and_reports_no_suppression():
    policy = SpecialPolicy([row('calm', ATMOSPHERIC)], random.Random(0))
    choice = policy.choose(LightIntent.BREAKDOWN, SpecialCapabilities.from_groups([FX]), 'gate')
    assert choice.special is None and choice.suppressed is None
