from __future__ import annotations

import math
import random
from collections.abc import Sequence

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lighting.palettes import BREAKDOWN_SUFFIX, DROP_SUFFIX, Mood, Palette

RECENCY_CAP_TRACKS = 4
FINALISTS = 3
FARTHEST_MOODS_APART = math.dist((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
VARIANT_SUFFIXES = {LightIntent.BREAKDOWN: BREAKDOWN_SUFFIX, LightIntent.DROP: DROP_SUFFIX}


def palette_variant(base: str, intent: LightIntent) -> str:
    return base + VARIANT_SUFFIXES.get(intent, '')


def mood_closeness(first: Mood, second: Mood) -> float:
    return 1.0 - math.dist(first.axes(), second.axes()) / FARTHEST_MOODS_APART


class PalettePolicy:
    def __init__(self, palettes: Sequence[Palette], rng: random.Random):
        if len(palettes) < 2:
            raise ValueError('palette rotation needs at least two palettes')
        self._palettes = tuple(palettes)
        self._rng = rng
        self._tracks = 0
        self._track_used: dict[str, int] = {}
        self._track_palette = self._palettes[0].name

    @property
    def track_palette(self) -> str:
        return self._track_palette

    def variant_for(self, intent: LightIntent) -> str:
        return palette_variant(self._track_palette, intent)

    def choose_for_track(self, mood: Mood) -> str:
        finalists = sorted(
            self._palettes, key=lambda palette: self.score(palette, mood), reverse=True
        )[:FINALISTS]
        weights = [self.score(palette, mood) for palette in finalists]
        chosen = self._rng.choices(finalists, weights)[0].name
        self._tracks += 1
        self._track_used[chosen] = self._tracks
        self._track_palette = chosen
        return chosen

    def score(self, palette: Palette, mood: Mood) -> float:
        return mood_closeness(palette.mood, mood) * self._recency_weight(palette.name)

    def _recency_weight(self, name: str) -> float:
        if name not in self._track_used:
            return 1.0
        tracks_since_used = self._tracks + 1 - self._track_used[name]
        return min(tracks_since_used, RECENCY_CAP_TRACKS) / RECENCY_CAP_TRACKS
