"""A track's Mood from its opening, on fixed reference ranges rather than session statistics.

The window runs from new_track to the first BUILDUP or DROP entry, at most MAX_MEASURED_BARS.

drive = 0.6 x tempo on TEMPO_RANGE_BPM + 0.4 x 90th-percentile level on DRIVE_LEVEL_RANGE
weight = mean(mean level on LEVEL_RANGE, spectral centroid on CENTROID_RANGE_HZ)
temperature = bass-over-treble balance on BASS_OVER_TREBLE_RANGE_DB

Tempo is the analyser's folded bpm, 85..170: the same fold that keeps the beat grid in one
octave. Genres at 170 bpm or more (drum & bass, hardcore) therefore read at half tempo, ~85,
and their drive comes mostly from level; unfolding here would disagree with the beat grid
the rest of the show runs on. Level is the per-beat normalised RMS the room already
receives (-48..-12 dBFS onto 0..1); the 90th percentile lets an opening's loud beats count
for drive even when its quiet bars pull the mean down. Each range maps linearly onto
-1..1 and clamps outside it.

Calibration: tools/track_mood_measures.py --labels over 8 eval-set tracks
(autolux/training/eval_audio), window ended at the labelled first buildup or drop; tempo is
the labelled bpm, folded, because librosa's tracker misread two of them. Raw values:

    track       genre        beats  bpm    mean   p90    centroid Hz  bass/treble dB
    WyETTD0PP0  drum & bass  126     85.2  0.820  0.923  2683          -4.4
    kjxyhFKv6r  drum & bass  104    159.8  0.587  0.677  2078          -5.6
    XYrnLI3FFV  techno        96    128.9  0.613  0.694   737          13.6
    0yMOLeJRKr  tech house    64    127.0  0.672  0.829  4203         -22.8
    ND8VBV4P7w  trance        51    138.0  0.934  1.000  1972          -8.7
    wAbGhEja3s  trance       112    140.9  0.657  0.710  2107           0.5
    7w0BT6RBPp  other        127    117.0  0.663  0.751  1461          10.3
    g025pLdm4T  trance        64    134.0  0.649  0.740  3630         -15.2

Tempo, level and centroid ranges are the measured span; bass/treble is widened so the tracks
land at -0.8..+0.8. On this set drive spans -0.39 (WyETTD0PP0) .. +0.65 (ND8VBV4P7w),
weight -0.93 .. +0.36 and temperature -0.81 .. +0.80.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from statistics import fmean, median

from autolux.lighting.palettes import Mood

MAX_MEASURED_BARS = 32
DRIVE_TEMPO_SHARE = 0.6
DRIVE_LEVEL_PERCENTILE = 90
TEMPO_RANGE_BPM = (85.0, 160.0)
LEVEL_RANGE = (0.59, 0.93)
DRIVE_LEVEL_RANGE = (0.68, 1.0)
CENTROID_RANGE_HZ = (740.0, 4200.0)
BASS_OVER_TREBLE_RANGE_DB = (-27.0, 18.0)


@dataclass(frozen=True)
class MeasuredBeat:
    bpm: float
    level: float
    centroid_hz: float
    bass_over_treble_db: float


def on_axis(value: float, reference: tuple[float, float]) -> float:
    low, high = reference
    return max(-1.0, min(1.0, 2.0 * (value - low) / (high - low) - 1.0))


def percentile(values: Sequence[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, ceil(percent / 100.0 * len(ordered)) - 1)]


def track_mood(beats: Sequence[MeasuredBeat]) -> Mood:
    level = on_axis(fmean(beat.level for beat in beats), LEVEL_RANGE)
    brightness = on_axis(fmean(beat.centroid_hz for beat in beats), CENTROID_RANGE_HZ)
    tempo = on_axis(median(beat.bpm for beat in beats), TEMPO_RANGE_BPM)
    loud_beats = on_axis(
        percentile([beat.level for beat in beats], DRIVE_LEVEL_PERCENTILE), DRIVE_LEVEL_RANGE
    )
    return Mood(
        temperature=on_axis(
            fmean(beat.bass_over_treble_db for beat in beats), BASS_OVER_TREBLE_RANGE_DB
        ),
        weight=fmean((level, brightness)),
        drive=DRIVE_TEMPO_SHARE * tempo + (1.0 - DRIVE_TEMPO_SHARE) * loud_beats,
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
