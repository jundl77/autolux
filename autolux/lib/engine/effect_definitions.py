import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from enum import Enum


class LightIntent(Enum):
    ATMOSPHERIC = 'atmospheric'
    BREAKDOWN = 'breakdown'
    BUILDUP = 'buildup'
    DROP = 'drop'


# Nine musical sections map onto four lighting intents.
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
    LightIntent.ATMOSPHERIC: 'dawn',
    LightIntent.BREAKDOWN: 'drift_apart',
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
    'movement': ('still', 'sweep', 'snap'),
    'texture': ('wash', 'beam', 'pixel', 'mixed'),
}


@dataclass(frozen=True)
class AutoloopMeta:
    energy: int
    movement: str
    texture: str

    def __post_init__(self):
        if type(self.energy) is not int:
            raise ValueError(f'energy {self.energy!r} must be an integer')
        for name, allowed in AUTOLOOP_META_VALUES.items():
            if str(getattr(self, name)) not in allowed:
                raise ValueError(
                    f'{name} {getattr(self, name)!r} is not one of {", ".join(allowed)}'
                )


AutoloopMetaTable = Mapping[tuple[str, str], AutoloopMeta]

# Banks of Lua layers a section cue schedules above its base look.
OVERLAY_BANKS = ('hit', 'fill', 'riser', 'dip', 'accent')

OVERLAY_META_VALUES: dict[str, tuple[str, ...]] = {
    'strobe': ('yes', 'no'),
    'intensity': ('htp', 'ltp'),
}


@dataclass(frozen=True)
class OverlayMeta:
    """strobe: the overlay strobes, so the venue gate counts it as an intense event.
    intensity_htp: its intensity takes the higher of the look below and its own; else it
    replaces the look below (LTP)."""

    strobe: bool
    intensity_htp: bool


OverlayMetaTable = Mapping[tuple[str, str], OverlayMeta]

DEFAULT_AUTOLOOP_META: AutoloopMetaTable = {
    ('atmospheric', 'dawn'): AutoloopMeta(1, 'sweep', 'mixed'),
    ('breakdown', 'drift_apart'): AutoloopMeta(1, 'sweep', 'beam'),
    ('buildup', 'ramp'): AutoloopMeta(2, 'sweep', 'pixel'),
    ('drop', 'kick'): AutoloopMeta(3, 'snap', 'wash'),
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
# The dashboard's three intensity levels. They differ in kind, not just degree: calm
# re-maps drops and builds to quiet banks and drops strobes, wild leans on energy 3 and
# strobes more often. Starfire's pulse/movement curve (starfire::intensity) keys off the
# same three values.
INTENSITY_CALM = 0.3
INTENSITY_BALANCED = 0.7
INTENSITY_WILD = 1.0
DEFAULT_INTENSITY = INTENSITY_BALANCED
CALM, BALANCED, WILD = 'calm', 'balanced', 'wild'
# Strobes (and starfire's intense-special gate) are off at or below calm.
STROBE_GATE_INTENSITY = INTENSITY_CALM
# Calm stretches every look change; wild snaps between them.
CROSSFADE_SCALE = {CALM: 2.0, BALANCED: 1.0, WILD: 0.5}
DROP_STROBE_COOLDOWN_SCALE = {CALM: 1.0, BALANCED: 1.0, WILD: 0.5}


def intensity_level(intensity: float) -> str:
    """Continuous intensities between calm and wild behave as balanced."""
    if intensity <= INTENSITY_CALM:
        return CALM
    if intensity >= INTENSITY_WILD:
        return WILD
    return BALANCED


SHOW_SETTING_RANGES: dict[str, tuple[float, float]] = {
    'drop_strobe_cooldown_seconds': (0.0, MAX_STROBE_COOLDOWN_SECONDS),
    'strobe_burst_seconds': (0.1, STARFIRE_MAX_STROBE_BURST_SECONDS),
    'crossfade_beats': (0.25, MAX_CROSSFADE_BEATS),
    'quiet_brightness': (0.0, 1.0),
    'digital_silence_seconds': (0.5, MAX_DIGITAL_SILENCE_SECONDS),
    'strobe_max_rate_hz': (0.0, STARFIRE_MAX_STROBE_HZ),
    'strobe_max_burst_seconds': (0.1, STARFIRE_MAX_STROBE_BURST_SECONDS),
    'intensity': (0.0, 1.0),
}

RETIRED_SETTINGS = frozenset({'palette_temperature_bias'})


@dataclass(frozen=True)
class ShowSettings:
    drop_strobe_cooldown_seconds: float = STROBE_COOLDOWN_SECONDS
    strobe_burst_seconds: float = STROBE_BURST_SECONDS
    crossfade_beats: float = CROSSFADE_BEATS
    quiet_brightness: float = QUIET_BRIGHTNESS
    digital_silence_seconds: float = DIGITAL_SILENCE_SECONDS
    strobe_max_rate_hz: float = STROBE_RATE_HZ
    strobe_max_burst_seconds: float = STROBE_MAX_BURST_SECONDS
    intensity: float = DEFAULT_INTENSITY
    intensity_auto: bool = False
    fx_auto: bool = True
    # Inhibitive submasters by rig group, 0..1: Starfire scales the group after the master.
    # An absent group is at full.
    group_masters: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        for name in ('intensity_auto', 'fx_auto'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be true or false')
        if not isinstance(self.group_masters, dict):
            raise TypeError('group_masters must map group names to levels')
        # Before sorting, which mixed key types would fail with a bare TypeError.
        if any(type(group) is not str or not group for group in self.group_masters):
            raise ValueError('group_masters keys must be group names')
        masters = {}
        for group, level in sorted(self.group_masters.items()):
            if type(level) not in (int, float) or not 0.0 <= level <= 1.0:
                raise ValueError(f'group master {group} must be within 0..1')
            masters[group] = float(level)
        object.__setattr__(self, 'group_masters', masters)
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
        data = {name: value for name, value in data.items() if name not in RETIRED_SETTINGS}
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f'unknown settings: {", ".join(sorted(unknown))}')
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def live_crossfade_beats(settings: ShowSettings) -> float:
    """The renderer's default crossfade: the saved setting scaled by the intensity level."""
    return settings.crossfade_beats * CROSSFADE_SCALE[intensity_level(settings.intensity)]


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
