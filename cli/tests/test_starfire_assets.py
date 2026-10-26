import json
from pathlib import Path

import pytest

from autolux.lighting import mvr
from cli import starfire_assets as assets
from cli import starfire_protocol as protocol
from cli.starfire import DEFAULT_AUTOLOOPS, DEFAULT_RIG, ROOT
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

GDTF_XML = (Path(__file__).parents[2] / 'autolux/tests/data/gdtf/dimmer.xml').read_text(
    encoding='utf-8'
)
SPEC = 'Autolux@Test Dimmer.gdtf'


@pytest.fixture
def login_assets(tmp_path, write_rig):
    profiles = tmp_path / 'profiles.json'
    profiles.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'profiles': {'test_mover': {'gdtf_xml': GDTF_XML, 'dmx_mode': '16 bit'}},
            }
        ),
        encoding='utf-8',
    )
    rig = write_rig(profiles, 'front', 'movers', (180.0, 0.0, 0.0), invert_pan=True)
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
    assert login.protocol_minor == wire.PROTOCOL_MINOR
    assert login.artnet_destination == '127.0.0.1:6454'
    assert login.parameters == [parameter]
    fixture = login.rig.fixtures[0]
    assert fixture.fixture_id == 'front'
    assert fixture.profile_id == f'{SPEC}#16 bit'
    assert fixture.pan.inverted
    assert (fixture.pan.minimum, fixture.pan.maximum) == (0, 1)
    assert not fixture.HasField('tilt')
    assert list(fixture.matrix[:3]) == [-1, 0, 0]
    assert login.rig.groups[0].group_id == 'movers'
    assert login.rig.groups[0].fixture_ids == ['front']
    [profile] = login.rig.profiles
    assert profile.gdtf_xml == GDTF_XML
    assert profile.dmx_mode == '16 bit'
    script = login.autoloops[0]
    assert script.autoloop_id == 'atmospheric/blue'
    assert script.bank == 'atmospheric'
    assert script.lua_source == (autoloops / 'atmospheric/blue.lua').read_bytes().decode('utf-8')
    login.command_id = protocol.MAX_COMMAND_ID
    encoded = protocol.encode_command(login)
    assert wire.Login.FromString(encoded[protocol.FRAME_HEADER.size :]) == login


def test_room_assets_fit_one_login_without_losing_profile_definitions_or_scripts():
    login = assets.load_login(DEFAULT_RIG, DEFAULT_AUTOLOOPS, '127.0.0.1:6454', run_id=1)
    scene = mvr.read(DEFAULT_RIG.read_bytes())
    fixtures = mvr.patched_fixtures(scene)
    assert [fixture.fixture_id for fixture in login.rig.fixtures] == [f['name'] for f in fixtures]
    assert [list(fixture.matrix) for fixture in login.rig.fixtures] == [
        f['matrix'] for f in fixtures
    ]
    for profile in login.rig.profiles:
        spec, mode = profile.profile_id.split('#')
        assert profile.gdtf_xml == mvr.gdtf_description(scene, spec)
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


@pytest.mark.parametrize('name', ['room', 'generic'])
def test_committed_native_rig_is_the_exported_login_rig(name):
    rig = ROOT / f'rigs/{name}.mvr'
    login = assets.load_login(rig, DEFAULT_AUTOLOOPS, '127.0.0.1:6454', run_id=1)
    with assets.native_rig(rig) as exported:
        content = exported.read_bytes()
    assert content == (ROOT / f'rigs/{name}.rig.pb').read_bytes(), (
        f'Regenerate with: uv run autolux starfire export-rig rigs/{name}.mvr rigs/{name}.rig.pb'
    )
    assert rig_wire.RigDefinition.FromString(content) == login.rig


def test_login_carries_the_palette_table_and_palette_commands_round_trip():
    login = assets.load_login(DEFAULT_RIG, DEFAULT_AUTOLOOPS, '127.0.0.1:6454', run_id=1)
    assert [palette.name for palette in login.palettes][:2] == ['techno', 'techno_breakdown']
    techno = login.palettes[0]
    assert (techno.secondary.red, techno.secondary.green, techno.secondary.blue) == (
        1.0,
        0.55,
        0.1,
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
