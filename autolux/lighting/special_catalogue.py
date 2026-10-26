import json
import math
import re
from pathlib import Path

from autolux.lib.engine.effect_definitions import STARFIRE_MAX_STROBE_HZ, LightIntent
from autolux.lib.engine.special_policy import Special
from autolux.lighting.palettes import SLOTS

SPECIAL_DIRECTORY = Path(__file__).resolve().parents[2] / 'specials'
NAME = re.compile(r'[a-z][a-z0-9_]*')
SPECIAL_KEYS = frozenset(
    {'name', 'intents', 'group', 'colour_slot', 'energy', 'speed', 'attack_beats', 'release_beats'}
)
STROBE_KEYS = frozenset({'name', 'intents', 'group', 'strobe_hz'})
OPTIONAL_KEYS = {SPECIAL_KEYS: {'pulse_beats', 'pattern'}, STROBE_KEYS: set()}


class SpecialError(ValueError):
    pass


def _number(data: dict, key: str, low: float, high: float) -> float:
    value = data[key]
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise SpecialError(f'{key} {value!r} must be a number within {low:g}..{high:g}')
    return float(value)


def _optional(data: dict, key: str, parse):
    return None if data.get(key) is None else parse(data, key)


def _pulse(data: dict, key: str) -> int:
    value = data[key]
    if type(value) is not int or value < 1:
        raise SpecialError(f'{key} {value!r} must be a whole number of beats, at least 1')
    return value


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


def parse_special(data, stem: str) -> Special:
    if not isinstance(data, dict):
        raise SpecialError('a special must be a JSON object')
    shape = STROBE_KEYS if 'strobe_hz' in data else SPECIAL_KEYS
    missing = sorted(shape - data.keys())
    if missing:
        raise SpecialError(f'missing {", ".join(missing)}')
    unknown = sorted(data.keys() - shape - OPTIONAL_KEYS[shape])
    if unknown:
        raise SpecialError(f'unknown {", ".join(unknown)}')
    if data['name'] != stem:
        raise SpecialError(f'name {data["name"]!r} must match the file name {stem!r}')
    group = data['group']
    if not isinstance(group, str) or not NAME.fullmatch(group):
        raise SpecialError(f'group {group!r} must be a lower-case rig group name')
    common = {
        'name': stem,
        'intents': _intents(data['intents']),
        'group': group,
    }
    if shape is STROBE_KEYS:
        return Special(**common, strobe_hz=_number(data, 'strobe_hz', 0.0, STARFIRE_MAX_STROBE_HZ))
    return Special(**common, **special_values(data))


def special_values(data: dict) -> dict:
    if data['colour_slot'] not in SLOTS:
        raise SpecialError(f'colour_slot {data["colour_slot"]!r} is not one of {", ".join(SLOTS)}')
    return {
        'colour_slot': data['colour_slot'],
        'energy': _number(data, 'energy', 0.0, 1.0),
        'speed': _number(data, 'speed', 0.0, 1.0),
        'pulse_beats': _optional(data, 'pulse_beats', _pulse),
        'attack_beats': _number(data, 'attack_beats', 0.0, math.inf),
        'release_beats': _number(data, 'release_beats', 0.0, math.inf),
        'pattern': _optional(data, 'pattern', lambda data, key: _number(data, key, 0.0, 1.0)),
    }


def discover_specials(directory: Path = SPECIAL_DIRECTORY) -> tuple[Special, ...]:
    specials, problems = [], []
    for path in sorted(Path(directory).glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            specials.append(parse_special(data, path.stem))
        except json.JSONDecodeError as error:
            problems.append(f'{path.name}: not JSON ({error.msg} at line {error.lineno})')
        except SpecialError as error:
            problems.append(f'{path.name}: {error}')
    if problems:
        raise SpecialError('invalid specials: ' + '; '.join(problems))
    return tuple(specials)
