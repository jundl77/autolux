"""Bounded renderer pipes and the adapter from recorded musical commands."""

import math
import struct

from google.protobuf.message import Message

from autolux.lighting.mvr_schema import ACCENT_ROLE, DEPTHS, LOOP_ROLE, ZONES
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

ROLE_NAMES = {rig_wire.GROUP_ROLE_LOOP: LOOP_ROLE, rig_wire.GROUP_ROLE_ACCENT: ACCENT_ROLE}
# MIXED (members in several zones) and UNSPECIFIED have no name: the row matches no zone.
ZONE_NAMES = dict(
    zip(
        (wire.STAGE_ZONE_HOUSE_LEFT, wire.STAGE_ZONE_CENTRE, wire.STAGE_ZONE_HOUSE_RIGHT),
        ZONES,
        strict=True,
    )
)
DEPTH_NAMES = dict(zip((wire.STAGE_DEPTH_DOWNSTAGE, wire.STAGE_DEPTH_UPSTAGE), DEPTHS, strict=True))
MAX_LOGIN_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 4 * 1024
MAX_COMMAND_ID = (1 << 64) - 1
COMMAND_ID_MAX_ENCODED_BYTES = 11
FRAME_HEADER = struct.Struct('!BI')

COMMAND_MESSAGES = {
    wire.LOGIN: wire.Login,
    wire.PLAY_AUTOLOOP: wire.PlayAutoloop,
    wire.SET_ACTIVE_EFFECT: wire.SetActiveEffect,
    wire.UNSET_ACTIVE_EFFECT: wire.UnsetActiveEffect,
    wire.UPDATE_BEAT: wire.UpdateBeat,
    wire.SET_PARAMETER: wire.SetParameter,
    wire.DISCARD_DELAYED_AUDIO: wire.DiscardDelayedAudio,
    wire.HEARTBEAT: wire.Heartbeat,
}
EVENT_MESSAGES = {
    wire.LOGIN_REPLY: wire.LoginReply,
    wire.ERROR_REPLY: wire.ErrorReply,
    wire.HEARTBEAT: wire.Heartbeat,
    wire.METRICS: wire.Metrics,
}
MESSAGE_TYPE_BY_CLASS = {
    message: message_type for message_type, message in (COMMAND_MESSAGES | EVENT_MESSAGES).items()
}


PAYLOAD_LIMITS = {wire.LOGIN: MAX_LOGIN_BYTES}


def check_payload_size(message_type: int, length: int):
    limit = PAYLOAD_LIMITS.get(message_type, MAX_MESSAGE_BYTES)
    if length > limit:
        name = wire.MessageType.Name(message_type)
        raise ValueError(
            f'Starfire {name} payload of {length} bytes exceeds the {limit}-byte limit'
        )


PARAMETERS = {
    'set_master': (wire.OUTPUT_BRIGHTNESS, 'intensity', 'number'),
    'set_speed': (wire.ANIMATION_SPEED, 'multiplier', 'number'),
    'set_blackout': (wire.OUTPUT_BLACKOUT, 'enabled', 'boolean'),
    'set_normalized_rms_level': (wire.AUDIO_RMS_NORMALIZED, 'level', 'number'),
    'set_paused': (wire.PAUSED, 'paused', 'boolean'),
    'set_default_crossfade_beats': (wire.DEFAULT_CROSSFADE_BEATS, 'beats', 'number'),
    'set_strobe_max_rate_hz': (wire.STROBE_MAX_RATE_HZ, 'hz', 'number'),
    'set_strobe_max_burst_seconds': (wire.STROBE_MAX_BURST_SECONDS, 'seconds', 'number'),
    'set_palette': (wire.PALETTE, 'name', 'text'),
    'set_intensity': (wire.INTENSITY, 'level', 'number'),
    'set_group_master': (wire.GROUP_MASTER, 'level', 'number'),
}


def build_command(command: dict | Message) -> Message:
    """Copy a generated command, or adapt the application's replay command shape."""
    if isinstance(command, Message):
        if type(command) not in COMMAND_MESSAGES.values():
            raise ValueError('Expected a Starfire client command')
        message = type(command)()
        message.CopyFrom(command)
        if not isinstance(message, wire.Heartbeat):
            message.command_id = 0
    else:
        _finite(command)
        try:
            value = dict(command)
            kind = value.pop('kind')
            if kind == 'beat':
                bar_phase = value.pop('bar_phase', None)
                message = wire.UpdateBeat(
                    beat_index=value.pop('beat_index'),
                    beats_per_bar=value.pop('beats_per_bar'),
                    tempo_bpm=value.pop('bpm'),
                    **value,
                )
                if bar_phase is not None:
                    message.bar_phase = bar_phase
            elif kind == 'select':
                if 'transition' in value:
                    value['transition'] = _transition(value['transition'])
                message = wire.PlayAutoloop(
                    autoloop_id=f'{value.pop("bank")}/{value.pop("autoloop")}',
                    **value,
                )
            elif kind in PARAMETERS:
                parameter, field, value_type = PARAMETERS[kind]
                setting = wire.Parameter(type=parameter, **{value_type: value.pop(field)})
                if kind == 'set_master':
                    setting.fade_seconds = value.pop('fade_seconds', 0.0)
                if kind == 'set_group_master':
                    setting.group = value.pop('group')
                message = wire.SetParameter(parameter=setting)
                if value:
                    raise ValueError('Parameter command contains unexpected fields')
            elif kind == 'set_override':
                override = value.pop('value')
                if value:
                    raise ValueError('set_override requires exactly one value')
                message = _active_effect(override)
            elif kind == 'clear_override':
                message = wire.UnsetActiveEffect(effect_id=value.pop('id'), **value)
            elif kind == 'stop':
                if value:
                    raise ValueError('Idle playback command contains unexpected fields')
                message = wire.PlayAutoloop()
            elif kind == 'discard_delayed_audio':
                if value:
                    raise ValueError('Discarding delayed audio takes no fields')
                message = wire.DiscardDelayedAudio()
            elif kind == 'heartbeat':
                message = wire.Heartbeat(**value)
            else:
                raise ValueError(f'Unknown Starfire command: {kind}')
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f'Invalid Starfire command: {error}') from error
    return message


def _transition(value):
    value = dict(value)
    kind = value.pop('kind')
    if kind == 'cut':
        if value:
            raise ValueError('Cut transition contains unexpected fields')
        return wire.PlayAutoloop.Transition()
    if kind == 'crossfade':
        return wire.PlayAutoloop.Transition(crossfade_beats=value.pop('duration_beats'), **value)
    raise ValueError('Invalid Starfire transition')


def _active_effect(value):
    value = dict(value)
    target = value.pop('target')
    if target == 'all':
        target = {}
    elif not isinstance(target, dict) or set(target) not in ({'group'}, {'fixture'}):
        raise ValueError('Invalid fixture selection')
    lifetime = dict(value.pop('lifetime', {'kind': 'until_cleared'}))
    duration = lifetime.pop('kind')
    if duration == 'until_cleared':
        if lifetime:
            raise ValueError('Persistent effect contains unexpected lifetime fields')
    elif duration != 'seconds' or set(lifetime) != {'duration_seconds'}:
        raise ValueError('Effects support seconds or an explicitly cleared lifetime')
    properties = dict(value.pop('values', {}))
    if not properties.keys() <= {'intensity', 'colour', 'pan', 'tilt', 'strobe_hz', 'attributes'}:
        raise ValueError('Unknown effect property')
    if 'attributes' in properties:
        if not isinstance(properties['attributes'], dict):
            raise ValueError('GDTF attributes must map identifiers to physical values or presets')
        attributes = {}
        for name, setting in properties['attributes'].items():
            if type(setting) in (int, float):
                attributes[name] = wire.SetActiveEffect.AttributeValue(number=setting)
            elif isinstance(setting, str):
                attributes[name] = wire.SetActiveEffect.AttributeValue(preset=setting)
            else:
                raise ValueError('GDTF attribute values must be physical numbers or preset names')
        properties['attributes'] = attributes
    if value.pop('intensity_multiplier', None) is not None:
        raise ValueError('Effects set intensity directly; multipliers are not supported')
    if (special := value.pop('special_fx', None)) is not None:
        value['special_fx'] = _special_fx(special)
    return wire.SetActiveEffect(
        effect_id=value.pop('id'),
        **target,
        **properties,
        **lifetime,
        **value,
    )


SPECIAL_FX_FIELDS = {
    'colour_slot',
    'energy',
    'speed',
    'pulse_beats',
    'attack_beats',
    'release_beats',
    'pattern',
}


def _special_fx(value):
    if not isinstance(value, dict) or not value.keys() <= SPECIAL_FX_FIELDS:
        raise ValueError('Unknown special_fx field')
    return wire.SpecialFX(
        **{name: setting for name, setting in value.items() if setting is not None}
    )


def login_groups(reply) -> list[dict]:
    return [
        {
            'name': group.name,
            'cell_count': group.cell_count,
            'colour': group.colour,
            'speed': group.speed,
            'pulse': group.pulse,
            'pattern': group.pattern,
            'role': ROLE_NAMES[group.role],
            'zone': ZONE_NAMES.get(group.zone),
            'depth': DEPTH_NAMES.get(group.depth),
            'x': group.centre_x_metres,
        }
        for group in reply.groups
    ]


def encode_command(message: Message) -> bytes:
    if type(message) not in COMMAND_MESSAGES.values():
        raise ValueError('Expected a Starfire client command')
    if not isinstance(message, wire.Heartbeat) and not 0 < message.command_id <= MAX_COMMAND_ID:
        raise ValueError('Starfire command ID must be positive')
    payload = message.SerializeToString()
    message_type = MESSAGE_TYPE_BY_CLASS[type(message)]
    check_payload_size(message_type, len(payload))
    return FRAME_HEADER.pack(message_type, len(payload)) + payload


def read_event(stream) -> Message | None:
    prefix = _read_exact(stream, FRAME_HEADER.size, allow_eof=True)
    if prefix is None:
        return None
    message_type, length = FRAME_HEADER.unpack(prefix)
    if message_type not in EVENT_MESSAGES:
        raise ValueError(f'Unexpected Starfire server message type: {message_type}')
    check_payload_size(message_type, length)
    return EVENT_MESSAGES[message_type].FromString(_read_exact(stream, length))


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Starfire control values must be finite')
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite(item)


def _read_exact(stream, size: int, *, allow_eof=False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if allow_eof and not chunks:
                return None
            raise ValueError('Truncated Starfire protocol frame')
        chunks.extend(chunk)
    return bytes(chunks)
