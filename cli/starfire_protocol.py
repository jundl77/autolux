"""Bounded renderer pipes and the adapter from recorded musical commands."""

import math
import struct

from proto.starfire import starfire_pb2 as wire

MAX_MESSAGE_BYTES = 64 * 1024
MAX_COMMAND_ID = (1 << 64) - 1
COMMAND_ID_MAX_ENCODED_BYTES = 11
MAX_INTENSITY_MULTIPLIER = 8.0
LENGTH_PREFIX = struct.Struct('!I')


def build_command(command: dict | wire.RendererCommand) -> wire.RendererCommand:
    """Copy a generated command, or adapt the application's replay command shape."""
    if isinstance(command, wire.RendererCommand):
        message = wire.RendererCommand()
        message.CopyFrom(command)
        message.command_id = 0
    else:
        _finite(command)
        try:
            value = dict(command)
            kind = value.pop('kind')
            if kind == 'beat':
                message = wire.RendererCommand(
                    update_beat=wire.UpdateBeat(
                        position_beats=value.pop('position_beats'),
                        tempo_bpm=value.pop('bpm'),
                        **value,
                    )
                )
            elif kind == 'select':
                transition = value.pop('transition', {'kind': 'cut'})
                message = wire.RendererCommand(
                    play_sequence=wire.PlaySequence(
                        sequence=wire.SequenceReference(
                            bank_id=value.pop('bank'), sequence_id=value.pop('sequence'), **value
                        ),
                        transition=_transition(transition),
                    )
                )
            elif kind in ('set_master', 'set_speed', 'set_blackout', 'set_normalized_rms_level'):
                parameter, field = {
                    'set_master': ('output_brightness', 'intensity'),
                    'set_speed': ('animation_speed', 'multiplier'),
                    'set_blackout': ('output_blackout', 'enabled'),
                    'set_normalized_rms_level': ('audio_rms_normalized', 'level'),
                }[kind]
                message = wire.RendererCommand(
                    set_parameter=wire.SetParameter(**{parameter: value.pop(field)})
                )
                if value:
                    raise ValueError('Parameter command contains unexpected fields')
            elif kind == 'set_override':
                override = value.pop('value')
                if value:
                    raise ValueError('set_override requires exactly one value')
                message = wire.RendererCommand(set_active_effect=_active_effect(override))
            elif kind == 'clear_override':
                message = wire.RendererCommand(
                    unset_active_effect=wire.UnsetActiveEffect(effect_id=value.pop('id'), **value)
                )
            elif kind == 'stop':
                if value:
                    raise ValueError('Idle playback command contains unexpected fields')
                message = wire.RendererCommand(play_sequence=wire.PlaySequence())
            elif kind == 'heartbeat':
                message = wire.RendererCommand(heartbeat=wire.Heartbeat(**value))
            else:
                raise ValueError(f'Unknown Starfire command: {kind}')
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f'Invalid Starfire command: {error}') from error
    if message.WhichOneof('command') is None:
        raise ValueError('Starfire command is missing')
    return message


def _transition(value):
    value = dict(value)
    kind = value.pop('kind')
    if kind == 'cut':
        return wire.Transition(cut=wire.Empty(**value))
    if kind == 'crossfade':
        return wire.Transition(crossfade_beats=value.pop('duration_beats'), **value)
    raise ValueError('Invalid Starfire transition')


def _active_effect(value):
    value = dict(value)
    target = value.pop('target')
    if target == 'all':
        fixtures = wire.FixtureSelection(all=wire.Empty())
    elif isinstance(target, dict) and len(target) == 1:
        kind, identity = next(iter(target.items()))
        fixtures = wire.FixtureSelection(**{f'{kind}_id': identity})
    else:
        raise ValueError('Invalid fixture selection')
    lifetime = dict(value.pop('lifetime', {'kind': 'until_cleared'}))
    duration = lifetime.pop('kind')
    if duration == 'until_cleared':
        lifetime = wire.EffectLifetime(until_removed=wire.Empty(**lifetime))
    elif duration in ('seconds', 'beats') and set(lifetime) == {f'duration_{duration}'}:
        lifetime = wire.EffectLifetime(**lifetime)
    else:
        raise ValueError('Invalid effect lifetime')
    properties = dict(value.pop('values', {}))
    multiplier = value.pop('intensity_multiplier', None)
    attributes = []
    if multiplier is not None:
        if not 0 <= multiplier <= MAX_INTENSITY_MULTIPLIER:
            raise ValueError('Intensity multiplier is outside the supported range')
        if properties.get('intensity') is not None:
            if not 0 <= properties['intensity'] <= 1:
                raise ValueError('Intensity is outside the supported range')
            properties['intensity'] = min(1.0, properties['intensity'] * multiplier)
        else:
            attributes.append(
                wire.AttributeAssignment(
                    attribute=wire.INTENSITY,
                    value=wire.ParameterValue(number=multiplier),
                    operation=wire.MULTIPLY,
                )
            )
    for key, item in properties.items():
        if item is not None:
            parameter = (
                wire.ParameterValue(rgb=wire.RgbColor(**item))
                if key == 'colour'
                else wire.ParameterValue(number=item)
            )
            attribute = {
                'intensity': wire.INTENSITY,
                'colour': wire.COLOUR,
                'pan': wire.PAN,
                'tilt': wire.TILT,
                'strobe_hz': wire.STROBE_HZ,
            }[key]
            attributes.append(
                wire.AttributeAssignment(
                    attribute=attribute, value=parameter, operation=wire.REPLACE
                )
            )
    return wire.SetActiveEffect(
        effect_id=value.pop('id'),
        fixtures=fixtures,
        attributes=attributes,
        lifetime=lifetime,
        **value,
    )


def encode_command(message: wire.RendererCommand) -> bytes:
    if not 0 < message.command_id <= MAX_COMMAND_ID:
        raise ValueError('Starfire command ID must be positive')
    if message.WhichOneof('command') is None:
        raise ValueError('Starfire command is missing')
    payload = message.SerializeToString()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError('Starfire command exceeds the message limit')
    return LENGTH_PREFIX.pack(len(payload)) + payload


def read_event(stream) -> wire.RendererEvent | None:
    prefix = _read_exact(stream, LENGTH_PREFIX.size, allow_eof=True)
    if prefix is None:
        return None
    length = LENGTH_PREFIX.unpack(prefix)[0]
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise ValueError(f'Invalid Starfire event size: {length}')
    message = wire.RendererEvent.FromString(_read_exact(stream, length))
    if message.WhichOneof('event') is None:
        raise ValueError('Starfire event is missing')
    return message


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
