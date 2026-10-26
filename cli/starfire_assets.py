import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from autolux.lib.engine.static_mode import UV_ATTRIBUTE, StaticRig
from autolux.lighting import mvr
from autolux.lighting.fixture_setup import load_rig
from autolux.lighting.gdtf import MAX_DESCRIPTION_BYTES
from autolux.lighting.palettes import palette_table
from cli.starfire_protocol import (
    COMMAND_ID_MAX_ENCODED_BYTES,
    check_payload_size,
)
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

MAX_SCRIPT_BYTES = 64 * 1024
MAX_SCRIPT_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_SCRIPTS = 256
IDENTIFIER = re.compile(r'[a-z0-9_-]{1,64}')


def _read(path: Path, limit: int) -> bytes:
    with path.open('rb') as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'Asset exceeds its {limit}-byte limit: {path}')
    return content


def _movement(limits: dict) -> rig_wire.Fixture.MovementRange | None:
    if limits == {'min': 0.0, 'max': 1.0, 'inverted': False}:
        return None
    return rig_wire.Fixture.MovementRange(
        minimum=limits['min'], maximum=limits['max'], inverted=limits['inverted']
    )


def rig_definition(path: Path) -> rig_wire.RigDefinition:
    scene, fixtures = load_rig(path)
    rig = rig_wire.RigDefinition()
    profiles = set()
    for fixture in fixtures:
        profile_id = f'{fixture["gdtf_spec"]}#{fixture["gdtf_mode"]}'
        if profile_id not in profiles:
            profiles.add(profile_id)
            description = mvr.gdtf_description(scene, fixture['gdtf_spec'])
            if len(description.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
                raise ValueError('GDTF description exceeds its size limit')
            rig.profiles.add(
                profile_id=profile_id, gdtf_xml=description, dmx_mode=fixture['gdtf_mode']
            )
        entry = rig.fixtures.add(
            fixture_id=fixture['name'],
            profile_id=profile_id,
            universe=fixture['universe'],
            address=fixture['address'],
            matrix=fixture['matrix'],
        )
        for axis in mvr.MOVEMENT_AXES:
            if (movement := _movement(fixture[axis])) is not None:
                getattr(entry, axis).CopyFrom(movement)
    for group, members in mvr.groups(fixtures).items():
        rig.groups.add(group_id=group, fixture_ids=members)
    return rig


@contextmanager
def native_rig(path: Path):
    with tempfile.TemporaryDirectory(prefix='starfire-rig-') as directory:
        exported = Path(directory) / 'rig.pb'
        exported.write_bytes(rig_definition(Path(path)).SerializeToString())
        yield exported


def static_rig(path: Path) -> StaticRig:
    scene, fixtures = load_rig(path)
    lit_by_uv = {}
    for fixture in fixtures:
        spec, mode = fixture['gdtf_spec'], fixture['gdtf_mode']
        if (spec, mode) not in lit_by_uv:
            root = mvr.gdtf_root(mvr.gdtf_description(scene, spec), spec)
            lit_by_uv[spec, mode] = any(
                logical.get('Attribute') == UV_ATTRIBUTE
                for dmx_mode in root.iter('DMXMode')
                if dmx_mode.get('Name') == mode
                for logical in dmx_mode.iter('LogicalChannel')
            )
        fixture['uv'] = lit_by_uv[spec, mode]
    by_name = {fixture['name']: fixture for fixture in fixtures}
    groups = mvr.groups(fixtures)
    return StaticRig(
        groups=tuple(sorted(groups)),
        uv_groups=tuple(
            sorted(
                group
                for group, members in groups.items()
                if all(by_name[name]['uv'] for name in members)
            )
        ),
    )


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


def palette_definitions() -> list[wire.Palette]:
    return [
        wire.Palette(
            name=palette.name,
            **{
                slot: wire.RgbColor(red=red, green=green, blue=blue)
                for slot, (red, green, blue) in palette.slots().items()
            },
        )
        for palette in palette_table()
    ]


def load_login(
    rig: Path,
    autoloops: Path,
    artnet: str | None,
    parameters=(),
    *,
    run_id: int,
    radius: wire.Login.RadiusOutput | None = None,
    render_core: int | None = None,
) -> wire.Login:
    try:
        if type(run_id) is not int or not 0 < run_id < 1 << 64:
            raise ValueError('run_id must be a nonzero uint64')
        if (radius is None) == (not artnet):
            raise ValueError('Select exactly one output: Art-Net or Radius')
        login = wire.Login(
            run_id=run_id,
            protocol_major=wire.PROTOCOL_MAJOR,
            protocol_minor=wire.PROTOCOL_MINOR,
            rig=rig_definition(Path(rig)),
            autoloops=_autoloop_definitions(autoloops),
            palettes=palette_definitions(),
            parameters=parameters,
            **({'artnet_destination': artnet} if radius is None else {'radius': radius}),
        )
        if render_core is not None:
            login.render_core = render_core
        check_payload_size(wire.LOGIN, login.ByteSize() + COMMAND_ID_MAX_ENCODED_BYTES)
        return login
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'Invalid Starfire login assets: {error}') from error
