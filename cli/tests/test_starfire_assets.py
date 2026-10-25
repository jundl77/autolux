import json
from pathlib import Path

import pytest

from cli import starfire_assets as assets
from cli import starfire_protocol as protocol
from cli.starfire import DEFAULT_AUTOLOOPS, DEFAULT_RIG, PROFILE_CATALOGUE
from proto.starfire import starfire_pb2 as wire

GDTF_XML = (Path(__file__).parents[2] / 'autolux/tests/data/gdtf/dimmer.xml').read_text(
    encoding='utf-8'
)


@pytest.fixture
def login_assets(tmp_path, monkeypatch):
    rig = tmp_path / 'rig.json'
    rig.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'fixtures': [
                    {
                        'id': 'front',
                        'profile': 'test_mover',
                        'universe': 0,
                        'address': 1,
                        'pan': {'min': 0, 'inverted': True},
                    }
                ],
                'groups': {'movers': ['front']},
            }
        ),
        encoding='utf-8',
    )
    profiles = tmp_path / 'profiles.json'
    profiles.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'profiles': {
                    'test_mover': {
                        'gdtf_xml': GDTF_XML,
                        'dmx_mode': '16 bit',
                    },
                    'unused': {'source': 'Not installed'},
                },
            }
        ),
        encoding='utf-8',
    )
    monkeypatch.setattr(assets, 'PROFILE_CATALOGUE', profiles)
    autoloops = tmp_path / 'autoloops'
    bank = autoloops / 'atmospheric'
    bank.mkdir(parents=True)
    (bank / 'blue.lua').write_text('return { render = function(ctx) end }\n', encoding='utf-8')
    return rig, autoloops, profiles


def test_login_preserves_exact_gdtf_source_and_explicit_zero_movement_limit(login_assets):
    rig, autoloops, _ = login_assets
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
    assert fixture.pan.HasField('minimum')
    assert fixture.pan.minimum == 0
    assert not fixture.pan.HasField('maximum')
    assert fixture.pan.inverted
    assert login.rig.groups[0].fixture_ids == ['front']
    assert len(login.rig.profiles) == 1
    profile = login.rig.profiles[0]
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
    rig = json.loads(DEFAULT_RIG.read_text(encoding='utf-8'))
    profiles = json.loads(PROFILE_CATALOGUE.read_text(encoding='utf-8'))['profiles']
    assert [fixture.fixture_id for fixture in login.rig.fixtures] == [
        fixture['id'] for fixture in rig['fixtures']
    ]
    assert {profile.profile_id for profile in login.rig.profiles} == {
        fixture['profile'] for fixture in rig['fixtures']
    }
    for profile in login.rig.profiles:
        source = profiles[profile.profile_id]
        assert profile.gdtf_xml == source['gdtf_xml']
        assert profile.dmx_mode == source['dmx_mode']
    assert {script.autoloop_id for script in login.autoloops} == {
        path.relative_to(DEFAULT_AUTOLOOPS).with_suffix('').as_posix()
        for path in DEFAULT_AUTOLOOPS.glob('*/*.lua')
    }
    for script in login.autoloops:
        assert script.lua_source == (
            DEFAULT_AUTOLOOPS / f'{script.autoloop_id}.lua'
        ).read_bytes().decode('utf-8')
    assert login.ByteSize() + protocol.COMMAND_ID_MAX_ENCODED_BYTES <= protocol.MAX_MESSAGE_BYTES


def test_login_carries_rig_orientation_offsets(login_assets):
    rig, autoloops, _ = login_assets
    data = json.loads(rig.read_text(encoding='utf-8'))
    data['fixtures'][0].update(pan_offset_degrees=180, tilt_offset_degrees=-12.5)
    rig.write_text(json.dumps(data), encoding='utf-8')
    fixture = assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1).rig.fixtures[0]
    assert (fixture.pan_offset_degrees, fixture.tilt_offset_degrees) == (180, -12.5)


@pytest.mark.parametrize(
    'field,value',
    [('pan_offset_degrees', 541), ('tilt_offset_degrees', 270.5), ('pan_offset_degrees', None)],
)
def test_invalid_orientation_offset_is_not_sent_to_renderer(login_assets, field, value):
    rig, autoloops, _ = login_assets
    data = json.loads(rig.read_text(encoding='utf-8'))
    data['fixtures'][0][field] = value
    rig.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


@pytest.mark.parametrize('location', ['root', 'fixture', 'movement', 'profile'])
def test_unknown_fields_are_not_silently_dropped(login_assets, location):
    rig, autoloops, profiles = login_assets
    path = profiles if location == 'profile' else rig
    data = json.loads(path.read_text(encoding='utf-8'))
    if location == 'root':
        target = data
    elif location == 'fixture':
        target = data['fixtures'][0]
    elif location == 'movement':
        target = data['fixtures'][0]['pan']
    else:
        target = data['profiles']['test_mover']
    target['typo'] = None
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


@pytest.mark.parametrize(
    'document',
    [
        '[]',
        '{"schema_version":1,"schema_version":1,"fixtures":[]}',
        '{"schema_version":true,"fixtures":[]}',
        '{"schema_version":1,"fixtures":[],"groups":{"movers":"front"}}',
    ],
)
def test_invalid_json_shapes_and_duplicate_fields_are_rejected(login_assets, document):
    rig, autoloops, _ = login_assets
    rig.write_text(document, encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


@pytest.mark.parametrize('field,value', [('gdtf_xml', ''), ('gdtf_xml', 5), ('dmx_mode', None)])
def test_invalid_gdtf_payload_is_not_coerced(login_assets, field, value):
    rig, autoloops, profiles = login_assets
    data = json.loads(profiles.read_text(encoding='utf-8'))
    data['profiles']['test_mover'][field] = value
    profiles.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def test_login_uses_selected_profile_catalogue(login_assets, monkeypatch):
    rig, autoloops, profiles = login_assets
    monkeypatch.setattr(assets, 'PROFILE_CATALOGUE', profiles.with_name('missing.json'))
    login = assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1, profiles=profiles)
    assert login.rig.profiles[0].gdtf_xml == GDTF_XML


def test_combined_login_limit_is_checked_before_the_renderer_is_started(login_assets):
    rig, autoloops, _ = login_assets
    (autoloops / 'atmospheric/blue.lua').write_text(
        ' ' * protocol.MAX_MESSAGE_BYTES, encoding='utf-8'
    )
    with pytest.raises(ValueError, match='Login assets exceed the renderer message limit'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)


def test_invalid_autoloop_name_is_rejected(login_assets):
    rig, autoloops, _ = login_assets
    (autoloops / 'atmospheric/blue.lua').rename(autoloops / 'atmospheric/Blue Sky.lua')
    with pytest.raises(ValueError, match='Invalid autoloop name'):
        assets.load_login(rig, autoloops, '127.0.0.1:6454', run_id=1)
