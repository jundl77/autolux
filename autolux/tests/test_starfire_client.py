import math

import pytest

from autolux.lib.clients.starfire_client import StarfireClient, rms_to_normalized_level


@pytest.mark.parametrize('dbfs, expected', [(-90, 0), (-48, 0), (-30, 0.5), (-12, 1), (0, 1)])
def test_loudness_has_a_calibrated_bounded_scale(dbfs, expected):
    assert rms_to_normalized_level(10 ** (dbfs / 20)) == pytest.approx(expected)


def test_silence_has_zero_normalized_rms_level():
    assert rms_to_normalized_level(0) == 0


@pytest.mark.parametrize('rms', [-0.1, math.inf, -math.inf, math.nan])
def test_invalid_signal_measurements_are_rejected(rms):
    with pytest.raises(ValueError, match='finite and nonnegative'):
        rms_to_normalized_level(rms)


def test_rejected_selection_does_not_change_cached_identity():
    calls = []

    def send(command):
        calls.append(command)
        if len(calls) == 1:
            raise RuntimeError('renderer queue full')

    client = StarfireClient(send)
    with pytest.raises(RuntimeError, match='queue full'):
        client.select_sequence('drop', 'blue_impact')
    assert client.selected_sequence is None
    assert client.select_sequence('drop', 'blue_impact')
    assert not client.select_sequence('drop', 'blue_impact')
    assert len(calls) == 2


async def test_measured_energy_and_beat_share_the_playback_delay():
    from autolux.tests.test_light_engine import engine

    light, queue, clock, lighting = engine()
    light.analyser.get_rms_energy = lambda: 0.01
    await light.on_beat(5, 120.0, False)
    light.analyser.get_rms_energy = lambda: 0.1
    clock.advance(0.5)
    await light.on_beat(6, 120.0, False)
    clock.advance(13.49)
    await queue.drain()
    assert lighting.events == []
    clock.advance(0.02)
    await queue.drain()
    first = [event['command'] for event in lighting.events]
    assert first == [
        {'kind': 'set_normalized_rms_level', 'level': pytest.approx(rms_to_normalized_level(0.01))},
        {'kind': 'beat', 'position_beats': 1, 'bpm': 120.0},
    ]
    clock.advance(0.5)
    await queue.drain()
    assert lighting.events[-2]['command']['level'] > first[0]['level']


async def test_warmup_never_sends_zero_tempo_and_song_stop_preserves_beat_ordinals():
    from autolux.tests.test_light_engine import engine

    light, _, _, lighting = engine(queue=False)
    await light.on_beat(1, 0.0, False)
    assert [event['command']['kind'] for event in lighting.events] == ['set_normalized_rms_level']
    await light.on_beat(2, 120.0, False)
    light.on_sound_stop()
    light._show_sound_stop()
    light.on_sound_start()
    await light.on_beat(3, 0.0, False)
    anchors = [event['command'] for event in lighting.events if event['command']['kind'] == 'beat']
    assert [anchor['position_beats'] for anchor in anchors] == [2, 3]
    assert [anchor['bpm'] for anchor in anchors] == [120.0, 120.0]


@pytest.mark.parametrize('playback_stop', [False, True])
async def test_song_relative_beat_reset_cannot_rewind_starfire(playback_stop):
    from autolux.tests.test_light_engine import engine

    light, queue, clock, lighting = engine()
    await light.on_beat(1, 120.0, False)
    await light.on_beat(2, 120.0, False)
    clock.advance(14.0)
    await queue.drain()
    light.on_sound_stop()
    if playback_stop:
        light._show_sound_stop()
    light.on_sound_start()
    # The analyser resets its own count even when silence is too short to stop lights.
    await light.on_beat(1, 120.0, False)
    await light.on_beat(2, 120.0, False)
    clock.advance(14.0)
    await queue.drain()
    anchors = [event['command'] for event in lighting.events if event['command']['kind'] == 'beat']
    assert [anchor['position_beats'] for anchor in anchors] == [1, 2, 3, 4]
