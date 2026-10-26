import numpy as np
import pytest

from autolux.lib.analyser.music_analyser import (
    TRACK_GAP_IGNORED_AFTER_START_SECONDS,
    MusicAnalyser,
)
from autolux.lib.clock import VirtualClock
from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.light_engine import RESYNC_GUARD_SECONDS, LightEngine
from autolux.simulate.lighting_recorder import RecordingStarfireClient
from autolux.tests.test_music_analyser import _FakeRhythm, _StubHandler

SAMPLE_RATE, BUFFER = 44100, 256
PLAYBACK_DELAY_SEC = 14.0


class GapRecorder(_StubHandler):
    def __init__(self):
        self.gaps = []
        self.silences = 0

    def on_digital_gap(self, started_sec, seconds):
        self.gaps.append((started_sec, seconds))

    def on_digital_silence(self):
        self.silences += 1


async def play(monkeypatch, plan):
    monkeypatch.setattr(
        'autolux.lib.analyser.music_analyser.MadmomRhythm', lambda *_: _FakeRhythm()
    )
    clock = VirtualClock()
    handler = GapRecorder()
    analyser = MusicAnalyser(SAMPLE_RATE, BUFFER, handler, clock=clock)
    for level, seconds in plan:
        for _ in range(round(seconds * SAMPLE_RATE / BUFFER)):
            clock.advance(BUFFER / SAMPLE_RATE)
            await analyser.analyse(np.full(BUFFER, level, dtype=np.float32))
    return handler


async def test_fifty_ms_of_exact_silence_after_the_opening_is_a_new_track_cue(monkeypatch):
    handler = await play(monkeypatch, [(0.2, 3.0), (0.0, 0.06), (0.2, 1.0)])
    [(started, seconds)] = handler.gaps
    assert started == pytest.approx(3.0, abs=0.01)
    assert seconds == pytest.approx(0.06, abs=0.006)
    assert handler.silences == 0


async def test_a_twenty_ms_dropout_is_no_cue(monkeypatch):
    handler = await play(monkeypatch, [(0.2, 3.0), (0.0, 0.02), (0.2, 1.0)])
    assert handler.gaps == []


async def test_a_gap_in_the_first_two_seconds_of_a_track_is_no_cue(monkeypatch):
    early = TRACK_GAP_IGNORED_AFTER_START_SECONDS - 0.5
    handler = await play(
        monkeypatch,
        [(0.2, early), (0.0, 0.1), (0.2, 3.0), (0.0, 0.1), (0.2, 1.0), (0.0, 0.1), (0.2, 0.5)],
    )
    assert len(handler.gaps) == 1, 'only the gap 2 s into the track counts; the next is too soon'


async def test_two_seconds_of_silence_is_still_standby_not_a_cue(monkeypatch):
    handler = await play(monkeypatch, [(0.2, 3.0), (0.0, 2.1), (0.2, 1.0)])
    assert handler.silences == 1
    assert handler.gaps == []


def light_engine():
    clock = VirtualClock()
    lighting = RecordingStarfireClient(clock=clock)
    queue = DelayedCommandQueue(PLAYBACK_DELAY_SEC, clock=clock)
    light = LightEngine(
        lighting,
        EffectController(lighting),
        queue,
        playback_delay_sec=PLAYBACK_DELAY_SEC,
        clock=clock,
    )
    light._after_silence = False
    return light, queue, clock


def new_track(light, clock):
    return light._selection_context(LightIntent.ATMOSPHERIC, 'intro', clock.monotonic()).new_track


async def test_a_gap_cue_starts_a_track_when_the_room_hears_the_gap():
    light, queue, clock = light_engine()
    light.on_digital_gap(30.0, 0.06)
    clock.advance(PLAYBACK_DELAY_SEC - 0.1)
    await queue.drain()
    assert not new_track(light, clock)
    clock.advance(0.2)
    await queue.drain()
    assert new_track(light, clock)


@pytest.mark.parametrize('resync_offset', [-RESYNC_GUARD_SECONDS + 0.5, 1.0])
async def test_a_gap_at_a_capture_resync_is_no_new_track(resync_offset):
    light, queue, clock = light_engine()
    light.note_capture_discontinuities(3)
    if resync_offset < 0:
        light.note_capture_discontinuities(4)
        clock.advance(-resync_offset)
        light.on_digital_gap(30.0, 0.06)
    else:
        light.on_digital_gap(30.0, 0.06)
        clock.advance(resync_offset)
        light.note_capture_discontinuities(4)
    clock.advance(PLAYBACK_DELAY_SEC + 1.0)
    await queue.drain()
    assert not new_track(light, clock)


async def test_a_resync_long_before_the_gap_does_not_mask_it():
    light, queue, clock = light_engine()
    light.note_capture_discontinuities(3)
    light.note_capture_discontinuities(4)
    clock.advance(RESYNC_GUARD_SECONDS + 1.0)
    light.on_digital_gap(30.0, 0.06)
    clock.advance(PLAYBACK_DELAY_SEC + 0.1)
    await queue.drain()
    assert new_track(light, clock)
