import random
from collections import Counter

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.special_policy import (
    DROP_STROBE,
    Special,
    SpecialCapabilities,
    SpecialPolicy,
)
from autolux.lighting.special_catalogue import discover_specials

DROP, ATMOSPHERIC = LightIntent.DROP, LightIntent.ATMOSPHERIC
FX = {
    'name': 'fx',
    'cell_count': 1,
    'colour': True,
    'speed': True,
    'pulse': True,
    'pattern': False,
}


def row(name, intent=DROP, *, group='fx', speed=0.3, pulse=None, pattern=None, cooldown=0.0):
    return Special(
        name,
        (intent,),
        group,
        colour_slot='primary',
        energy=0.5,
        speed=speed,
        pulse_beats=pulse,
        pattern=pattern,
        cooldown_seconds=cooldown,
    )


def test_capabilities_keep_populated_groups_with_their_axes():
    capabilities = SpecialCapabilities.from_groups(
        [FX, {'name': 'empty', 'cell_count': 0, 'colour': True}, {'cell_count': 2}]
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


def test_the_shipped_fx_rows_skip_buildups_and_keep_textures_calm():
    policy = SpecialPolicy(discover_specials(), random.Random(0))
    capabilities = SpecialCapabilities.from_groups([FX])
    assert policy.choose(LightIntent.BUILDUP, capabilities).special is None
    for intent in (ATMOSPHERIC, LightIntent.BREAKDOWN):
        assert not policy.choose(intent, capabilities, 'cooldown 30s of 60s left').special.intense
    assert policy.choose(DROP, capabilities).special.group == 'fx'
    blocked = policy.choose(DROP, capabilities, 'cooldown 30s of 60s left')
    assert (blocked.special, blocked.strobes) == (None, ())


def test_a_soft_drop_keeps_the_strobe_and_a_calm_row_but_no_intense_one():
    policy = SpecialPolicy([row('calm'), row('spin', pulse=1), DROP_STROBE], random.Random(0))
    choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]), soft_drop='soft')
    assert (choice.special.name, choice.strobes, choice.suppressed) == (
        'calm',
        (DROP_STROBE,),
        'soft',
    )


def fire(policy, intent, now, blocked=None):
    choice = policy.choose(intent, SpecialCapabilities.from_groups([FX]), blocked, now=now)
    if choice.special is not None:
        policy.record(choice.special, now)
    return choice.special


def test_a_texture_cannot_return_inside_its_row_cooldown():
    policy = SpecialPolicy([row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)], random.Random(0))
    assert fire(policy, ATMOSPHERIC, 1000.0).name == 'drift'
    assert fire(policy, ATMOSPHERIC, 1239.0) is None
    assert fire(policy, ATMOSPHERIC, 1240.0).name == 'drift'


def test_a_drop_accent_waits_out_its_row_cooldown_after_the_shared_one_expires():
    policy = SpecialPolicy([row('spin', pulse=1, cooldown=90), DROP_STROBE], random.Random(0))
    assert fire(policy, DROP, 1000.0).name == 'spin'
    # 70 s on, the shared 60 s intense cooldown has run out (no block) but the row's has not.
    choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]), now=1070.0)
    assert (choice.special, choice.strobes) == (None, (DROP_STROBE,))
    assert fire(policy, DROP, 1090.0).name == 'spin'


def test_cooldowns_list_every_row_and_intense_rows_wait_out_the_shared_cooldown():
    calm = row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)
    spin = row('spin', pulse=1, cooldown=90)
    plain = row('glow')
    policy = SpecialPolicy([calm, spin, plain, DROP_STROBE], random.Random(0))
    policy.record(calm, 1000.0)
    rows = {r['name']: r for r in policy.cooldowns(1100.0, 60.0, 20.0)}
    assert set(rows) == {'drift', 'spin', 'glow', 'strobe_burst'}
    assert rows['drift'] == {
        'name': 'drift',
        'group': 'fx',
        'cooldown_seconds': 240,
        'remaining_seconds': 140.0,
        'ready': False,
    }
    # Never fired, but intense: the shared cooldown alone holds them.
    assert (rows['spin']['cooldown_seconds'], rows['spin']['remaining_seconds']) == (90, 20.0)
    assert (rows['strobe_burst']['cooldown_seconds'], rows['strobe_burst']['ready']) == (60, False)
    assert (rows['glow']['cooldown_seconds'], rows['glow']['ready']) == (0.0, True)
