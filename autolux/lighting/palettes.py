from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass, replace

Rgb = tuple[float, float, float]

SLOTS = ('primary', 'secondary', 'accent', 'cold', 'black')
BREAKDOWN_SUFFIX = '_breakdown'
DROP_SUFFIX = '_drop'
PALETTE_NAME = re.compile(r'[a-z0-9_]{1,64}')
BLACK: Rgb = (0.0, 0.0, 0.0)
COLOUR_SHARES = (('primary', 0.5), ('secondary', 0.3), ('accent', 0.2))
WARMEST_HUE_DEGREES = 30.0


@dataclass(frozen=True)
class Mood:
    """Where a palette sits, and where a measured track sits; each axis spans -1..1.

    temperature: cold -1 .. warm +1. A palette's colour cast, derived from its colours by
        colour_temperature; a track's bass-over-treble balance.
    weight: dark -1 .. bright +1. How much light a palette gives, hand-set and kept in the
        order of its colours' luminance; a track's loudness and spectral brightness.
    drive: laid-back -1 .. hard +1. How pushed a palette feels, hand-set; a track's tempo
        and energy.

    The shipped palettes (temperature / weight / drive): techno +0.60/+0.2/+0.9,
    house +0.49/+0.3/+0.4, ice -0.65/+0.4/0, acid +0.37/+0.7/+0.8, sunset +0.39/-0.3/-0.8,
    uv -0.43/-0.8/+0.3, ember +0.87/-0.6/-0.2, mono_cold -0.09/+0.6/+0.5,
    mono_warm +0.42/+0.5/-0.6.
    """

    temperature: float
    weight: float
    drive: float

    def __post_init__(self):
        for axis, value in zip(('temperature', 'weight', 'drive'), self.axes(), strict=True):
            if not -1.0 <= value <= 1.0:
                raise ValueError(f'mood {axis} must be within -1..1')

    def axes(self) -> tuple[float, float, float]:
        return self.temperature, self.weight, self.drive


def colour_temperature(colours: dict[str, Rgb]) -> float:
    """Hue warmth (cos of the distance from orange, 30 deg) x chroma, averaged by slot share.

    Chroma scales each slot's say, so near-grey palettes come out mild rather than extreme.
    """
    warmth = sum(
        share * saturation * value * math.cos(math.radians(hue * 360.0 - WARMEST_HUE_DEGREES))
        for slot, share in COLOUR_SHARES
        for hue, saturation, value in [colorsys.rgb_to_hsv(*colours[slot])]
    )
    return round(warmth / sum(share for _, share in COLOUR_SHARES), 2)


def luminance(colours: dict[str, Rgb]) -> float:
    return sum(
        share * (0.2126 * red + 0.7152 * green + 0.0722 * blue)
        for slot, share in COLOUR_SHARES
        for red, green, blue in [colours[slot]]
    )


@dataclass(frozen=True)
class Palette:
    name: str
    weight: float
    drive: float
    primary: Rgb
    secondary: Rgb
    accent: Rgb
    cold: Rgb
    black: Rgb = BLACK

    def __post_init__(self):
        if PALETTE_NAME.fullmatch(self.name) is None:
            raise ValueError(f'palette name {self.name!r} must be lowercase letters, digits or _')
        for slot in SLOTS:
            colour = getattr(self, slot)
            if len(colour) != 3 or not all(0.0 <= channel <= 1.0 for channel in colour):
                raise ValueError(f'palette {self.name} {slot} must be three channels in 0..1')
        self.mood

    @property
    def mood(self) -> Mood:
        return Mood(colour_temperature(self.slots()), self.weight, self.drive)

    def slots(self) -> dict[str, Rgb]:
        return {slot: getattr(self, slot) for slot in SLOTS}


PALETTES: tuple[Palette, ...] = (
    Palette(
        'techno',
        weight=0.2,
        drive=0.9,
        primary=(1.0, 0.82, 0.6),
        secondary=(1.0, 0.55, 0.1),
        accent=(0.75, 0.0, 0.02),
        cold=(0.75, 0.85, 1.0),
    ),
    Palette(
        'house',
        weight=0.3,
        drive=0.4,
        primary=(1.0, 0.82, 0.6),
        secondary=(1.0, 0.0, 0.7),
        accent=(1.0, 0.4, 0.0),
        cold=(0.0, 0.85, 1.0),
    ),
    Palette(
        'ice',
        weight=0.4,
        drive=0.0,
        primary=(0.25, 0.5, 1.0),
        secondary=(0.0, 0.85, 1.0),
        accent=(1.0, 1.0, 1.0),
        cold=(0.85, 0.92, 1.0),
    ),
    Palette(
        'acid',
        weight=0.7,
        drive=0.8,
        primary=(0.6, 1.0, 0.0),
        secondary=(1.0, 0.0, 0.75),
        accent=(1.0, 1.0, 1.0),
        cold=(0.8, 0.9, 1.0),
    ),
    Palette(
        'sunset',
        weight=-0.3,
        drive=-0.8,
        primary=(1.0, 0.45, 0.1),
        secondary=(0.55, 0.1, 1.0),
        accent=(1.0, 0.3, 0.6),
        cold=(0.7, 0.6, 1.0),
    ),
    Palette(
        'uv',
        weight=-0.8,
        drive=0.3,
        primary=(0.5, 0.0, 1.0),
        secondary=(0.9, 0.9, 1.0),
        accent=(0.15, 0.1, 1.0),
        cold=(0.7, 0.6, 1.0),
    ),
    Palette(
        'ember',
        weight=-0.6,
        drive=-0.2,
        primary=(1.0, 0.15, 0.02),
        secondary=(1.0, 0.5, 0.0),
        accent=(0.6, 0.4, 0.0),
        cold=(1.0, 0.7, 0.4),
    ),
    Palette(
        'mono_cold',
        weight=0.6,
        drive=0.5,
        primary=(0.9, 0.95, 1.0),
        secondary=(0.6, 0.65, 0.7),
        accent=(0.35, 0.38, 0.42),
        cold=(0.75, 0.85, 1.0),
    ),
    Palette(
        'mono_warm',
        weight=0.5,
        drive=-0.6,
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


def _drop(palette: Palette, *, accent: Rgb) -> Palette:
    return replace(palette, name=palette.name + DROP_SUFFIX, accent=accent)


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
DROP_PALETTES: tuple[Palette, ...] = (
    _drop(_TECHNO, accent=(1.0, 0.0, 0.03)),
    _drop(_HOUSE, accent=(1.0, 0.4, 0.0)),
    _drop(_ICE, accent=(0.4, 0.9, 1.0)),
    _drop(_ACID, accent=(0.8, 1.0, 0.3)),
    _drop(_SUNSET, accent=(1.0, 0.05, 0.5)),
    _drop(_UV, accent=(0.05, 0.0, 1.0)),
    _drop(_EMBER, accent=(1.0, 0.667, 0.0)),
    _drop(_MONO_COLD, accent=(0.3, 0.45, 0.7)),
    _drop(_MONO_WARM, accent=(0.9, 0.55, 0.2)),
)


def palette_table() -> tuple[Palette, ...]:
    return tuple(
        variant
        for palette, breakdown, drop in zip(
            PALETTES, BREAKDOWN_PALETTES, DROP_PALETTES, strict=True
        )
        for variant in (palette, breakdown, drop)
    )


VARIANT_BASES: dict[str, str] = {
    variant.name: palette.name
    for palette, breakdown, drop in zip(PALETTES, BREAKDOWN_PALETTES, DROP_PALETTES, strict=True)
    for variant in (palette, breakdown, drop)
}


def base_palette_name(name: str) -> str:
    return VARIANT_BASES.get(name, name)


def palette_records() -> list[dict]:
    return [
        {'name': palette.name}
        | {
            slot: {'red': red, 'green': green, 'blue': blue}
            for slot, (red, green, blue) in palette.slots().items()
        }
        for palette in palette_table()
    ]
