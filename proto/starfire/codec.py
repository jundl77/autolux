"""Typed protobuf control over length-prefixed owned-process pipes."""

from functools import lru_cache
import math
from pathlib import Path
import struct

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
LENGTH_PREFIX = struct.Struct('!I')


@lru_cache(maxsize=1)
def _messages():
    package = Path(__file__).resolve().parent
    descriptor = package / 'starfire.pb'
    if not descriptor.is_file():
        descriptor = package.parents[1] / 'build/proto/starfire.pb'
    if not descriptor.is_file():
        raise RuntimeError('Starfire protocol is not built. Run build-starfire.ps1 first.')
    file_set = descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for file in file_set.file:
        pool.Add(file)
    return tuple(
        message_factory.GetMessageClass(pool.FindMessageTypeByName(f'autolux.starfire.v1.{name}'))
        for name in ('Control', 'RendererEvent')
    )


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Starfire control values must be finite')
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite(item)


def encode_control(command: dict) -> bytes:
    """Translate the shared semantic command shape into the generated schema."""
    _finite(command)
    value = dict(command)
    kind = value.pop('kind')
    if kind == 'select' and 'transition' in value:
        transition = dict(value['transition'])
        policy = transition.pop('kind')
        if policy == 'cut' and not transition:
            value['transition'] = {'cut': {}}
        elif policy == 'crossfade' and set(transition) == {'duration_beats'}:
            value['transition'] = {'crossfade_beats': transition['duration_beats']}
        else:
            raise ValueError('Invalid Starfire transition')
    elif kind == 'set_override':
        if set(value) != {'value'}:
            raise ValueError('set_override requires exactly one value')
        value = dict(value['value'])
        if value.get('target') == 'all':
            value['target'] = {'all': {}}
        if 'lifetime' in value:
            lifetime = dict(value['lifetime'])
            duration = lifetime.pop('kind')
            if duration == 'until_cleared' and not lifetime:
                value['lifetime'] = {'until_cleared': {}}
            elif duration == 'beats' and set(lifetime) == {'duration_beats'}:
                value['lifetime'] = {'beats': lifetime['duration_beats']}
            elif duration == 'seconds' and set(lifetime) == {'duration_seconds'}:
                value['lifetime'] = {'seconds': lifetime['duration_seconds']}
            else:
                raise ValueError('Invalid Starfire override lifetime')
    control_type, _ = _messages()
    try:
        message = json_format.ParseDict(
            {'schema_version': SCHEMA_VERSION, kind: value}, control_type()
        )
    except (json_format.ParseError, TypeError, ValueError) as error:
        raise ValueError(f'Invalid Starfire command: {error}') from error
    if message.WhichOneof('command') is None:
        raise ValueError('Starfire command is missing')
    payload = message.SerializeToString()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError('Starfire command exceeds the message limit')
    return LENGTH_PREFIX.pack(len(payload)) + payload


def read_event(stream) -> dict | None:
    """Read one complete event; distinguish clean EOF from truncated messages."""
    prefix = _read_exact(stream, LENGTH_PREFIX.size, allow_eof=True)
    if prefix is None:
        return None
    length = LENGTH_PREFIX.unpack(prefix)[0]
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise ValueError(f'Invalid Starfire event size: {length}')
    _, event_type = _messages()
    message = event_type.FromString(_read_exact(stream, length))
    if message.schema_version != SCHEMA_VERSION:
        raise ValueError(f'Unsupported Starfire schema version: {message.schema_version}')
    kind = message.WhichOneof('event')
    if kind is None:
        raise ValueError('Starfire event is missing')
    return {
        'event': kind,
        **json_format.MessageToDict(
            getattr(message, kind),
            preserving_proto_field_name=True,
            including_default_value_fields=True,
        ),
    }


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
