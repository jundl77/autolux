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

AUTOLOOP_BY_LIGHT_INTENT: dict[LightIntent, tuple[str, str]] = {
    LightIntent.ATMOSPHERIC: ('atmospheric', 'cold_breathe'),
    LightIntent.BREAKDOWN: ('breakdown', 'wide_space'),
    LightIntent.BUILDUP: ('buildup', 'ramp'),
    LightIntent.DROP: ('drop', 'kick'),
}

# A drop should hit, so it cuts; every other change blends over one beat.
TRANSITION_BY_LIGHT_INTENT: dict[LightIntent, dict] = {
    LightIntent.ATMOSPHERIC: {'kind': 'crossfade', 'duration_beats': 1},
    LightIntent.BREAKDOWN: {'kind': 'crossfade', 'duration_beats': 1},
    LightIntent.BUILDUP: {'kind': 'crossfade', 'duration_beats': 1},
    LightIntent.DROP: {'kind': 'cut'},
}


# Living-room parties: a strobe is a rare accent, never a sustained wash.
STROBE_BURST_BEATS = 8  # two bars at most
STROBE_MAX_BURST_SECONDS = 4.0
STROBE_RATE_HZ = 8.0  # ceiling; faster flicker is harsh in a small room
STROBE_COOLDOWN_SECONDS = 120.0


def intent_for_class(label: str) -> LightIntent:
    try:
        return SECTION_CLASS_INTENTS[label]
    except KeyError:
        raise KeyError(
            f"the decoder committed {label!r}, which no LightIntent claims; "
            f"known classes are {', '.join(sorted(SECTION_CLASS_INTENTS))}"
        ) from None
