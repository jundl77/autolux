import dataclasses
import logging
from types import SimpleNamespace

import pytest

from autolux.lib.engine.effect_definitions import (
    BALANCED,
    CALM,
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    WILD,
    LightIntent,
    ShowSettings,
)
from autolux.lib.engine.intensity_follow import CALM_OPENING_BEATS, FollowMusic
from autolux.lib.engine.section_decoder import BEATS_PER_BAR
from autolux.lib.engine.track_mood import (
    MAX_MEASURED_BARS,
    TEMPO_RANGE_BPM,
    MeasuredBeat,
    level_drive,
    loud_beats,
)


@pytest.mark.parametrize(
    ('drive', 'loud_drop', 'level'),
    [
        (0.41, True, WILD),
        (0.41, False, BALANCED),
        (0.39, True, BALANCED),
        (-0.29, False, BALANCED),
        (-0.31, False, CALM),
    ],
)
def test_a_drop_reads_the_level_from_the_drive_and_its_loudness(drive, loud_drop, level):
    assert FollowMusic().level_at_drop(drive, loud_drop) == level


@pytest.mark.parametrize(
    ('current', 'level'), [(CALM, CALM), (BALANCED, BALANCED), (WILD, BALANCED)]
)
def test_the_mood_commit_only_steps_down_or_holds(current, level):
    follow = FollowMusic()
    follow.start_track(LightIntent.ATMOSPHERIC)
    assert follow.on_track_mood(current) == level


def test_a_soft_drop_caps_a_wild_track_once_and_never_flaps_back():
    follow = FollowMusic()
    assert follow.on_drop(0.5, BALANCED) is None, 'no level before the track is measured'
    assert follow.on_track_mood(BALANCED) == BALANCED, 'the first reading is never wild'
    assert follow.on_drop(0.95, BALANCED, drive=0.6) == WILD
    assert follow.on_drop(0.95, WILD) is None, 'a maximal drop keeps wild'
    assert follow.on_drop(0.6, WILD) == BALANCED
    assert follow.on_drop(0.95, BALANCED, drive=0.6) is None, 'one change per track'
    follow.start_track(LightIntent.ATMOSPHERIC)
    assert follow.on_track_mood(WILD) == BALANCED, 'the next track starts afresh'
    assert not follow.loud_drop and not follow.capped


class _Engine:
    def __init__(self, track_level=None):
        self.follow_music = SimpleNamespace(track_level=track_level)
        self.on_auto_intensity = None
        self.levels_sent = []
        self.paused, self.master_level, self.mode = False, 1.0, 'auto'
        self.static_looks = SimpleNamespace(state=dict)

    def intensity_changed(self):
        self.levels_sent.append(self.control.settings.intensity)


async def test_auto_levels_take_the_manual_path_and_the_manual_level_comes_back(tmp_path, caplog):
    from autolux.lib.main import ShowControl

    control = ShowControl(tmp_path / 'show.json')
    light = _Engine(track_level=WILD)
    light.control = control
    control.follow_music(light)

    async def send(command):
        control.commands.put_nowait(command)
        await control.apply_pending(light)

    await send({'action': 'intensity', 'level': INTENSITY_BALANCED})
    await send({'action': 'intensity_auto', 'enabled': True})
    assert (control.settings.intensity, control.settings.intensity_auto) == (INTENSITY_WILD, True)

    with caplog.at_level(logging.INFO):
        light.on_auto_intensity(INTENSITY_CALM, 'drive -0.52')
    assert 'intensity auto → calm (drive -0.52)' in caplog.text
    assert light.levels_sent == [INTENSITY_WILD, INTENSITY_CALM]

    await send({'action': 'intensity_auto', 'enabled': False})
    assert (control.settings.intensity, control.settings.intensity_auto) == (
        INTENSITY_BALANCED,
        False,
    )
    light.on_auto_intensity(INTENSITY_WILD, 'drive +0.60')
    assert control.settings.intensity == INTENSITY_BALANCED, 'auto changes stop once it is off'

    await send({'action': 'intensity_auto', 'enabled': True})
    await send({'action': 'intensity', 'level': INTENSITY_CALM})
    assert (control.settings.intensity, control.settings.intensity_auto) == (INTENSITY_CALM, False)


async def test_the_engine_follows_the_mood_and_a_soft_drop_only_while_auto_is_on():
    from autolux.tests.test_light_engine import engine

    light, *_ = engine(queue=False)
    settings = ShowSettings(intensity_auto=True)
    light._settings = lambda: settings
    asked = []
    light.on_auto_intensity = lambda level, why: asked.append((level, why))

    light._track_beats = [DRIVING_BEAT] * 8
    light._follow_drop(MAXIMAL_ARRIVAL)
    assert asked == [], 'no level before the mood'
    light._follow_track_mood()
    assert asked == [], 'the first reading is never wild, and balanced is already up'
    light._follow_drop(MAXIMAL_ARRIVAL)
    assert asked == [(INTENSITY_WILD, 'drive +0.84')]
    settings = ShowSettings(intensity=INTENSITY_WILD, intensity_auto=True)
    light._follow_drop(0.62)
    assert asked[-1] == (INTENSITY_BALANCED, 'drop arrival 0.62')

    settings = ShowSettings(intensity=INTENSITY_WILD, intensity_auto=False)
    light._follow_track_mood()
    assert len(asked) == 2, 'with auto off the mood is only remembered'
    assert light.follow_music.track_level == BALANCED


async def test_an_auto_level_reaches_the_specials_gate_through_the_shared_settings(tmp_path):
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.lib.main import ShowControl
    from autolux.tests.test_light_engine import RecordingStarfireClient

    control = ShowControl(tmp_path / 'show.json')
    light = _Engine()
    light.control = control
    control.follow_music(light)
    controller = EffectController(RecordingStarfireClient(), settings=lambda: control.settings)
    control.commands.put_nowait({'action': 'intensity_auto', 'enabled': True})
    await control.apply_pending(light)
    assert controller.intense_blocked(control.settings, 0.0) is None

    light.on_auto_intensity(INTENSITY_CALM, 'drive -0.52')
    assert 'at or below' in controller.intense_blocked(control.settings, 0.0)


async def test_a_mood_that_commits_at_the_section_after_an_early_drop_still_sets_the_level():
    from autolux.lib.engine.track_mood import MIN_MOOD_BEATS
    from autolux.tests.test_palette_policy import engine_showing_an_intro, enter, measure_beats

    light, _ = await engine_showing_an_intro()
    # Starting wild, so the laid-back track's balanced verdict is a visible change.
    settings = ShowSettings(intensity=INTENSITY_WILD, intensity_auto=True)
    light._settings = lambda: settings
    asked = []
    light.on_auto_intensity = lambda level, why: asked.append((level, why))

    measure_beats(light, MIN_MOOD_BEATS - 10)
    await enter(light, LightIntent.DROP)
    assert asked == [] and light.follow_music.track_level is None, 'the early drop defers the mood'

    measure_beats(light, 10)
    await enter(light, LightIntent.ATMOSPHERIC)
    assert light.follow_music.track_level == BALANCED
    [(level, why)] = asked
    assert level == INTENSITY_BALANCED and why.startswith('drive ')


BREAKDOWN_BEAT = MeasuredBeat(90.0, 0.6, 1000.0, 0.0)
DRIVING_BEAT = MeasuredBeat(140.0, 1.0, 3000.0, 0.0)
MAXIMAL_ARRIVAL = 0.95
WINDOW_BEATS = MAX_MEASURED_BARS * BEATS_PER_BAR


async def engine_joined_in_a_breakdown(intensity_auto=True):
    """Auto levels apply at once, as ShowControl does; the opening window reads calm."""
    from autolux.tests.test_palette_policy import engine_showing_an_intro

    light, _ = await engine_showing_an_intro()
    light.settings = ShowSettings(intensity=INTENSITY_BALANCED, intensity_auto=intensity_auto)
    light._settings = lambda: light.settings
    light.asked = []

    def apply(level, why):
        light.asked.append((level, why))
        light.settings = dataclasses.replace(light.settings, intensity=level)

    light.on_auto_intensity = apply
    # The full window commits on the downbeat after it.
    feed(light, BREAKDOWN_BEAT, WINDOW_BEATS + 1)
    return light


def feed(light, beat, count):
    for index in range(count):
        light._measure_track_mood(beat, index % BEATS_PER_BAR)


async def commit(light, intent, arrival=None):
    """A section entry as the decoder commits it: level evidence first, then the look."""
    await light._apply_intent(None, intent, None, None, arrival)


async def test_drops_move_the_level_one_step_at_a_time_from_every_beat_of_the_track():
    light = await engine_joined_in_a_breakdown()
    assert [level for level, _ in light.asked] == [INTENSITY_CALM]

    feed(light, DRIVING_BEAT, 2 * WINDOW_BEATS)
    await commit(light, LightIntent.DROP, MAXIMAL_ARRIVAL)
    assert light.asked[-1] == (INTENSITY_BALANCED, 'drive +0.84')
    await commit(light, LightIntent.DROP, MAXIMAL_ARRIVAL)
    assert light.asked[-1] == (INTENSITY_WILD, 'drive +0.84')

    await commit(light, LightIntent.DROP, MAXIMAL_ARRIVAL)
    assert len(light.asked) == 3, 'no change once the level is right'


async def test_a_drop_keeps_a_level_the_whole_track_still_agrees_with():
    light = await engine_joined_in_a_breakdown()
    for intent in (LightIntent.BREAKDOWN, LightIntent.ATMOSPHERIC, LightIntent.BREAKDOWN):
        await commit(light, intent)
    feed(light, BREAKDOWN_BEAT, 64)
    await commit(light, LightIntent.DROP, MAXIMAL_ARRIVAL)
    assert light.follow_music.mean_energy == pytest.approx(1.4)
    assert len(light.asked) == 1


async def test_manual_mode_ignores_drops():
    light = await engine_joined_in_a_breakdown(intensity_auto=False)
    feed(light, DRIVING_BEAT, 2 * WINDOW_BEATS)
    light._follow_drop(MAXIMAL_ARRIVAL)
    light._follow_drop(MAXIMAL_ARRIVAL)
    assert light.asked == []
    assert light.settings.intensity == INTENSITY_BALANCED


async def test_a_new_track_measures_from_its_own_start():
    from autolux.tests.test_palette_policy import enter

    light = await engine_joined_in_a_breakdown()
    feed(light, DRIVING_BEAT, 2 * WINDOW_BEATS)
    light._after_silence = True
    await enter(light, LightIntent.ATMOSPHERIC)
    assert light._track_beats == []

    feed(light, BREAKDOWN_BEAT, WINDOW_BEATS)
    light._follow_drop(MAXIMAL_ARRIVAL)
    assert [level for level, _ in light.asked] == [INTENSITY_CALM]


def alternating(bpm, low, high, count):
    return [MeasuredBeat(bpm, (low, high)[index % 2], 2000.0, 0.0) for index in range(count)]


def test_a_doubled_reading_on_a_flat_level_counts_at_half_tempo():
    ballad = alternating(164.0, 0.97, 0.98, 64)
    assert level_drive(ballad) == pytest.approx(0.3 * -1.0 + 0.7 * loud_beats(ballad))
    assert level_drive(ballad) < 0.4


def test_drum_and_bass_at_real_tempo_keeps_its_tempo():
    # kjxyhFKv6r in the calibration table: 159.8 bpm, mean level 0.587, p90 0.677.
    dnb = alternating(159.8, 0.50, 0.677, 104)
    tempo = 2 * (159.8 - TEMPO_RANGE_BPM[0]) / (TEMPO_RANGE_BPM[1] - TEMPO_RANGE_BPM[0]) - 1
    assert level_drive(dnb) == pytest.approx(0.3 * tempo + 0.7 * loud_beats(dnb))
    assert tempo > 0.99


async def engine_at(intensity):
    from autolux.tests.test_palette_policy import engine_showing_an_intro

    light, _ = await engine_showing_an_intro()
    light.settings = ShowSettings(intensity=intensity, intensity_auto=True)
    light._settings = lambda: light.settings
    light.asked = []

    def apply(level, why):
        light.asked.append((level, why))
        light.settings = dataclasses.replace(light.settings, intensity=level)

    light.on_auto_intensity = apply
    return light


def play_bars(light, beats, bars):
    """Plays whole bars of beats, cycling the given ones, from the light's current beat."""
    for index in range(bars * BEATS_PER_BAR):
        light._measure_track_mood(beats[index % len(beats)], index % BEATS_PER_BAR)


def levels(light):
    return [level for level, _ in light.asked]


async def test_a_loud_pop_ballad_with_a_doubled_tempo_reads_calm_and_never_wild():
    # Elizabeth Taylor live: bpm read 164, a loud flat level, no drop.
    light = await engine_at(INTENSITY_WILD)
    ballad = alternating(164.0, 0.97, 0.98, 2)
    play_bars(light, ballad, 8)
    await commit(light, LightIntent.BUILDUP)
    assert light.follow_music.track_level == BALANCED, 'the first reading is never wild'
    [(level, why)] = light.asked
    assert level == INTENSITY_BALANCED and why == 'drive +0.31'
    play_bars(light, ballad, 8)
    await commit(light, LightIntent.BREAKDOWN)
    play_bars(light, ballad, 15)
    assert levels(light) == [INTENSITY_BALANCED], 'calm waits for the whole opening'
    play_bars(light, ballad, 1)
    assert levels(light) == [INTENSITY_BALANCED, INTENSITY_CALM]
    assert light.asked[-1][1] == 'no drop in the opening, mean energy 1.33'

    for _ in range(3):
        await commit(light, LightIntent.BUILDUP)
        play_bars(light, ballad, 16)
        await commit(light, LightIntent.BREAKDOWN)
        play_bars(light, ballad, 16)
    assert INTENSITY_WILD not in levels(light)


async def test_techno_with_loud_drops_commits_balanced_and_goes_wild_at_the_first_loud_drop():
    light = await engine_at(INTENSITY_BALANCED)
    play_bars(light, alternating(130.0, 0.85, 0.90, 2), 16)
    await commit(light, LightIntent.BUILDUP)
    assert light.follow_music.track_level == BALANCED and levels(light) == []
    play_bars(light, alternating(130.0, 0.92, 0.96, 2), 8)
    await commit(light, LightIntent.DROP, MAXIMAL_ARRIVAL)
    assert levels(light) == [INTENSITY_WILD]


async def test_every_step_up_waits_for_a_drop():
    # Around the World live: calm before, commit at drive -0.10, a calm opening, drop at +0.18.
    light = await engine_at(INTENSITY_CALM)
    intro = alternating(121.0, 0.78, 0.82, 2)
    play_bars(light, intro, 8)
    await commit(light, LightIntent.BUILDUP)
    assert light.follow_music.track_level == CALM, 'the commit holds calm'
    play_bars(light, intro, 8)
    await commit(light, LightIntent.BREAKDOWN)
    play_bars(light, intro, 16)
    assert light.follow_music.opening_heard and light.follow_music.track_level == CALM
    assert levels(light) == []
    play_bars(light, alternating(121.0, 0.85, 0.92, 2), 8)
    await commit(light, LightIntent.DROP, MAXIMAL_ARRIVAL)
    assert levels(light) == [INTENSITY_BALANCED], 'one step up, at the drop'


async def test_house_with_soft_drops_steps_up_to_balanced_at_its_first_drop_and_no_further():
    light = await engine_at(INTENSITY_CALM)
    house = alternating(125.0, 0.80, 0.86, 2)
    play_bars(light, house, 16)
    await commit(light, LightIntent.BUILDUP)
    play_bars(light, house, 8)
    for _ in range(3):
        await commit(light, LightIntent.DROP, 0.8)
        play_bars(light, house, 16)
        await commit(light, LightIntent.BREAKDOWN)
        play_bars(light, house, 8)
    assert levels(light) == [INTENSITY_BALANCED]


def test_a_drop_in_the_opening_rules_out_calm():
    follow = FollowMusic()
    follow.start_track(LightIntent.ATMOSPHERIC)
    follow.on_intent(LightIntent.DROP)
    for _ in range(4):
        follow.on_intent(LightIntent.BREAKDOWN)
    assert follow.mean_energy <= 1.5
    assert follow.on_track_mood(BALANCED) == BALANCED
    assert follow.on_opening_heard() is None


def test_the_opening_must_be_heard_before_calm():
    follow = FollowMusic()
    follow.start_track(LightIntent.ATMOSPHERIC)
    assert follow.on_track_mood(BALANCED) == BALANCED
    assert follow.on_opening_heard() == CALM
    assert CALM_OPENING_BEATS == 32 * BEATS_PER_BAR
