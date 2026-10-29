import logging
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from autolux.lib.engine.static_mode import UV_ATTRIBUTE, StaticRig
from autolux.lighting import mvr
from autolux.lighting.autoloop_catalogue import read_overlay_meta
from autolux.lighting.fixture_setup import load_rig, rig_modes
from autolux.lighting.gdtf import MAX_DESCRIPTION_BYTES
from autolux.lighting.gdtf_compile import compile_mode
from autolux.lighting.palettes import palette_table, wheel_warnings
from cli.starfire_protocol import (
    COMMAND_ID_MAX_ENCODED_BYTES,
    ROLE_NAMES,
    STAGE_AREAS,
    check_payload_size,
    movement,
    profile_id,
)
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

MAX_SCRIPT_BYTES = 64 * 1024
MAX_SCRIPT_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_SCRIPTS = 256
IDENTIFIER = re.compile(r'[a-z0-9_-]{1,64}')
WIRE_ROLES = {name: role for role, name in ROLE_NAMES.items()}
LOGGER = logging.getLogger(__name__)
PATCH_KEYS = ('gdtf_spec', 'gdtf_mode', 'universe', 'address', 'matrix')


def _read(path: Path, limit: int) -> bytes:
    with path.open('rb') as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'Asset exceeds its {limit}-byte limit: {path}')
    return content


def stage_areas(positions: list[tuple[float, float]]) -> list[int]:
    # MVR positions are in mm; GDTF's viewer axes, seen from the audience: -X house left, -Y downstage.

    def thirds(values: list[float]) -> list[int]:
        low, high = min(values), max(values)
        third = (high - low) / 3
        return [0 if value < low + third else 2 if value > high - third else 1 for value in values]

    if not positions:
        return []
    columns = thirds([x for x, _ in positions])
    rows = thirds([y for _, y in positions])
    return [STAGE_AREAS[row][column] for row, column in zip(rows, columns, strict=True)]


def rig_definition(path: Path) -> rig_wire.RigDefinition:
    scene, fixtures = load_rig(path)
    rig = rig_wire.RigDefinition()
    modes = {}
    plan = slice(mvr.POSITION_X, mvr.POSITION_X + 2)
    areas = stage_areas([tuple(fixture['matrix'][plan]) for fixture in fixtures])
    for fixture, area in zip(fixtures, areas, strict=True):
        spec, mode = fixture['gdtf_spec'], fixture['gdtf_mode']
        if spec not in modes:
            description = mvr.gdtf_description(scene, spec)
            if len(description.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
                raise ValueError('GDTF description exceeds its size limit')
            modes[spec] = mvr.gdtf_modes(description, spec)
            for name in modes[spec]:
                try:
                    compiled = compile_mode(description, name)
                except ValueError:
                    if any(f['gdtf_spec'] == spec and f['gdtf_mode'] == name for f in fixtures):
                        raise
                    continue
                rig.profiles.add(profile_id=profile_id(spec, name), dmx_mode=name, mode=compiled)
        entry = rig.fixtures.add(
            fixture_uuid=fixture['fixture_uuid'],
            name=fixture['name'],
            profile_id=profile_id(spec, mode),
            universe=fixture['universe'],
            address=fixture['address'],
            matrix=fixture['matrix'],
            focus=fixture['focus'],
            cells=[rig_wire.Fixture.Cell(**cell) for cell in modes[spec][mode]['cells']],
            area=area,
        )
        for axis in mvr.MOVEMENT_AXES:
            if (limits := movement(fixture[axis])) is not None:
                getattr(entry, axis).CopyFrom(limits)
    roles, _, warnings = mvr.group_roles(fixtures)
    groups, order_warnings = mvr.groups(fixtures)
    for warning in warnings + order_warnings:
        LOGGER.warning('[rig] %s', warning)
    for group, members in groups.items():
        rig.groups.add(group_id=group, fixture_uuids=members, role=WIRE_ROLES[roles[group]])
    return rig


def rig_bytes(path: Path) -> bytes:
    return rig_definition(Path(path)).SerializeToString()


@contextmanager
def native_rig(path: Path):
    with tempfile.TemporaryDirectory(prefix='starfire-rig-') as directory:
        exported = Path(directory) / 'rig.pb'
        exported.write_bytes(rig_bytes(path))
        yield exported


def static_rig(path: Path) -> StaticRig:
    scene, fixtures = load_rig(path)
    definition = rig_definition(path)
    compiled = {profile.profile_id for profile in definition.profiles}
    modes = {}
    for spec, spec_modes in rig_modes(scene).items():
        root = mvr.gdtf_root(mvr.gdtf_description(scene, spec), spec)
        attributes = {
            dmx_mode.get('Name'): {
                element.get('Attribute')
                for element in dmx_mode.iter()
                if element.tag in ('LogicalChannel', 'ChannelFunction')
            }
            - {None}
            for dmx_mode in root.iter('DMXMode')
        }
        modes[spec] = {
            mode: {
                'channels': entry['channels'],
                'cells': [cell['geometry'] for cell in entry['cells']],
                'attributes': sorted(attributes[mode]),
            }
            for mode, entry in spec_modes.items()
            if profile_id(spec, mode) in compiled
        }
    for fixture in fixtures:
        attributes = modes[fixture['gdtf_spec']][fixture['gdtf_mode']]['attributes']
        fixture['uv'] = UV_ATTRIBUTE in attributes
        fixture['pan'] = 'Pan' in attributes
    by_uuid = {fixture['fixture_uuid']: fixture for fixture in fixtures}
    groups = mvr.groups(fixtures)[0]
    patched = [
        (
            entry.fixture_uuid,
            entry.name,
            tuple(
                mvr.cell_id(entry.fixture_uuid, cell.geometry, len(entry.cells))
                for cell in entry.cells
            ),
        )
        for entry in definition.fixtures
    ]
    return StaticRig(
        groups=tuple(sorted(groups)),
        uv_groups=tuple(
            sorted(
                group
                for group, members in groups.items()
                if all(by_uuid[member]['uv'] for member in members)
            )
        ),
        fixtures=tuple(patched),
        # Starfire checks UV per cell, and a multi-cell fixture's UV cell is not known here.
        uv_targets=tuple(
            fixture_uuid
            for fixture_uuid, _, cells in patched
            if by_uuid[fixture_uuid]['uv'] and len(cells) == 1
        ),
        pan_targets=tuple(
            fixture_uuid for fixture_uuid, _, _ in patched if by_uuid[fixture_uuid]['pan']
        ),
        patch={
            fixture['fixture_uuid']: {key: fixture[key] for key in PATCH_KEYS}
            for fixture in fixtures
        },
        modes=modes,
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
            scripts.append((bank.name, path.stem, content.decode('utf-8')))
    if not scripts:
        raise ValueError('Autoloop directory contains no Lua scripts')
    overlays = read_overlay_meta(Path(directory))
    return [
        wire.AutoloopDefinition(
            autoloop_id=f'{bank}/{name}',
            bank=bank,
            lua_source=source,
            intensity_htp=(bank, name) in overlays and overlays[bank, name].intensity_htp,
        )
        for bank, name, source in scripts
    ]


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
        for warning in wheel_warnings(login.rig):
            LOGGER.warning('[palette] %s', warning)
        check_payload_size(wire.LOGIN, login.ByteSize() + COMMAND_ID_MAX_ENCODED_BYTES)
        return login
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'Invalid Starfire login assets: {error}') from error
