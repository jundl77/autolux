"""A track's Mood from its first bars, on fixed reference ranges rather than session statistics.

drive = mean(tempo on TEMPO_RANGE_BPM, level on LEVEL_RANGE)
weight = mean(level on LEVEL_RANGE, spectral centroid on CENTROID_RANGE_HZ)
temperature = bass-over-treble balance on BASS_OVER_TREBLE_RANGE_DB

Tempo is the analyser's folded bpm (85..170, so 170+ bpm drum & bass reads as ~85). Level is
the per-beat normalised RMS the room already receives (-48..-12 dBFS onto 0..1). Each range
maps linearly onto -1..1 and clamps outside it.

Calibration: tools/track_mood_measures.py over the first 8 bars of 8 eval-set tracks
(autolux/training/eval_audio; tempo is the labelled bpm, folded, because librosa's tracker
misread two of them). Raw means:

    track       genre        bpm    level  centroid Hz  bass/treble dB
    WyETTD0PP0  drum & bass   85.2  0.833  2287          -4.4
    kjxyhFKv6r  drum & bass  159.9  0.531  1695          -4.8
    XYrnLI3FFV  techno       128.9  0.606   452          18.2
    0yMOLeJRKr  tech house   127.0  0.688  4177         -23.2
    ND8VBV4P7w  trance       138.0  0.934  1790          -9.0
    wAbGhEja3s  trance       140.8  0.670  1402           4.7
    7w0BT6RBPp  other        117.0  0.565  1074          12.7
    g025pLdm4T  trance       134.0  0.621  3678         -15.0

Tempo, level and centroid ranges are the measured span; bass/treble is widened so the tracks
land at -0.8..+0.8. Tempo and intro level anti-correlate on this set, so drive only spans
-0.49 (7w0BT6RBPp) .. +0.71 (ND8VBV4P7w), and the first 8 bars are mostly intros.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, median

from autolux.lighting.palettes import Mood

MEASURED_BARS = 8
TEMPO_RANGE_BPM = (85.0, 160.0)
LEVEL_RANGE = (0.53, 0.93)
CENTROID_RANGE_HZ = (450.0, 4200.0)
BASS_OVER_TREBLE_RANGE_DB = (-28.0, 23.0)


@dataclass(frozen=True)
class MeasuredBeat:
    bpm: float
    level: float
    centroid_hz: float
    bass_over_treble_db: float


def on_axis(value: float, reference: tuple[float, float]) -> float:
    low, high = reference
    return max(-1.0, min(1.0, 2.0 * (value - low) / (high - low) - 1.0))


def track_mood(beats: Sequence[MeasuredBeat]) -> Mood:
    level = on_axis(fmean(beat.level for beat in beats), LEVEL_RANGE)
    brightness = on_axis(fmean(beat.centroid_hz for beat in beats), CENTROID_RANGE_HZ)
    tempo = on_axis(median(beat.bpm for beat in beats), TEMPO_RANGE_BPM)
    return Mood(
        temperature=on_axis(
            fmean(beat.bass_over_treble_db for beat in beats), BASS_OVER_TREBLE_RANGE_DB
        ),
        weight=fmean((level, brightness)),
        drive=fmean((tempo, level)),
    )


class TrackMoodMeter:
    def __init__(self, beats_to_measure: int):
        self._beats_to_measure = beats_to_measure
        self._beats: list[MeasuredBeat] | None = None

    @property
    def measuring(self) -> bool:
        return self._beats is not None

    def start(self) -> None:
        self._beats = []

    def add(self, beat: MeasuredBeat) -> Mood | None:
        if self._beats is None:
            return None
        self._beats.append(beat)
        return self.finish() if len(self._beats) >= self._beats_to_measure else None

    def finish(self) -> Mood | None:
        beats, self._beats = self._beats, None
        return track_mood(beats) if beats else None
