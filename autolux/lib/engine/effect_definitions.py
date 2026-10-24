from enum import Enum

from autolux.lib.clients.midi_message import MidiChannel


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

# Authored SoundSwitch loops; require infinite repeat in SoundSwitch.
AUTOLOOP_BY_LIGHT_INTENT: dict[LightIntent, MidiChannel] = {
    LightIntent.ATMOSPHERIC: MidiChannel.AUTOLOOP_BANK_2A,  # ATM Blue Tide
    LightIntent.BREAKDOWN: MidiChannel.AUTOLOOP_BANK_2F,  # BREAK Violet Space
    LightIntent.BUILDUP: MidiChannel.AUTOLOOP_BANK_1A,  # BUILD Rising Current
    LightIntent.DROP: MidiChannel.AUTOLOOP_BANK_1F,  # DROP Blue Impact
}


def intent_for_class(label: str) -> LightIntent:
    try:
        return SECTION_CLASS_INTENTS[label]
    except KeyError:
        raise KeyError(
            f"the decoder committed {label!r}, which no LightIntent claims; "
            f"known classes are {', '.join(sorted(SECTION_CLASS_INTENTS))}"
        ) from None
