import dataclasses
import random
from collections import Counter

import pytest

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.special_policy import (
    DROP_STROBE,
    Special,
    SpecialCapabilities,
    SpecialPolicy,
    Venue,
)
from autolux.lighting.special_catalogue import SpecialError, discover_specials, read_venue

DROP, ATMOSPHERIC = LightIntent.DROP, LightIntent.ATMOSPHERIC
FX = {
    'name': 'fx',
    'cell_count': 1,
    'colour': True,
    'speed': True,
    'pulse': True,
    'pattern': False,
    'role': 'accent',
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


def test_a_texture_cannot_return_inside_its_group_cooldown():
    policy = SpecialPolicy([row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)], random.Random(0))
    assert fire(policy, ATMOSPHERIC, 1000.0).name == 'drift'
    assert fire(policy, ATMOSPHERIC, 1239.0) is None
    assert fire(policy, ATMOSPHERIC, 1240.0).name == 'drift'


def test_a_texture_rests_its_group_so_no_accent_or_other_texture_plays_there():
    drift = row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)
    cold = row('cold_drift', ATMOSPHERIC, speed=0.1, cooldown=240)
    spin = row('spin', pulse=1, cooldown=90)
    policy = SpecialPolicy([drift, cold, spin, DROP_STROBE], random.Random(0))
    assert fire(policy, ATMOSPHERIC, 1000.0) is not None
    assert fire(policy, ATMOSPHERIC, 1100.0) is None
    # A drop inside the texture's 240 s keeps its strobe burst but plays nothing on fx.
    choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]), now=1239.0)
    assert (choice.special, choice.strobes) == (None, (DROP_STROBE,))
    assert fire(policy, DROP, 1240.0).name == 'spin'


def test_an_accent_frees_its_group_after_its_own_cooldown():
    drift = row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)
    policy = SpecialPolicy([drift, row('spin', pulse=1, cooldown=90)], random.Random(0))
    assert fire(policy, DROP, 1000.0).name == 'spin'
    assert fire(policy, ATMOSPHERIC, 1089.0) is None
    assert fire(policy, ATMOSPHERIC, 1090.0).name == 'drift'


def test_a_drop_accent_waits_out_its_group_cooldown_after_the_shared_one_expires():
    policy = SpecialPolicy([row('spin', pulse=1, cooldown=90), DROP_STROBE], random.Random(0))
    assert fire(policy, DROP, 1000.0).name == 'spin'
    # 70 s on, the shared 60 s intense cooldown has run out (no block) but the group's has not.
    choice = policy.choose(DROP, SpecialCapabilities.from_groups([FX]), now=1070.0)
    assert (choice.special, choice.strobes) == (None, (DROP_STROBE,))
    assert fire(policy, DROP, 1090.0).name == 'spin'


def test_each_group_rests_on_its_own():
    movers = {**FX, 'name': 'movers'}
    capabilities = SpecialCapabilities.from_groups([FX, movers])
    fx_drift = row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)
    sweep = row('sweep', ATMOSPHERIC, group='movers', speed=0.1, cooldown=240)
    policy = SpecialPolicy([fx_drift, sweep], random.Random(0))
    first = policy.choose(ATMOSPHERIC, capabilities, now=1000.0).special
    policy.record(first, 1000.0)
    # The first group's rest leaves the other group free.
    second = policy.choose(ATMOSPHERIC, capabilities, now=1010.0).special
    policy.record(second, 1010.0)
    assert {first.group, second.group} == {'fx', 'movers'}
    assert policy.choose(ATMOSPHERIC, capabilities, now=1239.0).special is None
    assert policy.choose(ATMOSPHERIC, capabilities, now=1249.0).special == first


LASER = {**FX, 'name': 'laser', 'pulse': True}
WASH = {**FX, 'name': 'wash', 'role': 'loop'}


def test_a_role_row_plays_on_every_capable_group_of_its_role_and_each_rests_alone():
    spins = [s for s in discover_specials() if s.name in ('spin', 'cold_spin')]
    capabilities = SpecialCapabilities.from_groups([FX, LASER, WASH])
    assert {row.group for s in spins for row in capabilities.targets(s)} == {'fx', 'laser'}
    # A laser that cannot rotate does not inherit the spin rows.
    assert [
        row.group
        for row in SpecialCapabilities.from_groups([FX, LASER | {'speed': False}]).targets(spins[0])
    ] == ['fx']
    policy = SpecialPolicy(spins, random.Random(0))
    first = policy.choose(DROP, capabilities, now=1000.0).special
    policy.record(first, 1000.0)
    second = policy.choose(DROP, capabilities, now=1010.0).special
    policy.record(second, 1010.0)
    assert {first.group, second.group} == {'fx', 'laser'}
    assert policy.choose(DROP, capabilities, now=1089.0).special is None
    third = policy.choose(DROP, capabilities, now=1091.0).special
    assert third.group == first.group
    resting = {e['group']: e['ready'] for e in policy.cooldowns(1091.0, 0.0, 0.0, capabilities)}
    resting.pop('venue')
    assert resting == {'fx': first.group == 'fx', 'laser': first.group == 'laser'}


def test_a_zoned_role_row_plays_only_on_groups_lying_in_that_zone():
    left = FX | {'name': 'left', 'zone': 'house_left', 'depth': 'upstage', 'x': -3.0}
    spread = FX | {'name': 'spread', 'zone': None, 'depth': 'upstage', 'x': 0.0}
    right = FX | {'name': 'right', 'zone': 'house_right', 'depth': 'downstage', 'x': 3.0}
    capabilities = SpecialCapabilities.from_groups([left, spread, right])
    spin = dataclasses.replace(row('spin'), group='', role='accent')
    groups = lambda **narrowed: [
        r.group for r in capabilities.targets(dataclasses.replace(spin, **narrowed))
    ]
    assert groups() == ['left', 'right', 'spread']
    assert groups(zone='house_left') == ['left']
    assert groups(zone='centre') == []
    assert groups(depth='upstage') == ['left', 'spread']
    assert groups(zone='house_right', depth='upstage') == []


def three_accents():
    places = {'left': -4.0, 'centre': 0.5, 'right': 4.0}
    return SpecialCapabilities.from_groups(
        [FX | {'name': name, 'x': x} for name, x in places.items()]
    )


@pytest.mark.parametrize('seed', range(5))
def test_ready_accent_groups_alternate_across_the_room_by_x(seed):
    capabilities = three_accents()
    spin = dataclasses.replace(row('spin'), group='', role='accent')
    policy = SpecialPolicy([spin], random.Random(seed), Venue(max_accent_events_per_minute=99))
    fired = []
    for now in range(4):
        chosen = policy.choose(DROP, capabilities, now=float(now)).special
        policy.record(chosen, float(now), capabilities)
        fired.append(chosen.group)
    # House left first, then always the ready group furthest away, whatever the seed.
    assert fired == ['left', 'right', 'left', 'right']


def test_a_resting_accent_group_lets_the_next_furthest_take_its_turn():
    capabilities = three_accents()
    spin = dataclasses.replace(row('spin', cooldown=25.0), group='', role='accent')
    policy = SpecialPolicy([spin], random.Random(0), Venue(max_accent_events_per_minute=99))
    fired = []
    for now in (0.0, 10.0, 20.0, 30.0):
        chosen = policy.choose(DROP, capabilities, now=now).special
        policy.record(chosen, now, capabilities)
        fired.append(chosen.group)
    # At 20 s house left still rests, so the centre, furthest of the ready, plays.
    assert fired == ['left', 'right', 'centre', 'left']


def test_the_venue_caps_accent_specials_per_minute_across_accent_groups():
    capabilities = SpecialCapabilities.from_groups([FX, LASER, WASH])
    spin = dataclasses.replace(row('spin', pulse=1), group='', role='accent')
    sweep = row('sweep', ATMOSPHERIC, group='wash')
    policy = SpecialPolicy([spin, sweep], random.Random(0), Venue(max_accent_events_per_minute=2))

    def fire(now, intent=DROP):
        chosen = policy.choose(intent, capabilities, now=now).special
        if chosen is not None:
            policy.record(chosen, now, capabilities)
        return chosen and chosen.group

    def venue(now):
        [entry] = [
            e for e in policy.cooldowns(now, 0.0, 0.0, capabilities) if e['group'] == 'venue'
        ]
        return entry['label'], entry['ready'], entry['remaining_seconds']

    assert venue(0.0) == ('venue · accents 0/2', True, 0.0)
    assert sorted([fire(0.0), fire(10.0)]) == ['fx', 'laser']
    # Two accents fill the minute whichever groups played them; a loop group is not capped.
    assert fire(20.0) is None
    assert fire(20.0, ATMOSPHERIC) == 'wash'
    assert venue(20.0) == ('venue · accents 2/2', False, 40.0)
    # The window slides: once the first accent is a minute old, one more may fire.
    assert venue(60.0)[:2] == ('venue · accents 1/2', True)
    assert fire(60.0) in ('fx', 'laser')
    assert venue(60.0) == ('venue · accents 2/2', False, 10.0)


def test_the_venue_file_sets_the_cap_and_rejects_bad_values(tmp_path):
    assert read_venue(tmp_path) == Venue(max_accent_events_per_minute=2)
    assert read_venue() == Venue()
    (tmp_path / 'venue.toml').write_text('[venue]\nmax_accent_events_per_minute = 3\n')
    assert read_venue(tmp_path).max_accent_events_per_minute == 3
    assert discover_specials(tmp_path) == ()
    for text, message in [
        ('[venue]\nmax_accent_events_per_minute = 0\n', 'must be a whole number, at least 1'),
        ('[venue]\nmax_accents = 1\n', 'unknown venue.max_accents'),
    ]:
        (tmp_path / 'venue.toml').write_text(text)
        with pytest.raises(SpecialError, match=message):
            read_venue(tmp_path)


def test_cooldowns_list_one_entry_per_group_and_the_strobe_on_the_shared_cooldown():
    drift = row('drift', ATMOSPHERIC, speed=0.1, cooldown=240)
    spin = row('spin', pulse=1, cooldown=90)
    sweep = row('sweep', ATMOSPHERIC, group='movers', speed=0.1, cooldown=240)
    policy = SpecialPolicy([drift, spin, sweep, DROP_STROBE], random.Random(0))
    policy.record(drift, 1000.0)
    assert policy.cooldowns(1100.0, 60.0, 20.0) == [
        {
            'group': 'fx',
            'last': 'drift',
            'cooldown_seconds': 240,
            'remaining_seconds': 140.0,
            'ready': False,
        },
        {
            'group': 'movers',
            'last': None,
            'cooldown_seconds': 0.0,
            'remaining_seconds': 0.0,
            'ready': True,
        },
        {
            'group': 'all',
            'last': 'strobe_burst',
            'cooldown_seconds': 60.0,
            'remaining_seconds': 20.0,
            'ready': False,
            'strobe': True,
        },
    ]
    policy.record(spin, 1300.0)
    fx = policy.cooldowns(1350.0, 60.0, 0.0)[0]
    assert (fx['last'], fx['cooldown_seconds'], fx['remaining_seconds']) == ('spin', 90, 40.0)
