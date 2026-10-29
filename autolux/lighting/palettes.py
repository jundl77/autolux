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
VARIANT_SUFFIXES = ('', BREAKDOWN_SUFFIX, DROP_SUFFIX, MONO_SUFFIX, BREAKDOWN_SUFFIX + MONO_SUFFIX)
# Starfire's palette table limits (starfire/src/palette.rs).
MAX_PALETTES = 64
MAX_PALETTE_NAME_BYTES = 64
MONO_COLD_LEVEL = 0.15
MIN_COLOUR_SATURATION = 0.05
WHITE: Rgb = (1.0, 1.0, 1.0)
COLOUR_ROLES = ('primary', 'secondary', 'accent')
SAME_HUE_DEGREES = 15.0
PALETTE_NAME = re.compile(r'[a-z0-9_]{1,64}')
BLACK: Rgb = (0.0, 0.0, 0.0)
COLOUR_SHARES = (('primary', 0.5), ('secondary', 0.3), ('accent', 0.2))
WARMEST_HUE_DEGREES = 30.0
PALETTE_DIRECTORY = Path(__file__).resolve().parents[2] / 'show' / 'palettes'
HEX_COLOUR = re.compile(r'#[0-9a-fA-F]{6}')


@dataclass(frozen=True)
class Mood:
    # Each axis spans -1..1: cold..warm, dark..bright, laid-back..hard.

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
    colours = {slot: _rgb(value, f'{section}.{slot}') for slot, value in table.items()}
    for slot in COLOUR_ROLES:
        if slot in colours and _saturation(colours[slot]) < MIN_COLOUR_SATURATION:
            raise PaletteError(
                f'{section}.{slot} {table[slot]} is white or grey; white is kept for hits'
            )
    return colours


def _saturation(colour: Rgb) -> float:
    return colorsys.rgb_to_hsv(*colour)[1]


def _hue_degrees(colour: Rgb) -> float:
    return colorsys.rgb_to_hsv(*colour)[0] * 360.0


def parse_palette(document: dict, name: str) -> tuple[int, Palette, Palette, Palette]:
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
    longest = max(VARIANT_SUFFIXES, key=len)
    problems += [
        f'{row[1].name}{longest} is longer than {MAX_PALETTE_NAME_BYTES} bytes'
        for row in rows
        if len((row[1].name + longest).encode()) > MAX_PALETTE_NAME_BYTES
    ]
    if len(rows) * len(VARIANT_SUFFIXES) > MAX_PALETTES:
        problems.append(
            f'{len(rows)} palettes make {len(rows) * len(VARIANT_SUFFIXES)} with their variants; '
            f'Starfire takes at most {MAX_PALETTES}'
        )
    if not rows and not problems:
        problems.append(f'no palettes in {directory}')
    if problems:
        raise PaletteError('invalid palettes: ' + '; '.join(problems))
    rows.sort(key=lambda row: row[0])
    return tuple(tuple(row[column] for row in rows) for column in (1, 2, 3))


def mono(palette: Palette) -> Palette:
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


def _slot_rgb(colour) -> Rgb | None:
    x, y, luminance = colour.x, colour.y, colour.luminance / 100
    if not all(math.isfinite(value) for value in (x, y, luminance)) or y <= 0 or luminance < 0:
        return None
    xyz = (x * luminance / y, luminance, (1 - x - y) * luminance / y)
    linear = [min(1.0, max(0.0, sum(w * v for w, v in zip(row, xyz)))) for row in XYZ_TO_SRGB]
    return _normalised(
        [c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in linear]
    )


def wheel_slots(mode) -> list[list[tuple[str, Rgb]]]:
    functions = [function for channel in mode.channels for function in channel.functions]
    if any(f.attribute.startswith(('ColorAdd_', 'ColorSub_')) for f in functions):
        return []
    wheels = []
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
            slots = [
                (s.name, rgb)
                for s in coloured
                if (end is None or s.dmx_from <= end)
                and (rgb := _slot_rgb(s.wheel_slot_colour)) is not None
            ]
            if slots:
                wheels.append(slots)
            break
    return wheels


def nearest_slot(slots: list[tuple[str, Rgb]], colour: Rgb) -> str:
    requested = _normalised(colour)
    white = _white_slots(slots)
    if requested != WHITE and any(rgb != BLACK and name not in white for name, rgb in slots):
        slots = [slot for slot in slots if slot[0] not in white]
    return min(
        reversed(slots),
        key=lambda slot: sum((a - b) ** 2 for a, b in zip(slot[1], requested, strict=True)),
    )[0]


def _white_slots(slots: list[tuple[str, Rgb]]) -> set[str]:
    return {
        name for name, rgb in slots if max(rgb) > 0 and _saturation(rgb) < MIN_COLOUR_SATURATION
    }


def _same_hue(palette: Palette) -> bool:
    distance = abs(_hue_degrees(palette.primary) - _hue_degrees(palette.secondary))
    return min(distance, 360.0 - distance) <= SAME_HUE_DEGREES


def wheel_warnings(rig, palettes: tuple[Palette, ...] | None = None) -> list[str]:
    palettes = PALETTES + BREAKDOWN_PALETTES + DROP_PALETTES if palettes is None else palettes
    patched = {fixture.profile_id for fixture in rig.fixtures}
    warnings = []
    for profile in rig.profiles:
        wheels = wheel_slots(profile.mode) if profile.profile_id in patched else []
        shared = {}
        for palette in palettes if wheels else ():
            primary, secondary = (
                [nearest_slot(slots, colour) for slots in wheels]
                for colour in (palette.primary, palette.secondary)
            )
            if primary == secondary and not _same_hue(palette):
                shared.setdefault(
                    (base_palette_name(palette.name), palette.primary, palette.secondary),
                    f'{palette.name} ({", ".join(primary)})',
                )
        if shared:
            warnings.append(
                f'{profile.profile_id}: primary and secondary share one wheel slot in '
                + ', '.join(shared.values())
                + '.'
            )
    return warnings
