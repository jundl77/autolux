"""A track's Mood from its first bars, on fixed reference ranges rather than session statistics.

drive = mean(tempo on TEMPO_RANGE_BPM, level on LEVEL_RANGE)
weight = mean(level on LEVEL_RANGE, spectral centroid on CENTROID_RANGE_HZ)
temperature = bass-over-treble balance on BASS_OVER_TREBLE_RANGE_DB

Level is the per-beat normalised RMS the room already receives (-48..-12 dBFS onto 0..1).
Each reference range maps linearly onto -1..1 and clamps outside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, median

from autolux.lighting.palettes import Mood

MEASURED_BARS = 8
TEMPO_RANGE_BPM = (100.0, 150.0)
LEVEL_RANGE = (0.5, 1.0)
CENTROID_RANGE_HZ = (1000.0, 3500.0)
BASS_OVER_TREBLE_RANGE_DB = (5.0, 25.0)


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
