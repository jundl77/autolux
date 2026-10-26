from __future__ import annotations

import random
from collections.abc import Sequence

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lighting.palettes import BREAKDOWN_SUFFIX, Palette

TEMPERATURE_SIGN = {'warm': 1.0, 'neutral': 0.0, 'cold': -1.0}
FULL_BIAS_PREFERENCE = 4.0


def palette_variant(base: str, intent: LightIntent) -> str:
    return base + BREAKDOWN_SUFFIX if intent == LightIntent.BREAKDOWN else base


class PalettePolicy:
    def __init__(self, palettes: Sequence[Palette], rng: random.Random):
        if len(palettes) < 2:
            raise ValueError('palette rotation needs at least two palettes')
        self._palettes = tuple(palettes)
        self._rng = rng
        self._tracks = 0
        self._track_used: dict[str, int] = {}
        self._track_palette: str | None = None

    @property
    def track_palette(self) -> str | None:
        return self._track_palette

    def palette_for(self, intent: LightIntent, *, new_track: bool, temperature_bias: float) -> str:
        if new_track or self._track_palette is None:
            self._start_track(temperature_bias)
        return self.variant_for(intent)

    def variant_for(self, intent: LightIntent) -> str:
        if self._track_palette is None:
            raise ValueError('no track palette has been chosen yet')
        return palette_variant(self._track_palette, intent)

    def _start_track(self, temperature_bias: float) -> None:
        candidates = [palette for palette in self._palettes if palette.name != self._track_palette]
        weights = [
            self._tracks_since_used(palette.name)
            * FULL_BIAS_PREFERENCE ** (temperature_bias * TEMPERATURE_SIGN[palette.temperature])
            for palette in candidates
        ]
        chosen = self._rng.choices(candidates, weights)[0].name
        self._tracks += 1
        self._track_used[chosen] = self._tracks
        self._track_palette = chosen

    def _tracks_since_used(self, name: str) -> int:
        return self._tracks + 1 - self._track_used.get(name, 0)
