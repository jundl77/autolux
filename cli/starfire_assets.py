"""Load controller-owned fixture data and Lua source into a renderer login."""

import json
import math
import re
from pathlib import Path

from autolux.lighting.gdtf import MAX_CATALOGUE_BYTES, MAX_DESCRIPTION_BYTES
from cli.starfire import PROFILE_CATALOGUE
from cli.starfire_protocol import (
    COMMAND_ID_MAX_ENCODED_BYTES,
    MAX_MESSAGE_BYTES,
)
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

ASSET_SCHEMA_VERSION = 1
MAX_RIG_INPUT_BYTES = 1024 * 1024
MAX_SCRIPT_BYTES = 128 * 1024
MAX_SCRIPT_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_SCRIPTS = 256
IDENTIFIER = re.compile(r'[a-z0-9_-]{1,64}')


def _read(path: Path, limit: int) -> bytes:
    with path.open('rb') as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'Asset exceeds its {limit}-byte limit: {path}')
    return content


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Asset contains duplicate JSON fields')
    return result


def _object(value):
    if not isinstance(value, dict):
        raise TypeError('Fixture data must use JSON objects')
    return dict(value)


def _document(path, limit=MAX_RIG_INPUT_BYTES):
    value = _object(json.loads(_read(Path(path), limit), object_pairs_hook=_unique_object))
    version = value.pop('schema_version')
    if type(version) is not int or version != ASSET_SCHEMA_VERSION:
        raise ValueError(f'Unsupported asset schema version: {version}')
    return value


def _integer(value):
    if type(value) is not int:
        raise TypeError('DMX addresses and channel indices must be integers')
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError('Fixture numbers must be finite numbers')
    return value


def _movement(value):
    value = _object(value)
    if not value.keys() <= {'min', 'max', 'inverted'}:
        raise ValueError('Unknown fixture movement field')
    if 'inverted' in value and type(value['inverted']) is not bool:
        raise TypeError('Fixture inversion must be boolean')
    for source, target in (('min', 'minimum'), ('max', 'maximum')):
        if source in value:
            value[target] = _number(value.pop(source))
    return rig_wire.Fixture.MovementRange(**value)


def _fixture(value):
    value = _object(value)
    for axis in ('pan', 'tilt'):
        if axis in value:
            value[axis] = _movement(value[axis])
    return rig_wire.Fixture(
        fixture_id=value.pop('id'),
        profile_id=value.pop('profile'),
        universe=_integer(value.pop('universe')),
        address=_integer(value.pop('address')),
        **value,
    )


def _profile(profile_id, value):
    value = _object(value)
    if value.keys() != {'gdtf_xml', 'dmx_mode'} or any(
        not isinstance(field, str) or not field for field in value.values()
    ):
        raise ValueError('Fixture profiles require GDTF XML and a selected DMX mode')
    if len(value['gdtf_xml'].encode('utf-8')) > MAX_DESCRIPTION_BYTES:
        raise ValueError('GDTF description exceeds its size limit')
    return rig_wire.FixtureProfile(profile_id=profile_id, **value)


def _autoloop_definitions(directory):
    scripts = []
    total_bytes = 0
    for bank in sorted(Path(directory).iterdir()):
        if bank.is_symlink():
            raise ValueError(f'Autoloop banks must not be symlinks: {bank}')
        if not bank.is_dir():
            continue
        if IDENTIFIER.fullmatch(bank.name) is None:
            raise ValueError(f'Invalid bank name: {bank.name}')
        for path in sorted(bank.glob('*.lua')):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'Autoloops must be regular files: {path}')
            if IDENTIFIER.fullmatch(path.stem) is None:
                raise ValueError(f'Invalid autoloop name: {path.stem}')
            content = _read(path, MAX_SCRIPT_BYTES)
            total_bytes += len(content)
            if total_bytes > MAX_SCRIPT_CATALOGUE_BYTES or len(scripts) >= MAX_SCRIPTS:
                raise ValueError('Autoloop catalogue exceeds its size limit')
            scripts.append(
                wire.AutoloopDefinition(
                    autoloop_id=f'{bank.name}/{path.stem}',
                    bank=bank.name,
                    lua_source=content.decode('utf-8'),
                )
            )
    if not scripts:
        raise ValueError('Autoloop directory contains no Lua scripts')
    return scripts


def load_login(
    rig: Path,
    autoloops: Path,
    artnet: str | None,
    parameters=(),
    *,
    run_id: int,
    profiles: Path | None = None,
    radius: wire.Login.RadiusOutput | None = None,
    render_core: int | None = None,
) -> wire.Login:
    """Load one immutable asset snapshot before starting the owned renderer."""
    try:
        if type(run_id) is not int or not 0 < run_id < 1 << 64:
            raise ValueError('run_id must be a nonzero uint64')
        if (radius is None) == (not artnet):
            raise ValueError('Select exactly one output: Art-Net or Radius')
        patch = _document(rig)
        fixtures = [_fixture(value) for value in patch.pop('fixtures')]
        groups = []
        for name, members in _object(patch.pop('groups', {})).items():
            if not isinstance(members, list):
                raise TypeError('Fixture group members must be an array')
            groups.append(rig_wire.FixtureGroup(group_id=name, fixture_ids=members))
        catalogue = _document(profiles or PROFILE_CATALOGUE, MAX_CATALOGUE_BYTES)
        definitions = _object(catalogue.pop('profiles'))
        if catalogue:
            raise ValueError('Unknown fixture profile catalogue field')
        login = wire.Login(
            run_id=run_id,
            protocol_major=wire.PROTOCOL_MAJOR,
            protocol_minor=wire.PROTOCOL_MINOR,
            rig=rig_wire.RigDefinition(
                fixtures=fixtures,
                groups=groups,
                profiles=[
                    _profile(name, definitions[name])
                    for name in sorted({fixture.profile_id for fixture in fixtures})
                ],
                **patch,
            ),
            autoloops=_autoloop_definitions(autoloops),
            parameters=parameters,
            **({'artnet_destination': artnet} if radius is None else {'radius': radius}),
        )
        if render_core is not None:
            login.render_core = render_core
        if login.ByteSize() + COMMAND_ID_MAX_ENCODED_BYTES > MAX_MESSAGE_BYTES:
            raise ValueError('Login assets exceed the renderer message limit')
        return login
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'Invalid Starfire login assets: {error}') from error
