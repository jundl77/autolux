"""Dashboard room time must agree with delayed lighting and retained audio."""

import pytest

from autolux.simulate.visualizer_app import _room_beats, _room_is_playing, _room_sound_events
from autolux.tests.test_light_engine import engine, hold_silence


async def test_short_song_and_following_song_use_their_own_delayed_boundaries():
    light, queue, clock, _ = engine(events=True, playing=False)
    light.on_sound_start()
    clock.advance(0.5)
    light.event_buffer.add_beat(128, False)
    clock.advance(0.5)
    light.on_sound_stop()
    await hold_silence(light, clock)
    light.on_sound_start()
    second_start = clock.monotonic() + queue.delay_sec

    for at, playing in [(13.9, False), (14.5, True), (15.0, False), (second_start, True)]:
        clock.advance(at - clock.monotonic())
        await queue.drain()
        snapshot = light.event_buffer.snapshot()
        assert _room_is_playing(snapshot) is playing
        assert light._room_playing is playing

    assert [(event['t'], event['playing']) for event in _room_sound_events(snapshot)] == [
        (14.0, True),
        (15.0, False),
        (pytest.approx(second_start), True),
    ]
    assert [beat['t'] for beat in _room_beats(snapshot)] == [14.5]


async def test_brief_source_gap_does_not_stop_room_playback():
    light, queue, clock, _ = engine(events=True, playing=False)
    light.on_sound_start()
    clock.advance(1.0)
    light.on_sound_stop()
    await hold_silence(light, clock, 1.0)
    light.on_sound_start()
    clock.advance(20.0)
    await queue.drain()
    snapshot = light.event_buffer.snapshot()
    assert _room_is_playing(snapshot) and light._room_playing
    assert _room_sound_events(snapshot) == [{'t': 14.0, 'playing': True}]
