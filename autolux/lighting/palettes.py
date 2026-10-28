from __future__ import annotations

import colorsys
import math
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

Rgb = tuple[float, float, float]

SLOTS = ('primary', 'secondary', 'accent', 'cold', 'black')
BREAKDOWN_SUFFIX = '_breakdown'
DROP_SUFFIX = '_drop'
MONO_SUFFIX = '_mono'
# A mono cold is primary held low, so builds and breakdowns keep white back for the hit.
MONO_COLD_LEVEL = 0.4
PALETTE_NAME = re.compile(r'[a-z0-9_]{1,64}')
BLACK: Rgb = (0.0, 0.0, 0.0)
COLOUR_SHARES = (('primary', 0.5), ('secondary', 0.3), ('accent', 0.2))
WARMEST_HUE_DEGREES = 30.0
PALETTE_DIRECTORY = Path(__file__).resolve().parents[2] / 'show' / 'palettes'
HEX_COLOUR = re.compile(r'#[0-9a-fA-F]{6}')


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
    uv -0.42/-0.8/+0.3, ember +0.87/-0.6/-0.2, mono_cold -0.09/+0.6/+0.5,
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
        self.mood  # noqa: B018

    @property
    def mood(self) -> Mood:
        return Mood(colour_temperature(self.slots()), self.weight, self.drive)

    def slots(self) -> dict[str, Rgb]:
        return {slot: getattr(self, slot) for slot in SLOTS}


class PaletteError(ValueError):
    pass


def _rgb(text, where: str) -> Rgb:
    if not isinstance(text, str) or HEX_COLOUR.fullmatch(text) is None:
        raise PaletteError(f'{where} {text!r} must be a #rrggbb colour')
    return tuple(int(text[i : i + 2], 16) / 255 for i in (1, 3, 5))


def _colours(table, section: str, required: tuple[str, ...]) -> dict[str, Rgb]:
    if not isinstance(table, dict):
        raise PaletteError(f'[{section}] must be a table')
    unknown = sorted(table.keys() - set(SLOTS))
    if unknown:
        raise PaletteError('unknown ' + ', '.join(f'{section}.{key}' for key in unknown))
    missing = [slot for slot in required if slot not in table]
    if missing:
        raise PaletteError('missing ' + ', '.join(f'{section}.{slot}' for slot in missing))
    return {slot: _rgb(value, f'{section}.{slot}') for slot, value in table.items()}


def parse_palette(document: dict, name: str) -> tuple[int, Palette, Palette, Palette]:
    """One file: its console number, the palette and its breakdown and drop variants."""
    unknown = sorted(document.keys() - {'number', 'slots', 'mood', 'breakdown', 'drop'})
    if unknown:
        raise PaletteError(f'unknown {", ".join(unknown)}')
    number = document.get('number')
    if type(number) is not int or number < 1:
        raise PaletteError(f'number {number!r} must be a whole number, at least 1')
    mood = document.get('mood')
    if not isinstance(mood, dict) or set(mood) != {'weight', 'drive'}:
        raise PaletteError('[mood] sets weight and drive; temperature comes from the colours')
    for axis, value in mood.items():
        if type(value) not in (int, float) or not -1.0 <= value <= 1.0:
            raise PaletteError(f'mood.{axis} {value!r} must be a number within -1..1')
    slots = _colours(document.get('slots'), 'slots', SLOTS)
    palette = Palette(name, weight=float(mood['weight']), drive=float(mood['drive']), **slots)
    breakdown, drop = (
        replace(palette, name=name + suffix, **_colours(document.get(section, {}), section, ()))
        for section, suffix in (('breakdown', BREAKDOWN_SUFFIX), ('drop', DROP_SUFFIX))
    )
    return number, palette, breakdown, drop


def load_palettes(directory: Path = PALETTE_DIRECTORY) -> tuple[tuple[Palette, ...], ...]:
    """Base, breakdown and drop palettes, each in console-number order."""
    rows, problems = [], []
    for path in sorted(Path(directory).glob('*.toml')):
        try:
            rows.append(parse_palette(tomllib.loads(path.read_text(encoding='utf-8')), path.stem))
        except tomllib.TOMLDecodeError as error:
            problems.append(f'{path.name}: not TOML ({error})')
        except ValueError as error:
            problems.append(f'{path.name}: {error}')
    numbers = [row[0] for row in rows]
    if len(set(numbers)) != len(numbers):
        problems.append('two palettes share a number')
    if not rows and not problems:
        problems.append(f'no palettes in {directory}')
    if problems:
        raise PaletteError('invalid palettes: ' + '; '.join(problems))
    rows.sort(key=lambda row: row[0])
    return tuple(tuple(row[column] for row in rows) for column in (1, 2, 3))


def mono(palette: Palette) -> Palette:
    """Colour A alone: secondary and accent take primary, cold is primary held low."""
    cold = tuple(channel * MONO_COLD_LEVEL for channel in palette.primary)
    return replace(
        palette,
        name=palette.name + MONO_SUFFIX,
        secondary=palette.primary,
        accent=palette.primary,
        cold=cold,
    )


PALETTES, BREAKDOWN_PALETTES, DROP_PALETTES = load_palettes()
MONO_PALETTES = tuple(mono(palette) for palette in PALETTES)
BREAKDOWN_MONO_PALETTES = tuple(mono(palette) for palette in BREAKDOWN_PALETTES)
DEFAULT_PALETTE = PALETTES[0].name
PALETTE_NAMES = tuple(palette.name for palette in PALETTES)
_FAMILIES = tuple(
    zip(
        PALETTES,
        BREAKDOWN_PALETTES,
        DROP_PALETTES,
        MONO_PALETTES,
        BREAKDOWN_MONO_PALETTES,
        strict=True,
    )
)


def palette_table() -> tuple[Palette, ...]:
    """The Login table: each base palette followed by its variants, the default first."""
    return tuple(variant for family in _FAMILIES for variant in family)


VARIANT_BASES: dict[str, str] = {
    variant.name: family[0].name for family in _FAMILIES for variant in family
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


# Starfire's wheel colour (gdtf.rs cie_to_rgb): CIE xyY to clamped sRGB, then peak-normalised.
XYZ_TO_SRGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.969266, 1.8760108, 0.041556),
    (0.0556434, -0.2040259, 1.0572252),
)


def _normalised(colour) -> Rgb:
    peak = max(colour)
    return tuple(channel / peak for channel in colour) if peak > 0 else BLACK


def _slot_rgb(colour) -> Rgb:
    luminance = colour.luminance / 100
    x, y = colour.x, colour.y
    xyz = (x * luminance / y, luminance, (1 - x - y) * luminance / y)
    linear = [min(1.0, max(0.0, sum(w * v for w, v in zip(row, xyz)))) for row in XYZ_TO_SRGB]
    return _normalised(
        [c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in linear]
    )


def _wheel_slots(mode) -> list[tuple[str, Rgb]]:
    """The slots of a compiled mode whose colour is only a wheel, as Starfire picks among them:
    the lowest-DMX unconditional colour-wheel function's sets. Empty if the mode mixes colour."""
    functions = [function for channel in mode.channels for function in channel.functions]
    if any(f.attribute.startswith(('ColorAdd_', 'ColorSub_')) for f in functions):
        return []
    for channel in mode.channels:
        ordered = sorted(channel.functions, key=lambda function: function.dmx_from)
        for index, function in enumerate(ordered):
            coloured = [s for s in function.sets if s.HasField('wheel_slot_colour')]
            if not function.attribute.startswith('Color') or not coloured:
                continue
            if function.HasField('mode_master'):
                continue
            end = next(
                (f.dmx_from - 1 for f in ordered[index + 1 :] if f.dmx_from > function.dmx_from),
                None,
            )
            return [
                (s.name, _slot_rgb(s.wheel_slot_colour))
                for s in coloured
                if end is None or s.dmx_from <= end
            ]
    return []


def _nearest_slot(slots: list[tuple[str, Rgb]], colour: Rgb) -> str:
    requested = _normalised(colour)
    # Starfire keeps the highest-DMX slot of equally near ones.
    return min(
        reversed(slots),
        key=lambda slot: sum((a - b) ** 2 for a, b in zip(slot[1], requested, strict=True)),
    )[0]


def wheel_collisions(rig, palettes: tuple[Palette, ...] | None = None) -> list[str]:
    """Where a patched wheel-only fixture type would show a palette's primary and secondary as
    one slot, so colour A and colour B look the same on it. Mono palettes mean to; skipped."""
    palettes = PALETTES + BREAKDOWN_PALETTES + DROP_PALETTES if palettes is None else palettes
    patched = {fixture.profile_id for fixture in rig.fixtures}
    collisions = []
    for profile in rig.profiles:
        slots = _wheel_slots(profile.mode) if profile.profile_id in patched else []
        for palette in palettes if slots else ():
            primary, secondary = (
                _nearest_slot(slots, colour) for colour in (palette.primary, palette.secondary)
            )
            if primary == secondary:
                collisions.append(
                    f'{profile.profile_id}: palette {palette.name} shows primary and secondary '
                    f'as the one wheel slot {primary!r}.'
                )
    return collisions
