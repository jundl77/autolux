import logging
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from autolux.lib.engine.static_mode import UV_ATTRIBUTE, StaticRig
from autolux.lighting import mvr
from autolux.lighting.fixture_setup import load_rig
from autolux.lighting.gdtf import MAX_DESCRIPTION_BYTES
from autolux.lighting.gdtf_compile import compile_mode
from autolux.lighting.palettes import palette_table
from cli.starfire_protocol import (
    COMMAND_ID_MAX_ENCODED_BYTES,
    ROLE_NAMES,
    STAGE_AREAS,
    check_payload_size,
)
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

MAX_SCRIPT_BYTES = 64 * 1024
MAX_SCRIPT_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_SCRIPTS = 256
IDENTIFIER = re.compile(r'[a-z0-9_-]{1,64}')
WIRE_ROLES = {name: role for role, name in ROLE_NAMES.items()}
LOGGER = logging.getLogger(__name__)


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


def stage_areas(positions: list[tuple[float, float]]) -> list[int]:
    """Each plan position's (X, Y in mm) cell of the nine-area stage grid.

    MVR 1.6 (DIN SPEC 15801) places a Fixture by its Matrix, "u1,u2,u3 / v1,v2,v3 / w1,w2,w3 /
    o1,o2,o3" in a "Right-handed, Z-Up, 1 Distance Unit equals 1 mm" system, the position being
    the o row. MVR names no audience side and gives its origin no meaning, so the axes follow
    GDTF 1.2 (DIN SPEC 15800), which draws a device from the viewer: "X – from left (-X) to
    right (+X), Y – from the outside of the monitor (-Y) to the inside of the monitor (+Y)".
    With the audience as the viewer, -X is house left and -Y downstage.

    The stage frame is therefore the rig's own bounding box: each axis is cut into equal thirds
    of its extent, a position on a cut belongs to the centre third, and an axis without extent
    puts every fixture in its centre third.
    """

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
    cells = {}
    plan = slice(mvr.POSITION_X, mvr.POSITION_X + 2)
    areas = stage_areas([tuple(fixture['matrix'][plan]) for fixture in fixtures])
    for fixture, area in zip(fixtures, areas, strict=True):
        spec, mode = fixture['gdtf_spec'], fixture['gdtf_mode']
        profile_id = f'{spec}#{mode}'
        if profile_id not in cells:
            description = mvr.gdtf_description(scene, spec)
            if len(description.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
                raise ValueError('GDTF description exceeds its size limit')
            cells[profile_id] = mvr.gdtf_modes(description, spec)[mode]['cells']
            rig.profiles.add(
                profile_id=profile_id,
                dmx_mode=mode,
                mode=compile_mode(description, mode),
            )
        entry = rig.fixtures.add(
            fixture_uuid=fixture['fixture_uuid'],
            name=fixture['name'],
            profile_id=profile_id,
            universe=fixture['universe'],
            address=fixture['address'],
            matrix=fixture['matrix'],
            cells=[rig_wire.Fixture.Cell(**cell) for cell in cells[profile_id]],
            area=area,
        )
        for axis in mvr.MOVEMENT_AXES:
            if (movement := _movement(fixture[axis])) is not None:
                getattr(entry, axis).CopyFrom(movement)
    roles, _, warnings = mvr.group_roles(fixtures)
    groups, order_warnings = mvr.groups(fixtures)
    for warning in warnings + order_warnings:
        LOGGER.warning('[rig] %s', warning)
    for group, members in groups.items():
        rig.groups.add(group_id=group, fixture_uuids=members, role=WIRE_ROLES[roles[group]])
    return rig


def rig_bytes(path: Path) -> bytes:
    """The rig.pb export-rig commits and Save regenerates beside the repo rig."""
    return rig_definition(Path(path)).SerializeToString()


@contextmanager
def native_rig(path: Path):
    with tempfile.TemporaryDirectory(prefix='starfire-rig-') as directory:
        exported = Path(directory) / 'rig.pb'
        exported.write_bytes(rig_bytes(path))
        yield exported


def static_rig(path: Path) -> StaticRig:
    scene, fixtures = load_rig(path)
    attributes = {}
    for fixture in fixtures:
        spec, mode = fixture['gdtf_spec'], fixture['gdtf_mode']
        if (spec, mode) not in attributes:
            root = mvr.gdtf_root(mvr.gdtf_description(scene, spec), spec)
            attributes[spec, mode] = {
                logical.get('Attribute')
                for dmx_mode in root.iter('DMXMode')
                if dmx_mode.get('Name') == mode
                for logical in dmx_mode.iter('LogicalChannel')
            }
        fixture['uv'] = UV_ATTRIBUTE in attributes[spec, mode]
        fixture['pan'] = 'Pan' in attributes[spec, mode]
    by_uuid = {fixture['fixture_uuid']: fixture for fixture in fixtures}
    groups = mvr.groups(fixtures)[0]
    # The ids come from the very RigDefinition Starfire loads, named by Starfire's cell rule.
    patched = [
        (
            entry.fixture_uuid,
            entry.name,
            tuple(
                mvr.cell_id(entry.fixture_uuid, cell.geometry, len(entry.cells))
                for cell in entry.cells
            ),
        )
        for entry in rig_definition(path).fixtures
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
        # Starfire checks attributes per cell, and which cell of a multi-cell fixture owns the
        # UV channel is not known here, so only single-cell fixtures get UV zeroed.
        uv_targets=tuple(
            fixture_uuid
            for fixture_uuid, _, cells in patched
            if by_uuid[fixture_uuid]['uv'] and len(cells) == 1
        ),
        pan_targets=tuple(
            fixture_uuid for fixture_uuid, _, _ in patched if by_uuid[fixture_uuid]['pan']
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
