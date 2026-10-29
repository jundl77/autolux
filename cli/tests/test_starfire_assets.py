import uuid
from pathlib import Path

import pytest

from autolux.lighting import mvr
from autolux.lighting.gdtf_compile import compile_mode
from cli import starfire_assets as assets
from cli import starfire_protocol as protocol
from cli.starfire import DEFAULT_AUTOLOOPS, DEFAULT_RIG, ROOT
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

GDTF_XML = (Path(__file__).parents[2] / 'autolux/tests/data/gdtf/dimmer.xml').read_text(
    encoding='utf-8'
)
SPEC = 'Generic@Test Dimmer@1.0.gdtf'
ROOM = ROOT / 'autolux/tests/data/room.mvr'


@pytest.fixture
def login_assets(tmp_path, write_rig):
    library = tmp_path / 'library'
    library.mkdir()
    (library / SPEC).write_bytes(mvr.gdtf_package(GDTF_XML))
    rig = write_rig(library, 'front', 'movers', (180.0, 0.0, 0.0), invert_pan=True, mode='16 bit')
    autoloops = tmp_path / 'autoloops'
    bank = autoloops / 'atmospheric'
    bank.mkdir(parents=True)
    (bank / 'blue.lua').write_text('return { render = function(ctx) end }\n', encoding='utf-8')
    return rig, autoloops


def edit_scene(rig: Path, old: str, new: str) -> None:
    scene = mvr.read(rig.read_bytes())
    xml = mvr._xml_bytes(scene.root).decode('utf-8')
    assert old in xml
    scene.root = mvr._guard_xml(xml.replace(old, new).encode('utf-8'), 'test')
    rig.write_bytes(mvr.write(scene))


def test_login_carries_embedded_gdtf_inversion_class_and_scene_matrix(login_assets):
    rig, autoloops = login_assets
    parameter = wire.Parameter(type=wire.OUTPUT_BRIGHTNESS, number=0)
    login = assets.load_login(rig, autoloops, '127.0.0.1:6454', [parameter], run_id=(1 << 64) - 1)
    assert login.run_id == (1 << 64) - 1
    assert login.command_id == 0
    assert login.protocol_major == wire.PROTOCOL_MAJOR
    assert login.artnet_destination == '127.0.0.1:6454'
    assert login.parameters == [parameter]
    fixture = login.rig.fixtures[0]
    assert uuid.UUID(fixture.fixture_uuid).version == 4 and fixture.name == 'front'
    assert fixture.profile_id == f'{SPEC}#16 bit'
    assert fixture.pan.inverted
    assert (fixture.pan.minimum, fixture.pan.maximum) == (0, 1)
    assert not fixture.HasField('tilt')
    assert list(fixture.matrix[:3]) == [-1, 0, 0]
    assert login.rig.groups[0].group_id == 'movers'
    assert login.rig.groups[0].fixture_uuids == [fixture.fixture_uuid]
    [profile] = [profile for profile in login.rig.profiles if profile.dmx_mode == '16 bit']
    assert profile.mode == compile_mode(GDTF_XML, '16 bit')
    assert profile.dmx_mode == '16 bit'
    script = login.autoloops[0]
    assert script.autoloop_id == 'atmospheric/blue'
    assert script.bank == 'atmospheric'
    assert script.lua_source == (autoloops / 'atmospheric/blue.lua').read_bytes().decode('utf-8')
    login.command_id = protocol.MAX_COMMAND_ID
    encoded = protocol.encode_command(login)
    assert wire.Login.FromString(encoded[protocol.FRAME_HEADER.size :]) == login


def test_room_assets_fit_one_login_without_losing_profile_definitions_or_scripts():
    login = assets.load_login(ROOM, DEFAULT_AUTOLOOPS, '127.0.0.1:6454', run_id=1)
    scene = mvr.read(ROOM.read_bytes())
    fixtures = mvr.patched_fixtures(scene)
    assert [(fixture.fixture_uuid, fixture.name) for fixture in login.rig.fixtures] == [
        (f['fixture_uuid'], f['name']) for f in fixtures
    ]
    assert [list(fixture.matrix) for fixture in login.rig.fixtures] == [
        f['matrix'] for f in fixtures
    ]
    for profile in login.rig.profiles:
        spec, mode = profile.profile_id.split('#')
        assert profile.mode == compile_mode(mvr.gdtf_description(scene, spec), mode)
        assert profile.dmx_mode == mode
    assert {script.autoloop_id for script in login.autoloops} == {
        path.relative_to(DEFAULT_AUTOLOOPS).with_suffix('').as_posix()
        for path in DEFAULT_AUTOLOOPS.glob('*/*.lua')
    }
    assert login.ByteSize() + protocol.COMMAND_ID_MAX_ENCODED_BYTES <= protocol.MAX_LOGIN_BYTES


@pytest.mark.parametrize(
    'old,new',
    [
        ('<GDTFMode>16 bit</GDTFMode>', '<GDTFMode>missing</GDTFMode>'),
        ('<Address break="0">1</Address>', '<Address break="0">1.513</Address>'),
        ('<FixtureIDNumeric>1</FixtureIDNumeric>', ''),
        ('{-1,0,0}', '{-1,0,0}{0,0,0}'),
    ],
)
def test_nonconforming_rig_is_not_sent_to_renderer(login_assets, old, new):
    rig, autoloops = login_assets
    edit_scene(rig, old, new)
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def test_corrupt_archive_is_not_sent_to_renderer(login_assets):
    rig, autoloops = login_assets
    rig.write_bytes(b'{"schema_version":1,"fixtures":[]}')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def _fill_bank(autoloops, count, size):
    for index in range(count):
        source = 'return { render = function(ctx) end }\n'
        (autoloops / f'atmospheric/loop_{index}.lua').write_bytes(
            (source + '-' * (size - len(source))).encode()
        )


def test_full_autoloop_library_fits_one_login(login_assets):
    rig, autoloops = login_assets
    _fill_bank(autoloops, 40, 3 * 1024)
    login = assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)
    assert len(login.autoloops) == 41


def test_overlay_intensity_composition_comes_from_the_loop_header(login_assets):
    rig, autoloops = login_assets
    (autoloops / 'hit').mkdir()
    body = 'return {render=function(ctx) return {} end}\n'
    (autoloops / 'hit/blinder.lua').write_text(
        '-- 1-beat hit\n-- strobe: no\n-- intensity: htp\n-- movement: still\n-- texture: wash\n'
        + body
    )
    login = assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)
    htp = {script.autoloop_id: script.intensity_htp for script in login.autoloops}
    assert htp == {'atmospheric/blue': False, 'hit/blinder': True}


def test_a_bad_overlay_header_fails_login(login_assets):
    rig, autoloops = login_assets
    (autoloops / 'hit').mkdir()
    body = 'return {render=function(ctx) return {} end}\n'
    (autoloops / 'hit/later.lua').write_text('-- x\nlocal y = 1\n-- intensity: htp\n' + body)
    with pytest.raises(ValueError, match='hit/later: missing strobe, intensity'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def test_single_autoloop_over_its_cap_is_rejected(login_assets):
    rig, autoloops = login_assets
    _fill_bank(autoloops, 1, assets.MAX_SCRIPT_BYTES + 1)
    with pytest.raises(ValueError, match=f'{assets.MAX_SCRIPT_BYTES}-byte limit'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def test_combined_login_limit_is_checked_before_the_renderer_is_started(login_assets, monkeypatch):
    rig, autoloops = login_assets
    monkeypatch.setitem(protocol.PAYLOAD_LIMITS, wire.LOGIN, 64 * 1024)
    _fill_bank(autoloops, 40, 3 * 1024)
    with pytest.raises(
        ValueError, match=r'LOGIN payload of \d+ bytes exceeds the 65536-byte limit'
    ):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def test_invalid_autoloop_name_is_rejected(login_assets):
    rig, autoloops = login_assets
    (autoloops / 'atmospheric/blue.lua').rename(autoloops / 'atmospheric/Blue Sky.lua')
    with pytest.raises(ValueError, match='Invalid autoloop name'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


@pytest.mark.parametrize(
    'rig',
    [
        DEFAULT_RIG,
        ROOM,
        ROOT / 'autolux/tests/data/generic_rig/generic.mvr',
        ROOT / 'autolux/tests/data/generic_rig/stage.mvr',
    ],
)
def test_committed_native_rig_is_the_exported_login_rig(rig):
    login = assets.load_login(rig, DEFAULT_AUTOLOOPS, '127.0.0.1:6454', run_id=1)
    with assets.native_rig(rig) as exported:
        content = exported.read_bytes()
    committed = rig.with_suffix('.rig.pb')
    assert content == committed.read_bytes(), (
        f'Regenerate with: uv run autolux starfire export-rig {rig} {committed}'
    )
    assert rig_wire.RigDefinition.FromString(content) == login.rig


def test_stage_areas_cut_the_rigs_bounding_box_into_thirds():
    areas = assets.stage_areas([(1000, -500), (2500, 1000), (4000, 2500), (1100, 2400)])
    assert [rig_wire.StageArea.Name(area) for area in areas] == [
        'STAGE_AREA_DOWN_LEFT',
        'STAGE_AREA_CENTRE',
        'STAGE_AREA_UP_RIGHT',
        'STAGE_AREA_UP_LEFT',
    ]


def test_a_fixture_on_a_cut_lies_in_the_centre_third():
    areas = assets.stage_areas([(0, 0), (1000, 1000), (2000, 2000), (3000, 3000)])
    assert [rig_wire.StageArea.Name(area) for area in areas] == [
        'STAGE_AREA_DOWN_LEFT',
        'STAGE_AREA_CENTRE',
        'STAGE_AREA_CENTRE',
        'STAGE_AREA_UP_RIGHT',
    ]


def test_an_axis_without_extent_puts_every_fixture_in_its_centre():
    one_row = assets.stage_areas([(-2000, 700), (0, 700), (2000, 700)])
    assert [rig_wire.StageArea.Name(area) for area in one_row] == [
        'STAGE_AREA_CENTRE_LEFT',
        'STAGE_AREA_CENTRE',
        'STAGE_AREA_CENTRE_RIGHT',
    ]
    assert assets.stage_areas([(5, 5)]) == [rig_wire.STAGE_AREA_CENTRE]
    assert assets.stage_areas([]) == []


def test_the_room_rig_fixtures_lie_in_their_stage_areas():
    expected = {
        'spot_left': 'STAGE_AREA_UP_CENTRE',
        'spot_right': 'STAGE_AREA_UP_CENTRE',
        'wash_left': 'STAGE_AREA_CENTRE_LEFT',
        'wash_mid_left': 'STAGE_AREA_CENTRE',
        'wash_mid_right': 'STAGE_AREA_CENTRE',
        'wash_right': 'STAGE_AREA_CENTRE_RIGHT',
        'zq06017': 'STAGE_AREA_DOWN_CENTRE',
        'kinta': 'STAGE_AREA_UP_CENTRE',
    }
    rig = assets.rig_definition(ROOM)
    assert {
        fixture.name: rig_wire.StageArea.Name(fixture.area) for fixture in rig.fixtures
    } == expected


def test_login_carries_the_palette_table_and_palette_commands_round_trip():
    login = assets.load_login(ROOM, DEFAULT_AUTOLOOPS, '127.0.0.1:6454', run_id=1)
    assert [palette.name for palette in login.palettes][:2] == ['techno', 'techno_breakdown']
    techno = login.palettes[0]
    assert (techno.secondary.red, techno.secondary.green, techno.secondary.blue) == (
        1.0,
        140 / 255,
        26 / 255,
    )
    commands = [
        protocol.build_command({'kind': 'set_palette', 'name': 'ice'}),
        protocol.build_command(
            {'kind': 'select', 'bank': 'drop', 'autoloop': 'kick', 'palette': 'ice_breakdown'}
        ),
    ]
    for message in commands:
        message.command_id = 1
        encoded = protocol.encode_command(message)
        assert type(message).FromString(encoded[protocol.FRAME_HEADER.size :]) == message
    parameter, selection = commands
    assert (parameter.parameter.type, parameter.parameter.text) == (wire.PALETTE, 'ice')
    assert (selection.autoloop_id, selection.palette) == ('drop/kick', 'ice_breakdown')


def test_static_looks_for_the_room_rig_build_small_wire_accents():
    from autolux.lib.engine.static_mode import StaticLooks
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    rig = assets.static_rig(ROOM)
    assert (rig.groups, rig.uv_groups) == (('fx', 'movers', 'strip', 'wash'), ('wash',))
    lighting = RecordingStarfireClient()
    looks = StaticLooks(lighting, rig)
    looks.preset('uv')
    looks.look('fx', colour=(1.0, 0.5, 0.0), dimmer=0.25)
    messages = []
    for event in lighting.events:
        message = protocol.build_command(event['command'])
        message.command_id = 1
        protocol.encode_command(message)
        messages.append(message)
    wash = next(m for m in messages if m.effect_id == 'static_wash')
    assert (wash.group, wash.intensity, wash.attributes['ColorAdd_UV'].number) == (
        'wash',
        1.0,
        255.0,
    )
    assert (wash.colour.red, wash.colour.green, wash.colour.blue) == (0.0, 0.0, 0.0)
    assert {m.effect_id for m in messages} == {'static_all', 'static_wash', 'static_fx'}


def test_the_room_rig_names_every_fixture_and_cell_and_a_highlight_encodes_for_starfire():
    from autolux.lib.engine.static_mode import StaticLooks
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    rig = assets.static_rig(ROOM)
    ids = {name: identity for identity, name, _ in rig.fixtures}
    cells = {name: cells for _, name, cells in rig.fixtures}
    assert list(ids.values()) == [
        entry.fixture_uuid for entry in assets.rig_definition(ROOM).fixtures
    ]
    assert cells['wash_left'] == (ids['wash_left'],)
    assert cells['zq06017'] == tuple(f'{ids["zq06017"]}/Beam Segment {n}' for n in range(1, 9))
    washes = ('wash_left', 'wash_mid_left', 'wash_mid_right', 'wash_right')
    assert rig.uv_targets == tuple(ids[name] for name in washes)
    assert rig.pan_targets == (ids['spot_left'], ids['spot_right'])
    lighting = RecordingStarfireClient()
    looks = StaticLooks(lighting, rig)
    looks.highlight({'fixture': ids['wash_left']}, 'warm')
    looks.highlight({'cell': cells['zq06017'][2]})
    looks.highlight({'fixture': ids['spot_left']}, sweep=True)
    pose = {
        'matrix': [0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 100.0, 200.0, 3000.0],
        'pan': {'min': 0.1, 'max': 0.8, 'inverted': True},
        'tilt': {'min': 0.0, 'max': 1.0, 'inverted': False},
        'focus': [0.0, 2000.0, 0.0],
        'universe': 0,
        'address': 300,
        'gdtf_mode': '12ch',
    }
    looks.highlight({'fixture': ids['spot_left']}, aim=True, pose=pose)
    wash, segment, sweep, aim = (
        protocol.build_command(event['command']) for event in lighting.events
    )
    moved = (sweep.pan_sweep, sweep.HasField('pan'), sweep.HasField('tilt'), aim.aim_focus)
    assert moved == (True, False, False, True)
    assert (list(aim.pose.matrix), list(aim.pose.focus), aim.HasField('tilt')) == (
        pose['matrix'],
        pose['focus'],
        False,
    )
    assert (aim.pose.pan.minimum, aim.pose.pan.inverted, aim.pose.HasField('tilt')) == (
        0.1,
        True,
        False,
    )
    assert (aim.pose.universe, aim.pose.address, aim.pose.profile_id) == (
        0,
        300,
        'Chauvet DJ@Intimidator Spot 110@1.0.gdtf#12ch',
    )
    assert not sweep.HasField('pose')
    assert (wash.fixture, wash.attributes['ColorAdd_UV'].number, wash.duration_seconds) == (
        ids['wash_left'],
        0.0,
        60.0,
    )
    assert (segment.fixture, segment.effect_id, dict(segment.attributes)) == (
        cells['zq06017'][2],
        'highlight',
        {},
    )


SAMPLE_RIG = ROOT / 'autolux/tests/data/generic_rig'


@pytest.mark.parametrize(
    ('folder', 'name', 'libraries'),
    [
        (ROOM.parent, 'room', [ROOT / 'rig/library']),
        (SAMPLE_RIG, 'generic', [ROOT / 'rig/library', SAMPLE_RIG]),
        (SAMPLE_RIG, 'stage', [ROOT / 'rig/library']),
    ],
)
def test_a_rig_rebuilt_from_the_library_exports_the_committed_rig_definition(
    tmp_path, folder, name, libraries
):
    from autolux.lighting.gdtf import fixture_library

    shipped = folder / f'{name}.mvr'
    scene = mvr.read(shipped.read_bytes())
    without_gdtf = {k: v for k, v in scene.entries.items() if not k.endswith(mvr.GDTF_SUFFIX)}
    bare = mvr.Scene(scene.root, without_gdtf)
    library = {spec: entry for path in libraries for spec, entry in fixture_library(path).items()}
    rebuilt = tmp_path / f'{name}.mvr'
    rebuilt.write_bytes(mvr.write(mvr.apply_draft(bare, mvr.draft(scene), library)))
    committed = folder / f'{name}.rig.pb'
    hint = f'Regenerate with: uv run autolux starfire export-rig {shipped} {committed}'
    assert rebuilt.read_bytes() == shipped.read_bytes(), hint
    exported = assets.rig_definition(rebuilt).SerializeToString()
    assert exported == committed.read_bytes(), hint


def test_the_room_bar_is_one_fixture_whose_eight_cells_are_the_strip():
    rig = assets.rig_definition(ROOM)
    [bar] = [fixture for fixture in rig.fixtures if fixture.name == 'zq06017']
    assert (bar.universe, bar.address) == (0, 97)
    assert bar.profile_id.endswith('#24Channel')
    assert [(cell.geometry, list(cell.offsets)) for cell in bar.cells] == [
        (f'Beam Segment {n}', [3 * n - 2, 3 * n - 1, 3 * n]) for n in range(1, 9)
    ]
    groups = {group.group_id: list(group.fixture_uuids) for group in rig.groups}
    assert groups['strip'] == [bar.fixture_uuid]
