import json

import pytest

from cli import starfire_assets as assets
from cli import starfire_protocol as protocol
from cli.starfire import DEFAULT_RIG, DEFAULT_SEQUENCES, PROFILE_CATALOGUE
from proto.starfire import starfire_pb2 as wire


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
                        'source': 'Fixture manual',
                        'notes': 'Calibrated movement',
                        'safe_values': [0, 0, 0, 0, 128, 0],
                        'rgb': [1, 2, 3],
                        'dimmer': 5,
                        'pan': {'coarse': 4, 'fine': 0},
                    },
                    'unused': {'source': 'Not installed'},
                },
            }
        ),
        encoding='utf-8',
    )
    monkeypatch.setattr(assets, 'PROFILE_CATALOGUE', profiles)
    sequences = tmp_path / 'sequences'
    bank = sequences / 'atmospheric'
    bank.mkdir(parents=True)
    (bank / 'blue.lua').write_text('return { render = function(ctx) end }\n', encoding='utf-8')
    return rig, sequences, profiles


def test_login_preserves_typed_patch_source_and_zero_channel_presence(login_assets):
    rig, sequences, _ = login_assets
    parameter = wire.Parameter(type=wire.OUTPUT_BRIGHTNESS, number=0)
    login = assets.load_login(rig, sequences, '127.0.0.1:6454', [parameter])
    assert login.command_id == 0
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
    assert profile.source == 'Fixture manual'
    assert profile.notes == 'Calibrated movement'
    assert profile.safe_values == bytes([0, 0, 0, 0, 128, 0])
    assert profile.pan.HasField('fine')
    assert profile.pan.fine == 0
    script = login.scripts[0]
    assert script.sequence_id == 'atmospheric/blue'
    assert script.bank == 'atmospheric'
    assert script.lua_source == (sequences / 'atmospheric/blue.lua').read_bytes().decode('utf-8')
    login.command_id = protocol.MAX_COMMAND_ID
    encoded = protocol.encode_command(login)
    assert wire.Login.FromString(encoded[protocol.FRAME_HEADER.size :]) == login


def test_room_assets_fit_one_login_without_losing_profile_definitions_or_scripts():
    login = assets.load_login(DEFAULT_RIG, DEFAULT_SEQUENCES, '127.0.0.1:6454')
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
        assert profile.source == source['source']
        assert profile.notes == source['notes']
        assert profile.safe_values == bytes(source['safe_values'])
    assert {script.sequence_id for script in login.scripts} == {
        path.relative_to(DEFAULT_SEQUENCES).with_suffix('').as_posix()
        for path in DEFAULT_SEQUENCES.glob('*/*.lua')
    }
    for script in login.scripts:
        assert script.lua_source == (
            DEFAULT_SEQUENCES / f'{script.sequence_id}.lua'
        ).read_bytes().decode('utf-8')
    assert login.ByteSize() + protocol.COMMAND_ID_MAX_ENCODED_BYTES <= protocol.MAX_MESSAGE_BYTES


@pytest.mark.parametrize('location', ['root', 'fixture', 'movement', 'profile', 'axis'])
def test_unknown_fields_are_not_silently_dropped(login_assets, location):
    rig, sequences, profiles = login_assets
    path = profiles if location in {'profile', 'axis'} else rig
    data = json.loads(path.read_text(encoding='utf-8'))
    if location == 'root':
        target = data
    elif location == 'fixture':
        target = data['fixtures'][0]
    elif location == 'movement':
        target = data['fixtures'][0]['pan']
    elif location == 'profile':
        target = data['profiles']['test_mover']
    else:
        target = data['profiles']['test_mover']['pan']
    target['typo'] = None
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, sequences, '127.0.0.1:6454')


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
    rig, sequences, _ = login_assets
    rig.write_text(document, encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, sequences, '127.0.0.1:6454')


@pytest.mark.parametrize('channels', [[1, 2], [1, 2, 3, 4], [True, 2, 3]])
def test_rgb_channel_data_is_not_truncated_or_coerced(login_assets, channels):
    rig, sequences, profiles = login_assets
    data = json.loads(profiles.read_text(encoding='utf-8'))
    data['profiles']['test_mover']['rgb'] = channels
    profiles.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid Starfire login assets'):
        assets.load_login(rig, sequences, '127.0.0.1:6454')


def test_combined_login_limit_is_checked_before_the_renderer_is_started(login_assets):
    rig, sequences, _ = login_assets
    (sequences / 'atmospheric/blue.lua').write_text(
        ' ' * protocol.MAX_MESSAGE_BYTES, encoding='utf-8'
    )
    with pytest.raises(ValueError, match='Login assets exceed the renderer message limit'):
        assets.load_login(rig, sequences, '127.0.0.1:6454')


def test_invalid_sequence_name_is_rejected(login_assets):
    rig, sequences, _ = login_assets
    (sequences / 'atmospheric/blue.lua').rename(sequences / 'atmospheric/Blue Sky.lua')
    with pytest.raises(ValueError, match='Invalid sequence name'):
        assets.load_login(rig, sequences, '127.0.0.1:6454')
