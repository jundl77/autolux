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

# Each intent's bank (named after the intent) is discovered from disk, and the
# selection policy chooses among all of it by the metadata each loop declares.
# The audited originals are the one fixed look per intent when no catalogue is
# loaded, and readiness requires them on disk.
PREFERRED_AUTOLOOPS: dict[LightIntent, str] = {
    LightIntent.ATMOSPHERIC: 'cold_breathe',
    LightIntent.BREAKDOWN: 'wide_space',
    LightIntent.BUILDUP: 'ramp',
    LightIntent.DROP: 'kick',
}

AutoloopTable = dict[LightIntent, tuple[tuple[str, str], ...]]

# Without a discovered catalogue (unit tests, offline tools) every intent keeps
# one fixed look, which keeps recorded command streams stable.
DEFAULT_AUTOLOOPS: AutoloopTable = {
    intent: ((intent.value, autoloop),) for intent, autoloop in PREFERRED_AUTOLOOPS.items()
}

# The header vocabulary of `autoloops/README.md`, as the strings a header spells.
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

# The headers of DEFAULT_AUTOLOOPS' files; a test keeps them equal.
DEFAULT_AUTOLOOP_META: AutoloopMetaTable = {
    ('atmospheric', 'cold_breathe'): AutoloopMeta(1, 'cold', 'sweep'),
    ('breakdown', 'wide_space'): AutoloopMeta(1, 'accent', 'sweep'),
    ('buildup', 'ramp'): AutoloopMeta(2, 'warm', 'sweep'),
    ('drop', 'kick'): AutoloopMeta(3, 'warm', 'snap'),
}


# Loops set their own levels, so a fade-out or a dead beat would otherwise stay
# at full output; master brightness dims the whole rig instead. QUIET_RMS is on
# the normalized level Starfire receives (0..1 over -48..-12 dBFS).
QUIET_RMS = 0.1
QUIET_HOLD_SECONDS = 2.0  # rides over a short breakdown dip
QUIET_BRIGHTNESS = 0.15  # dim, not dark: the room should still see the rig
BEAT_LOSS_SECONDS = 2.5

# Living-room parties: a strobe is a rare accent, never a sustained wash.
STROBE_BURST_BEATS = 8  # two bars at most
STROBE_MAX_BURST_SECONDS = 4.0
STROBE_RATE_HZ = 8.0  # ceiling; faster flicker is harsh in a small room
STROBE_BURST_SECONDS = 1.0
STROBE_COOLDOWN_SECONDS = 60.0
CROSSFADE_BEATS = 1.0
MAX_STROBE_COOLDOWN_SECONDS = 600.0
MAX_CROSSFADE_BEATS = 16.0

SHOW_SETTING_RANGES: dict[str, tuple[float, float]] = {
    'drop_strobe_cooldown_seconds': (0.0, MAX_STROBE_COOLDOWN_SECONDS),
    # Starfire refuses a zero-length burst or crossfade.
    'strobe_burst_seconds': (0.1, STROBE_MAX_BURST_SECONDS),
    'crossfade_beats': (0.25, MAX_CROSSFADE_BEATS),
    'quiet_brightness': (0.0, 1.0),
}


@dataclass(frozen=True)
class ShowSettings:
    """Operator-tunable show policy; the controller owns the live instance."""

    drop_strobe_cooldown_seconds: float = STROBE_COOLDOWN_SECONDS
    strobe_burst_seconds: float = STROBE_BURST_SECONDS
    crossfade_beats: float = CROSSFADE_BEATS
    quiet_brightness: float = QUIET_BRIGHTNESS

    def __post_init__(self):
        for name, (low, high) in SHOW_SETTING_RANGES.items():
            value = getattr(self, name)
            # bool is an int subclass, and a JSON true must not become 1 s.
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'{name} must be a number')
            if not low <= value <= high:
                raise ValueError(f'{name} must be within {low:g}..{high:g}')
            object.__setattr__(self, name, float(value))

    @classmethod
    def from_dict(cls, data: dict) -> 'ShowSettings':
        """Missing keys keep their defaults so older saved files still load."""
        if not isinstance(data, dict):
            raise TypeError('settings must be an object')
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f'unknown settings: {", ".join(sorted(unknown))}')
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def transition_for(intent: LightIntent, settings: ShowSettings) -> dict:
    # A drop should hit, so it cuts; every other change blends.
    if intent == LightIntent.DROP:
        return {'kind': 'cut'}
    return {'kind': 'crossfade', 'duration_beats': settings.crossfade_beats}


def intent_for_class(label: str) -> LightIntent:
    try:
        return SECTION_CLASS_INTENTS[label]
    except KeyError:
        raise KeyError(
            f"the decoder committed {label!r}, which no LightIntent claims; "
            f"known classes are {', '.join(sorted(SECTION_CLASS_INTENTS))}"
        ) from None
