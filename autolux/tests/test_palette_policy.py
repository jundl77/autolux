import colorsys
import itertools
import json
import random

import pytest

from autolux.lib.engine.effect_definitions import LightIntent, ShowSettings
from autolux.lib.engine.palette_policy import FINALISTS, PalettePolicy, palette_variant
from autolux.lib.engine.track_mood import (
    BASS_OVER_TREBLE_RANGE_DB,
    CENTROID_RANGE_HZ,
    DRIVE_LEVEL_RANGE,
    LEVEL_RANGE,
    TEMPO_RANGE_BPM,
    MeasuredBeat,
    TrackMoodMeter,
    track_mood,
)
from autolux.lighting.palettes import (
    BREAKDOWN_PALETTES,
    DEFAULT_PALETTE,
    DROP_PALETTES,
    PALETTES,
    Mood,
    Palette,
    colour_temperature,
    luminance,
    palette_table,
)

SAME_FAMILY_HUE_DEGREES = 45.0
CHROMATIC_SATURATION = 0.3
HARD_COLD_DARK = Mood(temperature=-1.0, weight=-1.0, drive=1.0)
WARM_BRIGHT_LAID_BACK = Mood(temperature=1.0, weight=1.0, drive=-1.0)
HARD_WARM_BRIGHT = Mood(temperature=1.0, weight=1.0, drive=1.0)
COLD_DARK_HARD_PALETTES = {'uv', 'mono_cold', 'ice'}
SUNSET_LIKE = {'sunset', 'house', 'mono_warm', 'ember'}
TECHNO_LIKE = {'techno', 'acid', 'house'}
CLEAR_LUMINANCE_GAP = 0.1


def by_name(name):
    return next(palette for palette in palette_table() if palette.name == name)


def picks(seed, moods):
    policy = PalettePolicy(PALETTES, random.Random(seed))
    return [policy.choose_for_track(mood) for mood in moods]


def test_every_palette_declares_a_mood_on_all_three_axes():
    for palette in PALETTES:
        assert isinstance(palette.mood, Mood)
        assert all(-1.0 <= axis <= 1.0 for axis in palette.mood.axes())
    assert len({palette.mood for palette in PALETTES}) == len(PALETTES)


def test_temperature_comes_from_the_colours_so_amber_techno_is_warm():
    for palette in PALETTES:
        assert palette.mood.temperature == colour_temperature(palette.slots())
    temperature = {palette.name: palette.mood.temperature for palette in PALETTES}
    assert temperature['techno'] > 0.5 and temperature['ember'] > 0.8
    assert temperature['ice'] < -0.6 and temperature['uv'] < -0.4
    for grey, saturated in [('mono_cold', 'ice'), ('mono_warm', 'ember')]:
        assert abs(temperature[grey]) < abs(temperature[saturated]) - 0.2
    white = {slot: (1.0, 1.0, 1.0) for slot in ('primary', 'secondary', 'accent')}
    assert colour_temperature(white) == 0.0


def test_hand_set_weight_follows_the_colours_luminance():
    for brighter, dimmer in itertools.permutations(PALETTES, 2):
        if luminance(brighter.slots()) - luminance(dimmer.slots()) > CLEAR_LUMINANCE_GAP:
            assert brighter.mood.weight > dimmer.mood.weight, (brighter.name, dimmer.name)


@pytest.mark.parametrize('axes', [(1.1, 0.0, 0.0), (0.0, -1.5, 0.0), (0.0, 0.0, 2.0)])
def test_a_mood_outside_the_axes_is_rejected(axes):
    with pytest.raises(ValueError, match='within -1..1'):
        Mood(*axes)


@pytest.mark.parametrize('seed', range(20))
def test_a_track_picks_palettes_of_its_own_mood(seed):
    assert picks(seed, [HARD_COLD_DARK])[0] in COLD_DARK_HARD_PALETTES
    assert picks(seed, [HARD_WARM_BRIGHT])[0] in TECHNO_LIKE
    assert picks(seed, [WARM_BRIGHT_LAID_BACK])[0] in SUNSET_LIKE


def test_the_draw_is_among_the_three_closest_palettes():
    policy = PalettePolicy(PALETTES, random.Random(0))
    ranked = sorted(PALETTES, key=lambda palette: policy.score(palette, HARD_COLD_DARK))
    farthest = {palette.name for palette in ranked[:-FINALISTS]}
    assert not farthest & {picks(seed, [HARD_COLD_DARK])[0] for seed in range(60)}


def test_recency_rotates_palettes_for_tracks_of_the_same_mood():
    chosen = picks(5, [HARD_COLD_DARK] * 12)
    assert all(previous != current for previous, current in itertools.pairwise(chosen))
    assert len(set(chosen)) >= FINALISTS


def test_a_just_used_palette_scores_a_quarter_of_an_unused_twin():
    policy = PalettePolicy(PALETTES, random.Random(0))
    chosen = by_name(policy.choose_for_track(HARD_COLD_DARK))
    assert policy.score(chosen, chosen.mood) == pytest.approx(0.25)
    unused = next(palette for palette in PALETTES if palette.name != chosen.name)
    assert policy.score(unused, unused.mood) == pytest.approx(1.0)


def test_the_same_seed_and_tracks_pick_the_same_palettes():
    moods = [HARD_COLD_DARK, WARM_BRIGHT_LAID_BACK, Mood(0.0, 0.0, 0.0)] * 10
    assert picks(7, moods) == picks(7, moods)
    assert any(picks(seed, moods) != picks(7, moods) for seed in range(8, 12))


def test_before_any_measurement_the_policy_holds_the_default_palette():
    policy = PalettePolicy(PALETTES, random.Random(0))
    assert policy.track_palette == DEFAULT_PALETTE
    assert policy.variant_for(LightIntent.BUILDUP) == DEFAULT_PALETTE


def test_every_section_of_a_track_shares_its_palette_with_drop_and_breakdown_variants():
    policy = PalettePolicy(PALETTES, random.Random(3))
    base = policy.choose_for_track(HARD_COLD_DARK)
    assert [
        policy.variant_for(intent)
        for intent in (
            LightIntent.ATMOSPHERIC,
            LightIntent.BREAKDOWN,
            LightIntent.BUILDUP,
            LightIntent.DROP,
        )
    ] == [base, f'{base}_breakdown', base, f'{base}_drop']
    assert palette_variant('ice', LightIntent.DROP) == 'ice_drop'


def beat(bpm, level, centroid_hz, bass_over_treble_db):
    return MeasuredBeat(bpm, level, centroid_hz, bass_over_treble_db)


def midpoint(reference):
    return sum(reference) / 2


def middle_beat(level=None):
    return beat(
        midpoint(TEMPO_RANGE_BPM),
        midpoint(LEVEL_RANGE) if level is None else level,
        midpoint(CENTROID_RANGE_HZ),
        midpoint(BASS_OVER_TREBLE_RANGE_DB),
    )


def test_the_reference_ranges_map_their_ends_onto_the_mood_axes():
    hard = beat(TEMPO_RANGE_BPM[1], 1.0, CENTROID_RANGE_HZ[0], BASS_OVER_TREBLE_RANGE_DB[0])
    assert track_mood([hard]) == Mood(temperature=-1.0, weight=0.0, drive=1.0)
    soft = beat(TEMPO_RANGE_BPM[0] - 20, 0.0, CENTROID_RANGE_HZ[1] + 1000, 40.0)
    assert track_mood([soft]) == Mood(temperature=1.0, weight=0.0, drive=-1.0)
    middle = track_mood([middle_beat()])
    assert (middle.temperature, middle.weight) == pytest.approx((0.0, 0.0))


def test_drive_is_mostly_tempo_and_counts_the_loud_beats_of_the_opening():
    at_tempo_midpoint = [middle_beat(level=midpoint(DRIVE_LEVEL_RANGE))]
    assert track_mood(at_tempo_midpoint).drive == pytest.approx(0.0)
    fast_but_quiet = beat(TEMPO_RANGE_BPM[1], 0.0, 2000.0, 0.0)
    assert track_mood([fast_but_quiet]).drive == pytest.approx(0.2)
    quiet_bars = [middle_beat(level=0.0)] * 9
    loud_beat = [middle_beat(level=DRIVE_LEVEL_RANGE[1])]
    assert track_mood(quiet_bars + loud_beat).drive == pytest.approx(-0.4)
    assert track_mood(quiet_bars[1:] + loud_beat * 2).drive == pytest.approx(0.4)


def test_the_meter_measures_only_after_a_start_and_reports_once_the_window_fills():
    meter = TrackMoodMeter(beats_to_measure=3)
    middle = middle_beat()
    assert meter.add(middle) is None and not meter.measuring
    meter.start()
    assert [meter.add(middle) for _ in range(2)] == [None, None]
    assert meter.add(middle) == track_mood([middle] * 3)
    assert not meter.measuring and meter.finish() is None


def test_an_interrupted_meter_reports_what_it_has():
    meter = TrackMoodMeter(beats_to_measure=32)
    meter.start()
    assert meter.finish() is None
    meter.start()
    meter.add(beat(TEMPO_RANGE_BPM[1], 1.0, CENTROID_RANGE_HZ[0], BASS_OVER_TREBLE_RANGE_DB[0]))
    assert meter.finish() == Mood(temperature=-1.0, weight=0.0, drive=1.0)


def hsv(colour):
    hue, saturation, value = colorsys.rgb_to_hsv(*colour)
    return hue * 360.0, saturation, value


def hue_distance(first, second):
    return min(abs(first - second), 360.0 - abs(first - second))


@pytest.mark.parametrize('base', PALETTES, ids=lambda palette: palette.name)
def test_each_breakdown_variant_keeps_its_palette_identity(base):
    breakdown = by_name(f'{base.name}_breakdown')
    assert (breakdown.weight, breakdown.drive, breakdown.secondary, breakdown.cold) == (
        base.weight,
        base.drive,
        base.secondary,
        base.cold,
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


@pytest.mark.parametrize('base', PALETTES, ids=lambda palette: palette.name)
def test_each_drop_variant_pushes_the_accent_and_keeps_everything_else(base):
    drop = by_name(f'{base.name}_drop')
    assert (drop.weight, drop.drive, drop.primary, drop.secondary, drop.cold, drop.black) == (
        base.weight,
        base.drive,
        base.primary,
        base.secondary,
        base.cold,
        base.black,
    )
    base_hue, base_saturation, base_value = hsv(base.accent)
    hue, saturation, value = hsv(drop.accent)
    assert saturation >= base_saturation and value >= base_value
    assert saturation > base_saturation or value > base_value or base_saturation == 1.0
    if base_saturation >= CHROMATIC_SATURATION:
        assert hue_distance(hue, base_hue) <= SAME_FAMILY_HUE_DEGREES


def test_the_login_table_starts_with_the_default_and_groups_each_palette_with_its_variants():
    table = palette_table()
    assert table[0].name == DEFAULT_PALETTE == 'techno'
    assert [palette.name for palette in table[1::3]] == [p.name for p in BREAKDOWN_PALETTES]
    assert [palette.name for palette in table[2::3]] == [p.name for p in DROP_PALETTES]
    assert [palette.name for palette in table[2::3]] == [f'{p.name}_drop' for p in PALETTES]
    assert len({palette.name for palette in table}) == 3 * len(PALETTES) == 27


@pytest.mark.parametrize(
    'change',
    [
        {'name': 'Ice'},
        {'weight': 1.5},
        {'drive': -2.0},
        {'accent': (1.2, 0.0, 0.0)},
        {'cold': (1.0, 1.0)},
    ],
)
def test_invalid_palettes_are_rejected(change):
    fields = {
        'name': 'ice',
        'weight': 0.4,
        'drive': 0.0,
        'primary': (0.0, 0.0, 1.0),
        'secondary': (0.0, 1.0, 1.0),
        'accent': (1.0, 1.0, 1.0),
        'cold': (0.9, 0.9, 1.0),
    }
    with pytest.raises(ValueError):
        Palette(**(fields | change))


def test_the_retired_warmth_setting_is_gone_but_an_old_show_file_still_loads(tmp_path):
    from autolux.lib.main import load_show_settings, save_show_settings

    assert 'palette_temperature_bias' not in ShowSettings().to_dict()
    with pytest.raises(TypeError):
        ShowSettings(palette_temperature_bias=0.5)
    path = tmp_path / 'show.json'
    path.write_text(
        json.dumps({'settings': {'palette_temperature_bias': -0.5, 'intensity': 0.3}}),
        encoding='utf-8',
    )
    assert load_show_settings(path) == ShowSettings(intensity=0.3)
    save_show_settings(path, load_show_settings(path))
    assert (
        'palette_temperature_bias' not in json.loads(path.read_text(encoding='utf-8'))['settings']
    )
    with pytest.raises(ValueError, match='unknown settings: warmth'):
        ShowSettings.from_dict({'warmth': 0.5})


def palette_kinds(lighting):
    return [
        (event['command']['kind'], event['command'].get('palette', event['command'].get('name')))
        for event in lighting.events
        if event['command']['kind'] in ('select', 'set_palette')
    ]


async def test_a_measured_track_sends_its_palette_once_and_the_next_look_keeps_it():
    from autolux.lib.engine.autoloop_policy import SelectionContext
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    lighting = RecordingStarfireClient()
    controller = EffectController(lighting, seed=4)
    await controller.change_effect(
        LightIntent.ATMOSPHERIC, context=SelectionContext(LightIntent.ATMOSPHERIC, new_track=True)
    )
    assert palette_kinds(lighting) == [('select', DEFAULT_PALETTE)]
    chosen = controller.choose_track_palette(WARM_BRIGHT_LAID_BACK)
    assert palette_kinds(lighting)[1:] == [('set_palette', chosen)]
    await controller.change_effect(LightIntent.DROP, cut=True)
    assert palette_kinds(lighting)[-1] == ('select', f'{chosen}_drop')
    assert [kind for kind, _ in palette_kinds(lighting)].count('set_palette') == 1


async def test_a_pinned_palette_survives_a_measurement_and_auto_returns_to_the_choice():
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    lighting = RecordingStarfireClient()
    controller = EffectController(lighting, seed=4)
    await controller.change_effect(LightIntent.BREAKDOWN)
    controller.pin_palette('sunset')
    sent = len(lighting.events)
    chosen = controller.choose_track_palette(HARD_COLD_DARK)
    assert len(lighting.events) == sent and lighting.palette == 'sunset_breakdown'
    controller.pin_palette(None)
    assert lighting.palette == f'{chosen}_breakdown'


async def test_a_choice_made_for_the_next_look_is_not_sent_early():
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    lighting = RecordingStarfireClient()
    controller = EffectController(lighting, seed=4)
    await controller.change_effect(LightIntent.BUILDUP)
    sent = len(lighting.events)
    chosen = controller.choose_track_palette(WARM_BRIGHT_LAID_BACK, send_now=False)
    assert len(lighting.events) == sent
    await controller.change_effect(LightIntent.ATMOSPHERIC)
    assert palette_kinds(lighting)[-1] == ('select', chosen)


async def engine_showing_an_intro():
    from autolux.lib.clock import VirtualClock
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.lib.engine.light_engine import LightEngine
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock)
    light = LightEngine(lighting, EffectController(lighting, seed=4), None, clock=clock)
    light._room_playing = True
    light._held_intent = LightIntent.ATMOSPHERIC
    await light._switch_held_intent()
    return light, lighting


WARM_BRIGHT_LAID_BACK_BEAT = beat(TEMPO_RANGE_BPM[0], 1.0, CENTROID_RANGE_HZ[1], 40.0)


async def test_the_opening_is_measured_until_the_first_buildup_and_committed_with_it():
    light, lighting = await engine_showing_an_intro()
    for index in range(16):
        light._measure_track_mood(WARM_BRIGHT_LAID_BACK_BEAT, index % 4)
    assert palette_kinds(lighting) == [('select', DEFAULT_PALETTE)]
    light._held_intent = LightIntent.BUILDUP
    await light._switch_held_intent()
    [_, (kind, palette)] = palette_kinds(lighting)
    assert kind == 'select' and palette in SUNSET_LIKE
    assert not light._track_mood.measuring


async def test_a_full_window_commits_its_palette_on_the_next_downbeat():
    from autolux.lib.engine.track_mood import TrackMoodMeter

    light, lighting = await engine_showing_an_intro()
    light._track_mood = TrackMoodMeter(beats_to_measure=6)
    light._track_mood.start()
    phases = [0, 1, 2, 3, 0, 1, 2, 3]
    sent_at_phase = []
    for phase in phases:
        before = len(palette_kinds(lighting))
        light._measure_track_mood(WARM_BRIGHT_LAID_BACK_BEAT, phase)
        sent_at_phase += [phase] * (len(palette_kinds(lighting)) - before)
    assert sent_at_phase == []
    light._measure_track_mood(WARM_BRIGHT_LAID_BACK_BEAT, 0)
    [(kind, palette)] = palette_kinds(lighting)[1:]
    assert kind == 'set_palette' and palette in SUNSET_LIKE


async def test_a_partial_window_is_discarded_on_a_new_track_and_the_palette_holds():
    light, lighting = await engine_showing_an_intro()
    for index in range(8):
        light._measure_track_mood(WARM_BRIGHT_LAID_BACK_BEAT, index % 4)
    light._after_silence = True
    light._held_intent = LightIntent.ATMOSPHERIC
    await light._switch_held_intent()
    assert {palette for _, palette in palette_kinds(lighting)} == {DEFAULT_PALETTE}
    assert light.effect_controller._palette_policy.track_palette == DEFAULT_PALETTE
    assert light._track_mood.measuring


async def test_a_silence_restart_discards_the_window():
    light, lighting = await engine_showing_an_intro()
    for index in range(8):
        light._measure_track_mood(WARM_BRIGHT_LAID_BACK_BEAT, index % 4)
    light._forget_room_look()
    assert not light._track_mood.measuring
    light._measure_track_mood(WARM_BRIGHT_LAID_BACK_BEAT, 0)
    assert light.effect_controller._palette_policy.track_palette == DEFAULT_PALETTE
