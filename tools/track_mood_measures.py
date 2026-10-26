"""Print the per-track measures the palette mood is built from, for a directory of audio.

The live engine measures the first MEASURED_BARS bars after a new track; this replays that
offline with the same per-beat features (folded tempo, normalised RMS level, spectral
centroid, bass-over-treble balance) so the reference ranges in track_mood can be calibrated.
Beats come from librosa's DSP beat tracker, not the live madmom network.

    python tools/track_mood_measures.py autolux/training/eval_audio --limit 8

Each file is decoded (or read from its .44100.npy cache) and processed one at a time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from statistics import fmean, median

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autolux.lib.analyser.madmom_rhythm import FRAME_SIZE, SAMPLE_RATE  # noqa: E402
from autolux.lib.analyser.music_analyser import (  # noqa: E402
    _ENERGY_WINDOW_BUFFERS,
    _SILENCE_RMS,
    MusicAnalyser,
    spectral_balance,
)
from autolux.lib.audio_config import BUFFER_SIZE  # noqa: E402
from autolux.lib.clients.starfire_client import rms_to_normalized_level  # noqa: E402
from autolux.lib.engine.section_decoder import BEATS_PER_BAR  # noqa: E402
from autolux.lib.engine.track_mood import MEASURED_BARS, MeasuredBeat, track_mood  # noqa: E402

AUDIO_SUFFIXES = ('.mp3', '.wav', '.flac', '.ogg')
DECODED_SUFFIX = f'.{SAMPLE_RATE}.npy'


def load(path: Path) -> np.ndarray:
    decoded = path.with_name(path.name + DECODED_SUFFIX)
    if decoded.is_file():
        return np.load(decoded).astype(np.float32)
    import librosa

    samples, _ = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    return samples


def sound_start(samples: np.ndarray) -> int:
    buffers = len(samples) // BUFFER_SIZE
    rms = np.sqrt(np.mean(samples[: buffers * BUFFER_SIZE].reshape(buffers, -1) ** 2, axis=1))
    loud = np.flatnonzero(rms >= _SILENCE_RMS)
    return int(loud[0]) * BUFFER_SIZE if len(loud) else 0


def beat_samples(samples: np.ndarray, start: int) -> tuple[float, np.ndarray]:
    import librosa

    tempo, beats = librosa.beat.beat_track(y=samples[start:], sr=SAMPLE_RATE, units='samples')
    return MusicAnalyser._fold_bpm(float(np.atleast_1d(tempo)[0])), start + beats


def measured_beat(samples: np.ndarray, at: int, bpm: float) -> MeasuredBeat | None:
    window = _ENERGY_WINDOW_BUFFERS * BUFFER_SIZE
    if at < max(window, FRAME_SIZE):
        return None
    buffers = samples[at - window : at].reshape(_ENERGY_WINDOW_BUFFERS, BUFFER_SIZE)
    rms = float(np.mean(np.sqrt(np.mean(buffers**2, axis=1))))
    spectrum = spectral_balance(samples[at - FRAME_SIZE : at].astype(np.float64))
    return MeasuredBeat(
        bpm, rms_to_normalized_level(rms), spectrum.centroid_hz, spectrum.bass_over_treble_db
    )


def row(name: str, beats: list[MeasuredBeat]) -> str:
    mood = track_mood(beats)
    return (
        f'{name:24s} {len(beats):4d} {median(b.bpm for b in beats):6.1f} '
        f'{fmean(b.level for b in beats):6.3f} {fmean(b.centroid_hz for b in beats):8.0f} '
        f'{fmean(b.bass_over_treble_db for b in beats):7.1f}   '
        f'{mood.temperature:+.2f} {mood.weight:+.2f} {mood.drive:+.2f}'
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('audio', type=Path, nargs='+', help='audio files or directories')
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args(argv)
    paths = [
        path
        for given in args.audio
        for path in (sorted(given.iterdir()) if given.is_dir() else [given])
        if path.suffix in AUDIO_SUFFIXES
    ]
    print(
        f'{"track (window)":24s} {"beats":>4s} {"bpm":>6s} {"level":>6s} {"centroid":>8s} '
        f'{"bass/tr":>7s}   temp   weight drive'
    )
    for path in paths[: args.limit]:
        samples = load(path)
        bpm, beats = beat_samples(samples, sound_start(samples))
        measured = [beat for at in beats if (beat := measured_beat(samples, int(at), bpm))]
        stem = path.stem
        print(row(f'{stem} first {MEASURED_BARS} bars', measured[: MEASURED_BARS * BEATS_PER_BAR]))
        print(row(f'{stem} whole', measured), flush=True)


if __name__ == '__main__':
    main()
