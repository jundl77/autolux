from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, replace

Rgb = tuple[float, float, float]

TEMPERATURES = ('warm', 'neutral', 'cold')
SLOTS = ('primary', 'secondary', 'accent', 'cold', 'black')
BREAKDOWN_SUFFIX = '_breakdown'
BREAKDOWN_ACCENT_SATURATION = 0.5
BREAKDOWN_PRIMARY_LEVEL = 0.6
HALF_TURN = 0.5
PALETTE_NAME = re.compile(r'[a-z0-9_]{1,64}')
BLACK: Rgb = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Palette:
    name: str
    temperature: str
    primary: Rgb
    secondary: Rgb
    accent: Rgb
    cold: Rgb
    black: Rgb = BLACK

    def __post_init__(self):
        if PALETTE_NAME.fullmatch(self.name) is None:
            raise ValueError(f'palette name {self.name!r} must be lowercase letters, digits or _')
        if self.temperature not in TEMPERATURES:
            raise ValueError(f'palette {self.name} temperature must be one of {TEMPERATURES}')
        for slot in SLOTS:
            colour = getattr(self, slot)
            if len(colour) != 3 or not all(0.0 <= channel <= 1.0 for channel in colour):
                raise ValueError(f'palette {self.name} {slot} must be three channels in 0..1')

    def slots(self) -> dict[str, Rgb]:
        return {slot: getattr(self, slot) for slot in SLOTS}


def breakdown_variant(palette: Palette) -> Palette:
    hue, saturation, value = colorsys.rgb_to_hsv(*palette.accent)
    complement = colorsys.hsv_to_rgb(
        (hue + HALF_TURN) % 1.0, saturation * BREAKDOWN_ACCENT_SATURATION, value
    )
    return replace(
        palette,
        name=palette.name + BREAKDOWN_SUFFIX,
        accent=tuple(round(channel, 6) for channel in complement),
        primary=tuple(round(channel * BREAKDOWN_PRIMARY_LEVEL, 6) for channel in palette.primary),
    )


PALETTES: tuple[Palette, ...] = (
    Palette(
        'techno',
        'warm',
        primary=(1.0, 0.82, 0.6),
        secondary=(1.0, 0.55, 0.1),
        accent=(0.75, 0.0, 0.02),
        cold=(0.75, 0.85, 1.0),
    ),
    Palette(
        'house',
        'neutral',
        primary=(1.0, 0.82, 0.6),
        secondary=(1.0, 0.0, 0.7),
        accent=(1.0, 0.4, 0.0),
        cold=(0.0, 0.85, 1.0),
    ),
    Palette(
        'ice',
        'cold',
        primary=(0.25, 0.5, 1.0),
        secondary=(0.0, 0.85, 1.0),
        accent=(1.0, 1.0, 1.0),
        cold=(0.85, 0.92, 1.0),
    ),
    Palette(
        'acid',
        'neutral',
        primary=(0.6, 1.0, 0.0),
        secondary=(1.0, 0.0, 0.75),
        accent=(1.0, 1.0, 1.0),
        cold=(0.8, 0.9, 1.0),
    ),
    Palette(
        'sunset',
        'warm',
        primary=(1.0, 0.45, 0.1),
        secondary=(0.55, 0.1, 1.0),
        accent=(1.0, 0.3, 0.6),
        cold=(0.7, 0.6, 1.0),
    ),
    Palette(
        'uv',
        'cold',
        primary=(0.5, 0.0, 1.0),
        secondary=(0.9, 0.9, 1.0),
        accent=(0.15, 0.1, 1.0),
        cold=(0.7, 0.6, 1.0),
    ),
    Palette(
        'ember',
        'warm',
        primary=(1.0, 0.15, 0.02),
        secondary=(1.0, 0.5, 0.0),
        accent=(0.6, 0.4, 0.0),
        cold=(1.0, 0.7, 0.4),
    ),
    Palette(
        'mono_cold',
        'cold',
        primary=(0.9, 0.95, 1.0),
        secondary=(0.6, 0.65, 0.7),
        accent=(0.35, 0.38, 0.42),
        cold=(0.75, 0.85, 1.0),
    ),
    Palette(
        'mono_warm',
        'warm',
        primary=(1.0, 0.82, 0.6),
        secondary=(1.0, 0.7, 0.45),
        accent=(0.6, 0.45, 0.3),
        cold=(1.0, 0.9, 0.8),
    ),
)
DEFAULT_PALETTE = PALETTES[0].name
PALETTE_NAMES = tuple(palette.name for palette in PALETTES)


def palette_table(palettes: tuple[Palette, ...] = PALETTES) -> tuple[Palette, ...]:
    return tuple(
        variant for palette in palettes for variant in (palette, breakdown_variant(palette))
    )
