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
    ShowSettings,
)
from autolux.lib.engine.intensity_follow import FollowMusic, level_for_drive


@pytest.mark.parametrize(
    ('drive', 'current', 'level'),
    [
        (-0.52, BALANCED, CALM),
        (-0.29, BALANCED, BALANCED),
        (0.0, BALANCED, BALANCED),
        (0.41, BALANCED, WILD),
        (0.39, BALANCED, BALANCED),
        # Hysteresis: a level holds until the drive is clearly past its threshold.
        (-0.25, CALM, CALM),
        (-0.15, CALM, BALANCED),
        (0.35, WILD, WILD),
        (0.25, WILD, BALANCED),
    ],
)
def test_the_drive_picks_the_level_with_hysteresis(drive, current, level):
    assert level_for_drive(drive, current) == level


def test_a_soft_drop_caps_a_wild_track_once_and_never_flaps_back():
    follow = FollowMusic()
    assert follow.on_drop(0.5, BALANCED) is None, 'no level before the track is measured'
    assert follow.on_track_mood(0.6, BALANCED) == WILD
    assert follow.on_drop(0.95, WILD) is None, 'a maximal drop keeps wild'
    assert follow.on_drop(0.6, WILD) == BALANCED
    assert follow.on_drop(0.95, BALANCED) is None, 'one change per track'
    assert follow.on_track_mood(0.6, BALANCED) == WILD, 'the next track starts afresh'
    assert follow.on_drop(0.6, WILD) == BALANCED


class _Engine:
    def __init__(self, track_level=None):
        self.follow_music = SimpleNamespace(track_level=track_level)
        self.on_auto_intensity = None
        self.levels_sent = []
        self.paused, self.master_level, self.mode = False, 1.0, 'auto'
        self.static_looks = SimpleNamespace(state=lambda: {})

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

    light._follow_track_mood(SimpleNamespace(drive=0.6))
    assert asked == [(INTENSITY_WILD, 'drive +0.60')]
    settings = ShowSettings(intensity=INTENSITY_WILD, intensity_auto=True)
    light._follow_drop(0.62)
    assert asked[-1] == (INTENSITY_BALANCED, 'drop arrival 0.62')

    settings = ShowSettings(intensity_auto=False)
    light._follow_track_mood(SimpleNamespace(drive=-0.6))
    assert len(asked) == 2, 'with auto off the mood is only remembered'
    assert light.follow_music.track_level == CALM


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
    assert controller._intense_blocked(control.settings, 0.0) is None

    light.on_auto_intensity(INTENSITY_CALM, 'drive -0.52')
    assert 'at or below' in controller._intense_blocked(control.settings, 0.0)


async def test_a_mood_that_commits_at_the_section_after_an_early_drop_still_sets_the_level():
    from autolux.lib.engine.effect_definitions import LightIntent
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

    measure_beats(light, 4)
    await enter(light, LightIntent.ATMOSPHERIC)
    assert light.follow_music.track_level == BALANCED
    [(level, why)] = asked
    assert level == INTENSITY_BALANCED and why.startswith('drive ')
