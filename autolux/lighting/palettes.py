from __future__ import annotations

import re
from dataclasses import dataclass, replace

Rgb = tuple[float, float, float]

TEMPERATURES = ('warm', 'neutral', 'cold')
SLOTS = ('primary', 'secondary', 'accent', 'cold', 'black')
BREAKDOWN_SUFFIX = '_breakdown'
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


def _breakdown(palette: Palette, *, primary: Rgb, accent: Rgb) -> Palette:
    return replace(palette, name=palette.name + BREAKDOWN_SUFFIX, primary=primary, accent=accent)


_TECHNO, _HOUSE, _ICE, _ACID, _SUNSET, _UV, _EMBER, _MONO_COLD, _MONO_WARM = PALETTES
BREAKDOWN_PALETTES: tuple[Palette, ...] = (
    _breakdown(_TECHNO, primary=(1.0, 0.9, 0.8), accent=(0.7, 0.15, 0.3)),
    _breakdown(_HOUSE, primary=(1.0, 0.9, 0.8), accent=(0.85, 0.5, 0.45)),
    _breakdown(_ICE, primary=(0.55, 0.7, 1.0), accent=(0.8, 0.9, 1.0)),
    _breakdown(_ACID, primary=(0.75, 1.0, 0.5), accent=(0.8, 0.95, 0.9)),
    _breakdown(_SUNSET, primary=(1.0, 0.65, 0.45), accent=(0.85, 0.45, 0.75)),
    _breakdown(_UV, primary=(0.65, 0.45, 1.0), accent=(0.35, 0.35, 0.95)),
    _breakdown(_EMBER, primary=(1.0, 0.4, 0.3), accent=(0.55, 0.4, 0.2)),
    _breakdown(_MONO_COLD, primary=(0.94, 0.97, 1.0), accent=(0.4, 0.44, 0.5)),
    _breakdown(_MONO_WARM, primary=(1.0, 0.9, 0.8), accent=(0.55, 0.47, 0.4)),
)


def palette_table() -> tuple[Palette, ...]:
    return tuple(
        variant
        for palette, breakdown in zip(PALETTES, BREAKDOWN_PALETTES, strict=True)
        for variant in (palette, breakdown)
    )


def palette_records() -> list[dict]:
    return [
        {'name': palette.name}
        | {
            slot: {'red': red, 'green': green, 'blue': blue}
            for slot, (red, green, blue) in palette.slots().items()
        }
        for palette in palette_table()
    ]
