import pytest

from autolux.lighting import mvr
from autolux.lighting.fixture_setup import load_rig
from autolux.lighting.gdtf import fixture_library
from tools.rig_json_to_mvr import ROOT, convert

LIBRARY = fixture_library(ROOT / 'rigs/profiles.json')
SPOT = 'chauvet_intimidator_spot_110_12ch'
WASH = 'fun_generation_separ_quad_uv_8ch'
LEGACY_ROOM = {
    'schema_version': 1,
    'fixtures': [
        {
            'id': 'spot_left',
            'profile': SPOT,
            'universe': 0,
            'address': 1,
            'pan_offset_degrees': 180,
        },
        {
            'id': 'spot_right',
            'profile': SPOT,
            'universe': 0,
            'address': 13,
            'pan_offset_degrees': 180,
        },
        *(
            {'id': name, 'profile': WASH, 'universe': 0, 'address': address}
            for name, address in [
                ('wash_left', 65),
                ('wash_mid_left', 73),
                ('wash_mid_right', 81),
                ('wash_right', 89),
            ]
        ),
        *(
            {
                'id': f'cf805_pixel_{index}',
                'profile': 'generic_rgb',
                'universe': 0,
                'address': 94 + 3 * index,
            }
            for index in range(1, 9)
        ),
    ],
    'groups': {
        'movers': ['spot_left', 'spot_right'],
        'wash': ['wash_left', 'wash_mid_left', 'wash_mid_right', 'wash_right'],
        'strip': [f'cf805_pixel_{index}' for index in range(1, 9)],
    },
}


def test_committed_room_mvr_is_the_conversion_of_the_legacy_room_rig():
    assert convert(LEGACY_ROOM, LIBRARY, 'room') == (ROOT / 'rigs/room.mvr').read_bytes()


def test_room_mvr_keeps_patch_groups_profiles_and_half_turn_mover_offsets():
    scene, fixtures = load_rig(ROOT / 'rigs/room.mvr')
    by_profile = {entry['profile_id']: spec for spec, entry in LIBRARY.items()}
    assert [(f['name'], f['gdtf_spec'], f['universe'], f['address']) for f in fixtures] == [
        (f['id'], by_profile[f['profile']], f['universe'], f['address'])
        for f in LEGACY_ROOM['fixtures']
    ]
    assert all(f['gdtf_mode'] == LIBRARY[f['gdtf_spec']]['default_mode'] for f in fixtures)
    assert mvr.groups(fixtures) == LEGACY_ROOM['groups']
    for fixture, legacy in zip(fixtures, LEGACY_ROOM['fixtures'], strict=True):
        yaw, pitch, _ = mvr.rotation_degrees(fixture['matrix'])
        assert (yaw, pitch) == (legacy.get('pan_offset_degrees', 0), 0)
    for spec in {f['gdtf_spec'] for f in fixtures}:
        assert mvr.gdtf_description(scene, spec) == LIBRARY[spec]['gdtf_xml']


def test_movement_limits_and_inversion_survive_conversion():
    rig = {
        'schema_version': 1,
        'fixtures': [
            {
                'id': 'spot',
                'profile': SPOT,
                'universe': 0,
                'address': 1,
                'pan': {'min': 0.25, 'max': 0.75, 'inverted': True},
            }
        ],
    }
    [fixture] = mvr.patched_fixtures(mvr.read(convert(rig, LIBRARY, 'test')))
    assert fixture['pan'] == {'min': 0.25, 'max': 0.75, 'inverted': True}
    assert fixture['tilt'] == {'min': 0.0, 'max': 1.0, 'inverted': False}


def test_fixtures_in_several_groups_cannot_be_expressed_as_one_class():
    rig = {**LEGACY_ROOM, 'groups': {'a': ['spot_left'], 'b': ['spot_left']}}
    with pytest.raises(ValueError, match='one class'):
        convert(rig, LIBRARY, 'room')
