import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from enum import Enum


class LightIntent(Enum):
    ATMOSPHERIC = 'atmospheric'
    BREAKDOWN = 'breakdown'
    BUILDUP = 'buildup'
    DROP = 'drop'


# Nine musical sections share four lighting families.
SECTION_CLASS_INTENTS: dict[str, LightIntent] = {
    'intro': LightIntent.ATMOSPHERIC,
    'altintro': LightIntent.ATMOSPHERIC,
    'buildup': LightIntent.BUILDUP,
    'breakdown': LightIntent.BREAKDOWN,
    'bridge': LightIntent.BREAKDOWN,
    'drop': LightIntent.DROP,
    'cooldown': LightIntent.BREAKDOWN,
    'outro': LightIntent.ATMOSPHERIC,
    'altoutro': LightIntent.ATMOSPHERIC,
}

PREFERRED_AUTOLOOPS: dict[LightIntent, str] = {
    LightIntent.ATMOSPHERIC: 'cold_breathe',
    LightIntent.BREAKDOWN: 'wide_space',
    LightIntent.BUILDUP: 'ramp',
    LightIntent.DROP: 'kick',
}

AutoloopTable = dict[LightIntent, tuple[tuple[str, str], ...]]

STANDBY_AUTOLOOP = ('standby', 'warm_white')
STANDBY_CROSSFADE_SECONDS = 1.0

DEFAULT_AUTOLOOPS: AutoloopTable = {
    intent: ((intent.value, autoloop),) for intent, autoloop in PREFERRED_AUTOLOOPS.items()
}

AUTOLOOP_META_VALUES: dict[str, tuple[str, ...]] = {
    'energy': ('1', '2', '3'),
    'family': ('cold', 'warm', 'accent'),
    'movement': ('still', 'sweep', 'snap'),
}
FAMILIES = AUTOLOOP_META_VALUES['family']


@dataclass(frozen=True)
class AutoloopMeta:
    energy: int
    family: str
    movement: str

    def __post_init__(self):
        if type(self.energy) is not int:
            raise ValueError(f'energy {self.energy!r} must be an integer')
        for name, allowed in AUTOLOOP_META_VALUES.items():
            if str(getattr(self, name)) not in allowed:
                raise ValueError(
                    f'{name} {getattr(self, name)!r} is not one of {", ".join(allowed)}'
                )


AutoloopMetaTable = Mapping[tuple[str, str], AutoloopMeta]

DEFAULT_AUTOLOOP_META: AutoloopMetaTable = {
    ('atmospheric', 'cold_breathe'): AutoloopMeta(1, 'cold', 'sweep'),
    ('breakdown', 'wide_space'): AutoloopMeta(1, 'accent', 'sweep'),
    ('buildup', 'ramp'): AutoloopMeta(2, 'warm', 'sweep'),
    ('drop', 'kick'): AutoloopMeta(3, 'warm', 'snap'),
}


QUIET_RMS = 0.1
QUIET_HOLD_SECONDS = 2.0
QUIET_BRIGHTNESS = 0.4
BEAT_LOSS_SECONDS = 2.5

STROBE_BURST_BEATS = 8
STROBE_MAX_BURST_SECONDS = 4.0
STROBE_RATE_HZ = 8.0
STARFIRE_MAX_STROBE_HZ = 10.0
STARFIRE_MAX_STROBE_BURST_SECONDS = 60.0
STROBE_BURST_SECONDS = 1.0
STROBE_COOLDOWN_SECONDS = 60.0
CROSSFADE_BEATS = 1.0
MAX_STROBE_COOLDOWN_SECONDS = 600.0
DIGITAL_SILENCE_SECONDS = 2.0
MAX_DIGITAL_SILENCE_SECONDS = 10.0
MAX_CROSSFADE_BEATS = 16.0
DEFAULT_INTENSITY = 0.7
STROBE_GATE_INTENSITY = 0.3

SHOW_SETTING_RANGES: dict[str, tuple[float, float]] = {
    'drop_strobe_cooldown_seconds': (0.0, MAX_STROBE_COOLDOWN_SECONDS),
    'strobe_burst_seconds': (0.1, STARFIRE_MAX_STROBE_BURST_SECONDS),
    'crossfade_beats': (0.25, MAX_CROSSFADE_BEATS),
    'quiet_brightness': (0.0, 1.0),
    'digital_silence_seconds': (0.5, MAX_DIGITAL_SILENCE_SECONDS),
    'strobe_max_rate_hz': (0.0, STARFIRE_MAX_STROBE_HZ),
    'strobe_max_burst_seconds': (0.1, STARFIRE_MAX_STROBE_BURST_SECONDS),
    'palette_temperature_bias': (-1.0, 1.0),
    'intensity': (0.0, 1.0),
}


@dataclass(frozen=True)
class ShowSettings:
    drop_strobe_cooldown_seconds: float = STROBE_COOLDOWN_SECONDS
    strobe_burst_seconds: float = STROBE_BURST_SECONDS
    crossfade_beats: float = CROSSFADE_BEATS
    quiet_brightness: float = QUIET_BRIGHTNESS
    digital_silence_seconds: float = DIGITAL_SILENCE_SECONDS
    strobe_max_rate_hz: float = STROBE_RATE_HZ
    strobe_max_burst_seconds: float = STROBE_MAX_BURST_SECONDS
    palette_temperature_bias: float = 0.0
    intensity: float = DEFAULT_INTENSITY

    def __post_init__(self):
        for name, (low, high) in SHOW_SETTING_RANGES.items():
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'{name} must be a number')
            if not low <= value <= high:
                raise ValueError(f'{name} must be within {low:g}..{high:g}')
            object.__setattr__(self, name, float(value))

    @classmethod
    def from_dict(cls, data: dict) -> 'ShowSettings':
        if not isinstance(data, dict):
            raise TypeError('settings must be an object')
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f'unknown settings: {", ".join(sorted(unknown))}')
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def transition_for(intent: LightIntent) -> dict | None:
    return {'kind': 'cut'} if intent == LightIntent.DROP else None


def intent_for_class(label: str) -> LightIntent:
    try:
        return SECTION_CLASS_INTENTS[label]
    except KeyError:
        raise KeyError(
            f"the decoder committed {label!r}, which no LightIntent claims; "
            f"known classes are {', '.join(sorted(SECTION_CLASS_INTENTS))}"
        ) from None
