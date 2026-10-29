import numpy as np
import pytest

from autolux.lib.analyser.madmom_rhythm import FRAME_SIZE, SAMPLE_RATE
from autolux.lib.analyser.music_analyser import (
    BAND_RANGE_DB,
    REFERENCE_HALF_LIFE_BEATS,
    band_powers,
    decayed_reference,
    kick_flux,
    onset_strength,
    relative_level,
)
from autolux.lib.engine.cue import BaseLook, Cue, Overlay
from autolux.lib.engine.effect_definitions import LightIntent
from cli.starfire_protocol import MAX_MESSAGE_BYTES, build_command


def magnitude(hz: float, amplitude: float) -> np.ndarray:
    frame = amplitude * np.sin(2 * np.pi * hz * np.arange(FRAME_SIZE) / SAMPLE_RATE)
    return np.abs(np.fft.rfft(frame * np.hanning(FRAME_SIZE)))


def test_a_bass_tone_reads_as_bass_and_only_kick_band_arrivals_are_onsets():
    bass = band_powers(magnitude(60.0, 0.5))
    assert bass['bass'] > 100 * bass['mid'] and bass['bass'] > 1e4 * bass['high']
    silence = np.zeros(FRAME_SIZE // 2 + 1)
    assert kick_flux(silence, magnitude(60.0, 0.5)) > 0
    assert kick_flux(magnitude(60.0, 0.5), silence) == 0.0, 'a decay is no onset'
    assert kick_flux(silence, magnitude(1000.0, 0.5)) < 1e-3 * kick_flux(
        silence, magnitude(60.0, 0.5)
    ), 'a pad above the kick band is no onset'


def test_levels_are_relative_to_the_track_and_bounded():
    for gain in (1e-4, 1.0, 1e4):
        assert relative_level(0.5 * gain, gain) == pytest.approx(
            1 + 10 * np.log10(0.5) / BAND_RANGE_DB
        )
    assert relative_level(2.0, 1.0) == 1.0
    assert relative_level(0.0, 1.0) == 0.0
    assert relative_level(1.0, 0.0) == 0.0
    assert onset_strength(3.0, 2.0) == 1.0 and onset_strength(1.0, 2.0) == 0.5
    assert onset_strength(1.0, 0.0) == 0.0


def test_the_track_reference_halves_over_its_half_life_and_follows_new_peaks():
    reference = 1.0
    for _ in range(REFERENCE_HALF_LIFE_BEATS):
        reference = decayed_reference(reference, 0.0)
    assert reference == pytest.approx(0.5)
    assert decayed_reference(reference, 3.0) == 3.0


def test_the_largest_cue_fits_the_realtime_cap_and_round_trips():
    name = 'x' * 60
    cue = Cue(
        start_beat_index=2**63,
        intent=LightIntent.DROP,
        rotation=tuple(
            BaseLook(name, name + 'y' * 7, 2**31, {'kind': 'crossfade', 'duration_beats': 1024.0})
            for _ in range(8)
        ),
        palette='techno',
        overlays=tuple(
            Overlay(name, name + 'y' * 7, -(2**31), -64.0, 1024.0, 0, 1024.0, 1024.0)
            for _ in range(8)
        ),
        drop_strength=0.5,
        seed=2**32 - 1,
    )
    command = cue.command()
    command['value']['palette'] = 'p' * 128
    message = build_command(command)
    assert message.ByteSize() <= MAX_MESSAGE_BYTES
    assert message.rotation[0].autoloop_id == f'{name}/{name}yyyyyyy'
    assert message.overlays[0].begin_beats == -64.0
    assert (message.seed, message.drop_strength) == (2**32 - 1, 0.5)


@pytest.mark.parametrize(
    'change',
    [
        {'rotation': ()},
        {'rotation': (BaseLook('drop', 'kick', 0),)},
        {'overlays': (Overlay('hit', 'blinder', 40, 1025.0, 2.0),)},
        {'overlays': (Overlay('fill', 'wipe', 20, 0.0, 4.0, every_bars=1, release_beats=1.0),)},
        {'overlays': (Overlay('fill', 'wipe', 20, 4.0, 1.0, every_bars=1),)},
        {'overlays': (Overlay('fill', 'wipe', 20, -1.0, 4.0, every_bars=8),)},
        {'overlays': (Overlay('hit', 'blinder', 40, 0.0, 0.0),)},
        {'intent': LightIntent.BUILDUP, 'drop_strength': 0.5},
        {'drop_strength': 1.5},
        {'seed': 2**32},
        {'palette': 'no such palette'},
        {'rotation': (BaseLook('drop', 'kick', 16, {'kind': 'crossfade', 'duration_beats': 0}),)},
        {'rotation': (BaseLook('drop', 'kick', 16, {'kind': 'crossfade', 'duration_beats': 2e3}),)},
        {'rotation': (BaseLook('drop', 'kick', 16, {'kind': 'fade'}),)},
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


def test_one_shot_rows_may_begin_after_the_start():
    Cue(
        8,
        LightIntent.DROP,
        (BaseLook('drop', 'kick', 16),),
        'techno',
        overlays=(
            Overlay('hit', 'blinder', 40, 62.0, 1.0),
            Overlay('fill', 'wipe', 20, 0.0, 3.0, every_bars=1, release_beats=1.0),
        ),
    )


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
