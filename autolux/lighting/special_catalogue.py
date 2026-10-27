import math
import re
import tomllib
from pathlib import Path

from autolux.lib.engine.effect_definitions import STARFIRE_MAX_STROBE_HZ, LightIntent
from autolux.lib.engine.special_policy import Special, Venue
from autolux.lighting.mvr_schema import DEPTHS, ROLES, ZONES
from autolux.lighting.palettes import SLOTS

SPECIAL_DIRECTORY = Path(__file__).resolve().parents[2] / 'show' / 'specials'
# The room's limits across all specials; every other file is one special.
VENUE_FILE = 'venue.toml'
NAME = re.compile(r'[a-z][a-z0-9_]*')
# The one place the file's console words meet the wire fields.
FILE_FIELDS = {
    ('name',): 'name',
    ('intents',): 'intents',
    ('group',): 'group',
    ('role',): 'role',
    ('zone',): 'zone',
    ('depth',): 'depth',
    ('preset', 'colour'): 'colour_slot',
    ('effect', 'rate'): 'speed',
    ('effect', 'size'): 'energy',
    ('effect', 'form', 'pulse_beats'): 'pulse_beats',
    ('effect', 'form', 'strobe_hz'): 'strobe_hz',
    ('effect', 'pattern'): 'pattern',
    ('playback', 'attack_beats'): 'attack_beats',
    ('playback', 'release_beats'): 'release_beats',
    ('playback', 'cooldown_seconds'): 'cooldown_seconds',
}
FILE_NAMES = {field: '.'.join(path) for path, field in FILE_FIELDS.items()}
SECTIONS = {path[:depth] for path in FILE_FIELDS for depth in range(1, len(path))}
SPECIAL_KEYS = frozenset(
    {'name', 'intents', 'colour_slot', 'energy', 'speed', 'attack_beats', 'release_beats'}
)
STROBE_KEYS = frozenset({'name', 'intents', 'group', 'strobe_hz'})
# A special row plays on one named group or on every group of a role; the strobe on a group.
TARGET_KEYS = {SPECIAL_KEYS: ('group', 'role'), STROBE_KEYS: ()}
OPTIONAL_KEYS = {
    SPECIAL_KEYS: {'pulse_beats', 'pattern', 'cooldown_seconds', 'group', 'role', 'zone', 'depth'},
    STROBE_KEYS: set(),
}


class SpecialError(ValueError):
    pass


def file_fields(document: dict) -> dict:
    """Flatten a special file into wire fields, naming an unknown key by its file path."""
    fields, unknown = {}, []

    def walk(table: dict, path: tuple) -> None:
        for key, value in table.items():
            here = (*path, key)
            if here in FILE_FIELDS:
                fields[FILE_FIELDS[here]] = value
            elif here in SECTIONS and isinstance(value, dict):
                walk(value, here)
            else:
                unknown.append('.'.join(here))

    walk(document, ())
    if unknown:
        raise SpecialError(f'unknown {", ".join(sorted(unknown))}')
    return fields


def _names(keys, labels: dict) -> str:
    return ', '.join(sorted(labels.get(key, key) for key in keys))


def _number(data: dict, key: str, low: float, high: float, labels: dict) -> float:
    value = data[key]
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise SpecialError(
            f'{labels.get(key, key)} {value!r} must be a number within {low:g}..{high:g}'
        )
    return float(value)


def _optional(data: dict, key: str, parse, labels: dict):
    return None if data.get(key) is None else parse(data, key, labels)


def _pulse(data: dict, key: str, labels: dict) -> int:
    value = data[key]
    if type(value) is not int or value < 1:
        raise SpecialError(
            f'{labels.get(key, key)} {value!r} must be a whole number of beats, at least 1'
        )
    return value


def _pattern(data: dict, key: str, labels: dict) -> float:
    return _number(data, key, 0.0, 1.0, labels)


def _intents(values) -> tuple[LightIntent, ...]:
    known = [intent.value for intent in LightIntent]
    if not isinstance(values, list) or not values:
        raise SpecialError('intents must be a non-empty list')
    for value in values:
        if value not in known:
            raise SpecialError(f'intent {value!r} is not one of {", ".join(known)}')
    if len(set(values)) != len(values):
        raise SpecialError('intents lists an intent twice')
    return tuple(LightIntent(value) for value in values)


def parse_special(document, stem: str) -> Special:
    if not isinstance(document, dict):
        raise SpecialError('a special must be a TOML table')
    data, labels = file_fields(document), FILE_NAMES
    shape = STROBE_KEYS if 'strobe_hz' in data else SPECIAL_KEYS
    missing = shape - data.keys()
    if missing:
        raise SpecialError(f'missing {_names(missing, labels)}')
    unknown = data.keys() - shape - OPTIONAL_KEYS[shape]
    if unknown:
        raise SpecialError(f'unknown {_names(unknown, labels)}')
    if data['name'] != stem:
        raise SpecialError(f'name {data["name"]!r} must match the file name {stem!r}')
    if TARGET_KEYS[shape] and len(data.keys() & set(TARGET_KEYS[shape])) != 1:
        raise SpecialError('give exactly one of group, role')
    common = {'name': stem, 'intents': _intents(data['intents'])}
    if 'role' in data:
        if data['role'] not in ROLES:
            raise SpecialError(f'role {data["role"]!r} is not one of {", ".join(ROLES)}')
        common['role'] = data['role']
        for key, names in (('zone', ZONES), ('depth', DEPTHS)):
            if key in data:
                if data[key] not in names:
                    raise SpecialError(f'{key} {data[key]!r} is not one of {", ".join(names)}')
                common[key] = data[key]
    elif data.keys() & {'zone', 'depth'}:
        raise SpecialError('zone and depth narrow a role row; a group row names its group')
    else:
        group = data['group']
        if not isinstance(group, str) or not NAME.fullmatch(group):
            raise SpecialError(f'group {group!r} must be a lower-case rig group name')
        common['group'] = group
    if shape is STROBE_KEYS:
        strobe_hz = _number(data, 'strobe_hz', 0.0, STARFIRE_MAX_STROBE_HZ, labels)
        return Special(**common, strobe_hz=strobe_hz)
    cooldown = (
        _number(data, 'cooldown_seconds', 0.0, math.inf, labels)
        if 'cooldown_seconds' in data
        else 0.0
    )
    return Special(**common, **_special_values(data, labels), cooldown_seconds=cooldown)


def _special_values(data: dict, labels: dict | None = None) -> dict:
    """Validate wire fields; labels name each field the way its source wrote it."""
    labels = labels or {}
    if data['colour_slot'] not in SLOTS:
        raise SpecialError(
            f'{labels.get("colour_slot", "colour_slot")} {data["colour_slot"]!r} '
            f'is not one of {", ".join(SLOTS)}'
        )
    return {
        'colour_slot': data['colour_slot'],
        'energy': _number(data, 'energy', 0.0, 1.0, labels),
        'speed': _number(data, 'speed', 0.0, 1.0, labels),
        'pulse_beats': _optional(data, 'pulse_beats', _pulse, labels),
        'attack_beats': _number(data, 'attack_beats', 0.0, math.inf, labels),
        'release_beats': _number(data, 'release_beats', 0.0, math.inf, labels),
        'pattern': _optional(data, 'pattern', _pattern, labels),
    }


def discover_specials(directory: Path = SPECIAL_DIRECTORY) -> tuple[Special, ...]:
    specials, problems = [], []
    for path in sorted(Path(directory).glob('*.toml')):
        if path.name == VENUE_FILE:
            continue
        try:
            document = tomllib.loads(path.read_text(encoding='utf-8'))
            specials.append(parse_special(document, path.stem))
        except tomllib.TOMLDecodeError as error:
            problems.append(f'{path.name}: not TOML ({error})')
        except SpecialError as error:
            problems.append(f'{path.name}: {error}')
    if problems:
        raise SpecialError('invalid specials: ' + '; '.join(problems))
    return tuple(specials)


def read_venue(directory: Path = SPECIAL_DIRECTORY) -> Venue:
    """The `[venue]` table of venue.toml; an absent file or key keeps the default."""
    path = Path(directory) / VENUE_FILE
    if not path.is_file():
        return Venue()
    try:
        document = tomllib.loads(path.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as error:
        raise SpecialError(f'{VENUE_FILE}: not TOML ({error})') from error
    table = document.get('venue', {})
    if not isinstance(table, dict):
        raise SpecialError(f'{VENUE_FILE}: [venue] must be a table')
    unknown = sorted(document.keys() - {'venue'}) + sorted(
        f'venue.{key}' for key in table.keys() - {'max_accent_events_per_minute'}
    )
    if unknown:
        raise SpecialError(f'{VENUE_FILE}: unknown {", ".join(unknown)}')
    cap = table.get('max_accent_events_per_minute', Venue.max_accent_events_per_minute)
    if type(cap) is not int or cap < 1:
        raise SpecialError(
            f'{VENUE_FILE}: venue.max_accent_events_per_minute {cap!r} must be a whole number, '
            'at least 1'
        )
    return Venue(max_accent_events_per_minute=cap)
