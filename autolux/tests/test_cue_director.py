import itertools
import random

import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.autoloop_policy import AutoloopPolicy, SelectionContext
from autolux.lib.engine.cue_director import (
    CueDirector,
    SectionRequest,
    cue_seed,
    dip_beats,
)
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import (
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    AutoloopMeta,
    LightIntent,
    OverlayMeta,
    ShowSettings,
)
from autolux.lib.engine.special_policy import Venue
from autolux.simulate.lighting_recorder import RecordingStarfireClient

A, B, D, BR = (
    LightIntent.ATMOSPHERIC,
    LightIntent.BUILDUP,
    LightIntent.DROP,
    LightIntent.BREAKDOWN,
)
BPM = 120.0
BEAT_SEC = 60.0 / BPM

META = {
    ('atmospheric', 'glow'): AutoloopMeta(1, 'still', 'wash'),
    ('atmospheric', 'drift'): AutoloopMeta(1, 'sweep', 'beam'),
    ('breakdown', 'pulse'): AutoloopMeta(1, 'still', 'wash'),
    ('breakdown', 'float'): AutoloopMeta(1, 'sweep', 'beam'),
    ('buildup', 'climb'): AutoloopMeta(2, 'sweep', 'pixel'),
    ('buildup', 'rise'): AutoloopMeta(2, 'still', 'wash'),
    ('drop', 'stomp'): AutoloopMeta(2, 'snap', 'wash'),
    ('drop', 'stomp_twin'): AutoloopMeta(2, 'snap', 'wash'),
    ('drop', 'fan'): AutoloopMeta(2, 'sweep', 'beam'),
    ('drop', 'lift'): AutoloopMeta(3, 'sweep', 'beam'),
}
LTP, HTP = OverlayMeta(strobe=False, intensity_htp=False), OverlayMeta(False, True)
STROBE = OverlayMeta(strobe=True, intensity_htp=True)
OVERLAYS = {
    ('dip', 'last_beat'): LTP,
    ('dip', 'fade'): LTP,
    ('hit', 'colour_bump'): HTP,
    ('hit', 'blinder'): HTP,
    ('hit', 'strobe'): STROBE,
    ('riser', 'strip_fill'): HTP,
    ('riser', 'strobe_ramp'): STROBE,
    ('fill', 'wipe'): LTP,
    ('fill', 'turnaround'): LTP,
    ('accent', 'sparkle'): LTP,
    ('accent', 'mover_shapes'): LTP,
}
LEVELS = {'calm': INTENSITY_CALM, 'balanced': INTENSITY_BALANCED, 'wild': INTENSITY_WILD}
# Atmospheric, buildup, drop, breakdown, buildup, drop: start beats, 16 or 32 bars apart.
SET = ((A, 0), (B, 64), (D, 128), (BR, 256), (B, 384), (D, 448))
LOUD_BUILD = (0.8,) * 16


class Gate:
    def __init__(self, blocked: str | None = None, crowded: bool = False):
        self.blocked, self.crowded = blocked, crowded

    def intense_blocked(self, at, moment):
        return self.blocked

    def accent_crowded(self, at, moment):
        return self.crowded


def table(meta):
    return {
        intent: tuple(loop for loop in meta if loop[0] == intent.value) for intent in LightIntent
    }


def director(seed=0, overlays=OVERLAYS, meta=META):
    return CueDirector(AutoloopPolicy(table(meta), meta, random.Random(seed)), overlays, seed)


def request(intent, start, *, new_track=False, levels=LOUD_BUILD, arrival=None):
    context = SelectionContext(intent, new_track=new_track, arrival_of_peak=arrival)
    return SectionRequest(start, intent, context, start * BEAT_SEC, BPM, levels)


def play(sections=SET, *, level='balanced', gate=None, cues=None, seed=0):
    cues = cues or director(seed)
    settings = ShowSettings(intensity=LEVELS[level])
    plans = []
    for number, (intent, start) in enumerate(sections):
        plans.append(
            cues.plan(
                start,
                request(intent, start, new_track=number == 0),
                settings,
                'ice',
                gate or Gate(),
            )
        )
    return plans


def rows(plan):
    return [f'{row.bank}/{row.autoloop}' for row in plan.cue.overlays]


def row(plan, bank):
    [found] = [overlay for overlay in plan.cue.overlays if overlay.bank == bank]
    return found


def test_each_section_is_one_cue_on_its_start_beat_with_its_intent_seed_and_strength():
    cues = director()
    settings = ShowSettings(intensity=INTENSITY_BALANCED)
    first = cues.plan(0, request(A, 0, new_track=True), settings, 'ice', Gate())
    drop = cues.plan(1, request(D, 64, arrival=0.82), settings, 'ice', Gate())
    assert (first.cue.start_beat_index, first.cue.intent) == (0, A)
    assert (drop.cue.start_beat_index, drop.cue.intent, drop.cue.drop_strength) == (64, D, 0.82)
    assert first.cue.drop_strength is None
    assert (first.cue.seed, drop.cue.seed) == (cue_seed(0, 1, 1), cue_seed(0, 1, 2))
    assert first.cue.seed != drop.cue.seed
    command = drop.cue.command()['value']
    assert set(command) == {
        'start_beat_index',
        'intent',
        'rotation',
        'overlays',
        'palette',
        'seed',
        'drop_strength',
    }, 'names, beats and bars only, never render parameters'


def test_a_set_plays_the_same_cues_for_the_same_seed():
    first = [plan.cue for plan in play(seed=7)]
    again = [plan.cue for plan in play(seed=7)]
    assert first == again
    assert [plan.cue for plan in play(seed=8)] != first


def test_a_new_track_restarts_the_section_count_of_the_seed():
    cues = director()
    play(cues=cues)
    [opening] = play(((A, 600),), cues=cues)
    assert (opening.track, opening.section) == (2, 1)
    assert opening.cue.seed == cue_seed(0, 2, 1)


def test_rotation_per_intent_with_phrase_transitions():
    atmos, build, drop, breakdown, rebuild, _ = play()
    assert [(look.bars, look.transition) for look in drop.cue.rotation] == [
        (16, {'kind': 'cut'}),
        (16, {'kind': 'crossfade', 'duration_beats': 4.0}),
    ]
    assert [look.bars for look in atmos.cue.rotation] == [32, 32]
    assert atmos.cue.rotation[0].transition is None
    assert breakdown.cue.rotation[0].transition == {'kind': 'crossfade', 'duration_beats': 8.0}
    assert len(build.cue.rotation) == 1, 'a buildup keeps its tension: one look'
    assert build.cue.rotation[0].transition == {'kind': 'crossfade', 'duration_beats': 1.0}
    assert rebuild.cue.rotation[0].transition == {'kind': 'crossfade', 'duration_beats': 1.0}
    [after_drop] = play(((D, 0), (B, 64)))[1:]
    assert after_drop.cue.rotation[0].transition == {'kind': 'cut'}


def test_calm_stretches_and_wild_snaps_the_rotation_steps():
    for level, beats in (('calm', 8.0), ('wild', 2.0)):
        drop = play(level=level)[2]
        assert drop.cue.rotation[1].transition == {'kind': 'crossfade', 'duration_beats': beats}


def test_consecutive_looks_contrast_in_movement_or_texture():
    for seed in range(12):
        for plan in play(seed=seed):
            metas = [choice.meta for choice in plan.choices]
            # The rotation cycles, so the last look leads back into the first.
            for first, second in itertools.pairwise([*metas, metas[0]]):
                assert (first.movement, first.texture) != (second.movement, second.texture)


def test_contrast_falls_back_to_the_neighbouring_energy_tier():
    meta = {
        ('drop', 'a'): AutoloopMeta(2, 'snap', 'wash'),
        ('drop', 'b'): AutoloopMeta(2, 'snap', 'wash'),
        ('drop', 'c'): AutoloopMeta(3, 'sweep', 'beam'),
    }
    policy = AutoloopPolicy({D: tuple(meta)}, meta, random.Random(0))
    short_build = SelectionContext(D, seconds_since_drop=10.0)
    choice = policy.choose(short_build, exclude=('drop', 'a'), contrast=meta['drop', 'a'])
    assert (choice.bank, choice.autoloop) == ('drop', 'c')
    assert 'contrasts snap/wash from energy 3' in choice.reason


def test_mono_until_the_first_drop_and_in_every_breakdown_and_buildup():
    palettes = [plan.cue.palette for plan in play()]
    assert palettes == [
        'ice_mono',
        'ice_mono',
        'ice_drop',
        'ice_breakdown_mono',
        'ice_mono',
        'ice_drop',
    ]
    [_, atmos_after_drop] = play(((D, 0), (A, 64)))
    assert atmos_after_drop.cue.palette == 'ice', 'the base palette after a drop'


@pytest.mark.parametrize(
    ('level', 'gate', 'expected'),
    [
        (
            'calm',
            Gate(),
            [
                'dip/last_beat',
                'hit/colour_bump',
                'fill/wipe',
                'accent/mover_shapes',
                'accent/sparkle',
            ],
        ),
        (
            'balanced',
            Gate(),
            [
                'dip/last_beat',
                'riser/strip_fill',
                'hit/blinder',
                'fill/wipe',
                'fill/turnaround',
                'accent/mover_shapes',
                'accent/sparkle',
            ],
        ),
        (
            'wild',
            Gate(),
            [
                'dip/last_beat',
                'riser/strobe_ramp',
                'hit/strobe',
                'fill/wipe',
                'fill/turnaround',
                'accent/mover_shapes',
                'accent/sparkle',
            ],
        ),
        (
            'wild',
            Gate(blocked='cooldown'),
            [
                'dip/last_beat',
                'riser/strip_fill',
                'hit/blinder',
                'fill/wipe',
                'fill/turnaround',
                'accent/mover_shapes',
                'accent/sparkle',
            ],
        ),
        (
            'balanced',
            Gate(crowded=True),
            [
                'dip/last_beat',
                'riser/strip_fill',
                'hit/colour_bump',
                'fill/wipe',
                'fill/turnaround',
                'accent/mover_shapes',
                'accent/sparkle',
            ],
        ),
    ],
)
def test_the_drop_entry_per_venue_setting(level, gate, expected):
    drop = play(SET[:3], level=level, gate=gate)[2]
    assert sorted(rows(drop)) == sorted(expected)
    strobing = any(OVERLAYS[row.bank, row.autoloop].strobe for row in drop.cue.overlays)
    assert strobing == (level == 'wild' and gate.blocked is None and not gate.crowded)
    assert drop.events.intense == strobing
    assert drop.events.accent == (level != 'calm' and not gate.crowded)


def test_the_entry_rows_land_on_the_one():
    drop = play(SET[:3])[2]
    dip, riser, hit = row(drop, 'dip'), row(drop, 'riser'), row(drop, 'hit')
    assert (dip.begin_beats + dip.length_beats, dip.attack_beats) == (0.0, 0.0)
    assert riser.begin_beats + riser.length_beats == dip.begin_beats, 'the riser ends at the dip'
    assert (riser.length_beats, riser.every_bars) == (16, 0)
    assert (hit.begin_beats, hit.attack_beats, hit.every_bars) == (0.0, 0.0, 0)
    priorities = {overlay.bank: overlay.priority for overlay in drop.cue.overlays}
    assert priorities['fill'] < priorities['accent'] < priorities['riser']
    assert priorities['riser'] < priorities['hit'] < priorities['dip']


def test_the_wild_strobe_hit_is_the_burst_setting_in_beats_and_the_riser_fits_the_cap():
    drop = play(SET[:3], level='wild')[2]
    # 1 s at 120 bpm is two beats; a strobe ends on a snap.
    assert (row(drop, 'hit').length_beats, row(drop, 'hit').release_beats) == (2.0, 0.0)
    tight = ShowSettings(intensity=INTENSITY_WILD, strobe_max_burst_seconds=3.0)
    cues = director()
    for number, (intent, start) in enumerate(SET[:3]):
        plan = cues.plan(number, request(intent, start, new_track=not number), tight, 'ice', Gate())
    assert row(plan, 'riser').autoloop == 'strip_fill', '8 beats at 120 bpm is 4 s, over 3 s'


def test_fills_on_bar_8_and_a_bigger_one_on_bar_16_and_accents_8_bars_off_the_base():
    drop = play(SET[:3])[2]
    fills = {
        (o.autoloop, o.begin_beats, o.every_bars) for o in drop.cue.overlays if o.bank == 'fill'
    }
    assert fills == {('wipe', 28.0, 16), ('turnaround', 60.0, 16)}
    accents = sorted(
        (o.begin_beats, o.length_beats, o.every_bars)
        for o in drop.cue.overlays
        if o.bank == 'accent'
    )
    assert accents == [(32.0, 64.0, 32), (96.0, 64.0, 32)], 'bars 8 and 24 of each 32'
    assert all(o.attack_beats >= 0.5 for o in drop.cue.overlays if o.bank in ('fill', 'accent'))
    calm = play(SET[:3], level='calm')[2]
    assert [(o.begin_beats, o.every_bars) for o in calm.cue.overlays if o.bank == 'fill'] == [
        (60.0, 16)
    ], 'Calm fills at half the cadence'


def test_breakdown_and_atmospheric_rows():
    atmos, build, _, breakdown, _, _ = play()
    assert rows(breakdown) == ['dip/fade', 'fill/wipe']
    assert (row(breakdown, 'dip').begin_beats, row(breakdown, 'fill').begin_beats) == (-2.0, 60.0)
    assert rows(atmos) == ['fill/wipe'] and rows(build) == []
    calm_atmos, _, _, calm_breakdown, _, _ = play(level='calm')
    assert rows(calm_atmos) == []
    assert row(calm_breakdown, 'fill').every_bars == 32


def test_a_riser_only_after_a_buildup():
    _, drop = play(((A, 0), (D, 64)))
    assert 'riser' not in {o.bank for o in drop.cue.overlays}
    _, short = play(((B, 0), (D, 2)))
    assert 'riser' not in {o.bank for o in short.cue.overlays}, 'no room for a pre-roll'


def test_an_overlay_never_fights_its_base_loop():
    meta = {
        **{loop: value for loop, value in META.items() if loop[0] not in ('buildup', 'drop')},
        ('buildup', 'countdown'): AutoloopMeta(2, 'sweep', 'pixel'),
        ('drop', 'ballyhoo'): AutoloopMeta(3, 'sweep', 'beam'),
        ('drop', 'wide_fan'): AutoloopMeta(2, 'sweep', 'beam'),
        ('drop', 'stomp'): AutoloopMeta(2, 'snap', 'wash'),
    }
    for seed in range(8):
        _, drop = play(SET[1:3], cues=director(seed, meta=meta))
        assert drop.previous is B and 'riser/strip_fill' not in rows(drop), 'never over countdown'
        bases = {(choice.bank, choice.autoloop) for choice in drop.choices}
        assert bases & {('drop', 'ballyhoo'), ('drop', 'wide_fan')}
        assert 'accent/mover_shapes' not in rows(drop)
        assert 'accent/sparkle' in rows(drop)


@pytest.mark.parametrize(
    ('quiet_beats', 'dip'),
    [(0, 0.5), (1, 1.0), (3, 1.0), (4, 4.0), (6, 4.0), (8, 8.0), (12, 8.0)],
)
def test_the_dip_follows_the_energy_gap_before_the_drop(quiet_beats, dip):
    levels = (0.8,) * (16 - quiet_beats) + (0.1,) * quiet_beats
    assert dip_beats(levels) == dip


def test_dip_shapes_land_in_the_drop_cue():
    for levels, dip in (
        (LOUD_BUILD, 0.5),  # the build runs into the one
        ((0.8,) * 15 + (0.2,), 1.0),  # a one-beat breath
        ((0.7,) * 12 + (0.05,) * 4, 4.0),  # a bar of silence
        ((0.9,) * 8 + (0.1,) * 8, 8.0),  # two bars stripped back
    ):
        cues = director()
        settings = ShowSettings(intensity=INTENSITY_BALANCED)
        cues.plan(0, request(B, 0, new_track=True), settings, 'ice', Gate())
        drop = cues.plan(1, request(D, 64, levels=levels), settings, 'ice', Gate())
        assert (row(drop, 'dip').begin_beats, row(drop, 'dip').length_beats) == (-dip, dip)
        assert row(drop, 'riser').begin_beats == -(dip + 16)
    assert dip_beats(()) == 0.5, 'nothing measured: the shortest dip'


def test_a_second_drop_opens_on_the_first_drops_look_and_hit_and_adds_one_layer():
    plans = play()
    first, second = plans[2], plans[5]
    assert (second.choices[0].bank, second.choices[0].autoloop) == (
        first.choices[0].bank,
        first.choices[0].autoloop,
    )
    assert row(second, 'hit').autoloop == row(first, 'hit').autoloop
    once = [[o for o in plan.cue.overlays if o.every_bars == 0] for plan in (first, second)]
    assert [o.bank for o in once[0]] == ['dip', 'riser', 'hit']
    assert once[1][: len(once[0])] == once[0] and len(once[1]) == len(once[0]) + 1
    [added] = once[1][len(once[0]) :]
    assert (added.bank, added.begin_beats, added.length_beats) == ('accent', 0.0, 32.0)
    assert second.drop_number == 2


def test_a_level_change_refilters_rows_and_keeps_start_seed_and_rotation():
    cues = director()
    balanced = play(SET[:3], cues=cues)[2]
    assert cues.revise(999, ShowSettings(), Gate(), started=False) is None
    wild = cues.revise(128, ShowSettings(intensity=INTENSITY_WILD), Gate(), started=False)
    assert (wild.cue.start_beat_index, wild.cue.seed, wild.cue.rotation) == (
        balanced.cue.start_beat_index,
        balanced.cue.seed,
        balanced.cue.rotation,
    )
    assert row(wild, 'hit').autoloop == 'strobe' and wild.events.intense
    calm = cues.revise(128, ShowSettings(intensity=INTENSITY_CALM), Gate(), started=True)
    assert row(calm, 'hit').autoloop == 'strobe', 'a started section keeps its one-shot rows'
    assert calm.events == wild.events
    assert [o.every_bars for o in calm.cue.overlays if o.bank == 'fill'] == [16]


def test_a_dropped_or_reset_plan_is_forgotten():
    cues = director()
    play(SET[:3], cues=cues)
    assert cues.drop(128).cue.intent is D
    assert cues.plan_for(128) is None
    cues.reset()
    assert cues.plan_for(0) is None
    [opening] = play(((A, 200),), cues=cues)
    assert opening.track == 2, 'the next plan after a reset starts a track'


# ---- the venue gate through the effect controller, on a simulated clock ----


def controller(level='wild', clock=None, venue=None):
    clock = clock or VirtualClock()
    settings = ShowSettings(intensity=LEVELS[level])
    effects = EffectController(
        RecordingStarfireClient(clock=clock),
        clock=clock,
        settings=lambda: settings,
        autoloops=table(META),
        autoloop_meta=META,
        overlay_meta=OVERLAYS,
        venue=venue,
    )
    return effects, clock


def plan_drop(effects, moment, at, start):
    build = SectionRequest(start - 64, B, SelectionContext(B), at - 32.0, BPM, LOUD_BUILD)
    effects.plan_cue((moment, 'build'), build)
    drop = SectionRequest(start, D, SelectionContext(D), at, BPM, LOUD_BUILD)
    return effects.plan_cue(moment, drop)


def hit_of(cue):
    [hit] = [o.autoloop for o in cue.overlays if o.bank == 'hit']
    return hit


def test_the_intense_cooldown_blocks_strobe_rows_between_drops():
    effects, _ = controller('wild', venue=Venue(max_accent_events_per_minute=10))
    # Wild halves the 60 s cooldown.
    assert hit_of(plan_drop(effects, 'one', 100.0, 128)) == 'strobe'
    second = plan_drop(effects, 'two', 125.0, 384)
    assert hit_of(second) != 'strobe' and 'strobe_ramp' not in {o.autoloop for o in second.overlays}
    assert hit_of(plan_drop(effects, 'three', 131.0, 640)) == 'strobe', 'two booked nothing'
    effects.drop_cue('one')
    effects.drop_cue('three')
    assert hit_of(effects.revise_cue('two', started=False)) == 'strobe', 'the slot is free again'


def test_the_drops_special_shares_its_cues_intense_moment():
    effects, clock = controller('wild')
    plan_drop(effects, 'drop', 10.0, 128)
    clock.advance(10.0)
    assert effects.intense_blocked(10.0, 'drop') is None
    assert effects.intense_blocked(10.0, 'another') is not None


def test_the_venue_cap_turns_a_third_white_hit_in_a_minute_into_the_colour_bump():
    effects, _ = controller('balanced')
    hits = [hit_of(plan_drop(effects, n, 100.0 + 20 * n, 128 + 256 * n)) for n in range(3)]
    assert hits == ['blinder', 'blinder', 'colour_bump']
    assert hit_of(plan_drop(effects, 3, 161.0, 1024)) == 'blinder', 'the first left the window'


def test_resetting_the_plans_releases_bookings_still_ahead_of_the_room():
    effects, clock = controller('wild')
    plan_drop(effects, 'past', 10.0, 128)
    plan_drop(effects, 'ahead', 50.0, 256)
    clock.advance(20.0)
    effects.reset_cues()
    assert effects.intense_blocked(60.0, None) is None
    assert effects.intense_blocked(30.0, None) is not None, 'the drop the room saw still counts'
