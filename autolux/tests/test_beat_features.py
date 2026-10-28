import numpy as np
import pytest

from autolux.lib.analyser.madmom_rhythm import FRAME_SIZE, SAMPLE_RATE
from autolux.lib.analyser.music_analyser import band_levels_dbfs, onset_strength
from autolux.lib.clients.starfire_client import band_to_normalized_level
from autolux.lib.engine.cue import BaseLook, Cue, Overlay
from autolux.lib.engine.effect_definitions import LightIntent
from cli.starfire_protocol import MAX_MESSAGE_BYTES, build_command


def sine(hz: float, amplitude: float) -> np.ndarray:
    return amplitude * np.sin(2 * np.pi * hz * np.arange(FRAME_SIZE) / SAMPLE_RATE)


def test_a_bass_tone_reads_as_bass_and_a_full_scale_tone_as_its_rms():
    levels = band_levels_dbfs(sine(60.0, 0.5))
    assert levels['bass'] > levels['mid'] + 20 and levels['bass'] > levels['high'] + 40
    # Parseval: a full-scale sine has RMS 1/sqrt(2), -3 dBFS.
    assert band_levels_dbfs(sine(1000.0, 1.0))['mid'] == pytest.approx(-3.0, abs=0.5)
    assert band_levels_dbfs(np.zeros(FRAME_SIZE))['high'] < -100


def test_features_are_bounded_and_deterministic():
    frame = sine(60.0, 0.5) + sine(5000.0, 0.01)
    assert band_levels_dbfs(frame) == band_levels_dbfs(frame.copy())
    for dbfs in (-200.0, -60.0, -36.0, -12.0, 0.0, float('inf'), float('-inf')):
        assert 0.0 <= band_to_normalized_level(dbfs) <= 1.0
    peaks = [0.0, 1.0, 2.0, 50.0, 3.0]
    assert onset_strength(50.0, peaks) == 1.0
    assert 0.0 < onset_strength(2.0, peaks) < 1.0
    assert onset_strength(2.0, peaks) == onset_strength(2.0, list(peaks))
    assert onset_strength(1.0, [0.0, 0.0]) == 0.0


def test_the_largest_cue_fits_the_realtime_cap_and_round_trips():
    name = 'x' * 60
    cue = Cue(
        start_beat_index=2**63,
        intent=LightIntent.DROP,
        rotation=tuple(
            BaseLook(name, name + 'y' * 7, 2**31, {'kind': 'crossfade', 'duration_beats': 1e300})
            for _ in range(8)
        ),
        palette='p' * 128,
        overlays=tuple(
            Overlay(name, name + 'y' * 7, -(2**31), -64.0, 1e300, 0, 1e300, 1e300) for _ in range(8)
        ),
        drop_strength=0.5,
        seed=2**32 - 1,
    )
    message = build_command(cue.command())
    assert message.ByteSize() <= MAX_MESSAGE_BYTES
    assert message.rotation[0].autoloop_id == f'{name}/{name}yyyyyyy'
    assert message.overlays[0].begin_beats == -64.0
    assert (message.seed, message.drop_strength) == (2**32 - 1, 0.5)


@pytest.mark.parametrize(
    'change',
    [
        {'rotation': ()},
        {'rotation': (BaseLook('drop', 'kick', 0),)},
        {'overlays': (Overlay('hit', 'blinder', 40, 1.0, 2.0),)},
        {'overlays': (Overlay('fill', 'wipe', 20, -1.0, 4.0, every_bars=8),)},
        {'overlays': (Overlay('hit', 'blinder', 40, 0.0, 0.0),)},
        {'intent': LightIntent.BUILDUP, 'drop_strength': 0.5},
        {'drop_strength': 1.5},
        {'seed': 2**32},
    ],
)
def test_an_unsendable_cue_cannot_be_built(change):
    fields = {
        'start_beat_index': 8,
        'intent': LightIntent.DROP,
        'rotation': (BaseLook('drop', 'kick', 16),),
        'palette': 'techno',
    }
    with pytest.raises(ValueError):
        Cue(**(fields | change))


def test_beats_and_cue_clears_adapt_to_their_messages():
    beat = build_command(
        {
            'kind': 'beat',
            'beat_index': 3,
            'bar_phase': 0,
            'beats_per_bar': 4,
            'bpm': 128.0,
            'features': {'onset': 1.0, 'bass': 0.5, 'mid': 0.25, 'high': 0.0},
        }
    )
    assert (beat.features.onset, beat.features.mid) == (1.0, 0.25)
    plain = build_command({'kind': 'beat', 'beat_index': 1, 'beats_per_bar': 4, 'bpm': 1.0})
    assert not plain.HasField('features')
    assert type(build_command({'kind': 'clear_cues'})).__name__ == 'ClearCues'
