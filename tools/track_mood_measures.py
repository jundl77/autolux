"""Print the per-track measures the palette mood is built from, for a directory of audio.

The live engine measures from a new track until its first buildup or drop, at most
MAX_MEASURED_BARS bars; this replays that offline with the same per-beat features (folded
tempo, normalised RMS level, spectral centroid, bass-over-treble balance) so the reference
ranges in track_mood can be calibrated. Beats come from librosa's DSP beat tracker, not the
live madmom network. With --labels (an eval_labels.json) the window ends at the labelled
first buildup or drop and the tempo is the labelled bpm; without, the window is the cap.

    python tools/track_mood_measures.py autolux/training/eval_audio \
        --labels autolux/training/eval_labels.json --limit 8

Each file is decoded (or read from its .44100.npy cache) and processed one at a time.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
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
from autolux.lib.engine.track_mood import (  # noqa: E402
    DRIVE_LEVEL_PERCENTILE,
    MAX_MEASURED_BARS,
    MeasuredBeat,
    percentile,
    track_mood,
)

AUDIO_SUFFIXES = ('.mp3', '.wav', '.flac', '.ogg')
DECODED_SUFFIX = f'.{SAMPLE_RATE}.npy'
WINDOW_ENDING_SECTIONS = ('buildup', 'drop')


def labelled_tracks(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    tracks = json.loads(path.read_text(encoding='utf-8'))['tracks']
    return {audio_stem(track['id']): track for track in tracks}


def audio_stem(youtube_id: str) -> str:
    digest = hashlib.sha256(youtube_id.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()[:10]


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
    levels = [beat.level for beat in beats]
    return (
        f'{name:12s} {len(beats):4d} {median(b.bpm for b in beats):6.1f} '
        f'{fmean(levels):6.3f} {percentile(levels, DRIVE_LEVEL_PERCENTILE):6.3f} '
        f'{fmean(b.centroid_hz for b in beats):8.0f} '
        f'{fmean(b.bass_over_treble_db for b in beats):7.1f}   '
        f'{mood.temperature:+.2f} {mood.weight:+.2f} {mood.drive:+.2f}'
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('audio', type=Path, nargs='+', help='audio files or directories')
    parser.add_argument('--labels', type=Path, help='eval_labels.json for section windows')
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args(argv)
    labels = labelled_tracks(args.labels)
    paths = [
        path
        for given in args.audio
        for path in (sorted(given.iterdir()) if given.is_dir() else [given])
        if path.suffix in AUDIO_SUFFIXES
    ]
    print(
        f'{"track":12s} {"beats":>4s} {"bpm":>6s} {"level":>6s} {"p90":>6s} {"centroid":>8s} '
        f'{"bass/tr":>7s}   temp   weight drive'
    )
    for path in paths[: args.limit]:
        samples = load(path)
        bpm, beats = beat_samples(samples, sound_start(samples))
        track = labels.get(path.stem)
        if track is not None:
            bpm = MusicAnalyser._fold_bpm(track['average_bpm'])
            ends = [
                section['start']
                for section in track['sections']
                if section['name'] in WINDOW_ENDING_SECTIONS
            ]
            if ends:
                beats = beats[beats < min(ends) * SAMPLE_RATE]
        beats = beats[: MAX_MEASURED_BARS * BEATS_PER_BAR]
        measured = [beat for at in beats if (beat := measured_beat(samples, int(at), bpm))]
        print(row(path.stem, measured), flush=True)


if __name__ == '__main__':
    main()
