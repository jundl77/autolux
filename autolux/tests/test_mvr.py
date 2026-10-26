"""MVR archives, spec validation and orientation must preserve every rig fact."""

import copy
import io
import zipfile
from pathlib import Path

import pytest

from autolux.lighting import mvr
from autolux.lighting.fixture_setup import load_rig, require_renderable
from autolux.lighting.gdtf import fixture_library
from autolux.lighting.mvr_schema import dmx_address
from autolux.lighting.mvr_validation import validate

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).parent / 'data'
LIBRARY = fixture_library(ROOT / 'rigs/profiles.json')
DIMMER_SPEC = 'Autolux@Test Dimmer.gdtf'


def sample_text() -> str:
    return (DATA / 'mvr/sample_scene.xml').read_text(encoding='utf-8')


def sample_archive(scene_xml: str | None = None, *, drop: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as package:
        package.writestr(mvr.SCENE_ENTRY, scene_xml or sample_text())
        resources = {
            DIMMER_SPEC: mvr.gdtf_package((DATA / 'gdtf/dimmer.xml').read_text(encoding='utf-8')),
            'pipe.glb': b'glTF',
            'target.3ds': b'3ds',
            'gobo.png': b'png',
            'movie.mov': b'mov',
        }
        for name, content in resources.items():
            if name != drop:
                package.writestr(name, content)
    return output.getvalue()


def node(draft, tag, name=None):
    return next(
        item
        for item in mvr._nodes(draft['scene'])
        if item['tag'] == tag and (name is None or item['attributes'].get('name') == name)
    )


def child(item, tag):
    return next(entry for entry in item['children'] if entry['tag'] == tag)


def comparable(draft):
    stripped = mvr._without_keys(draft)
    for attribute in ('provider', 'providerVersion'):
        stripped['scene']['attributes'].pop(attribute)
    return stripped


# ------------------------------------------------------------------ orientation


@pytest.mark.parametrize(
    'yaw,expected',
    [(0, 0), (180, 180), (-180, 180), (-90, -90), (270, -90), (540, 180), (-450, -90)],
)
def test_matrix_yaw_is_the_pan_offset_and_wraps_to_one_canonical_turn(yaw, expected):
    matrix = mvr.compose_matrix([0, 0, 0], [yaw, 0, 0])
    assert mvr.rotation_degrees(matrix)[0] == pytest.approx(expected)


def test_exact_half_turn_matrix_selects_positive_180_not_minus_180():
    # atan2(-0.0, -1) is -180; that would pick different DMX on a 540-degree head.
    assert mvr.rotation_degrees([-1, 0, 0, 0, -1, 0, 0, 0, 1, 0, 0, 0]) == (180.0, 0.0, 0.0)


@pytest.mark.parametrize('rotation', [(30, 20, -40), (-120, -75, 170), (90, 45, 0)])
def test_yaw_pitch_roll_round_trip_through_the_matrix(rotation):
    matrix = mvr.compose_matrix([1, 2, 3], rotation)
    assert mvr.rotation_degrees(matrix) == pytest.approx(rotation, abs=1e-4)
    assert matrix[9:] == [1, 2, 3]


def test_gimbal_lock_keeps_the_heading_in_yaw():
    assert mvr.rotation_degrees(mvr.compose_matrix([0, 0, 0], [60, 90, 0])) == pytest.approx(
        (60, 90, 0), abs=1e-4
    )


@pytest.mark.parametrize(
    'matrix',
    [
        [1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0],
        [-1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
        [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
    ],
)
def test_matrices_without_a_rotation_are_rejected(matrix):
    with pytest.raises(ValueError, match='Matrix'):
        mvr.rotation_degrees(matrix)


def test_nested_objects_compose_parent_matrices_into_scene_space():
    fixtures = {f['name']: f for f in mvr.patched_fixtures(mvr.read(sample_archive()))}
    placed = fixtures['Dim 1']['matrix']
    # The layer lifts by 1000 mm; the group turns 90 degrees and sits at y=2000.
    assert placed[9:] == pytest.approx([0, 2500, 1000])
    assert mvr.rotation_degrees(placed)[0] == pytest.approx(90)


# ------------------------------------------------------------------ addresses


@pytest.mark.parametrize(
    'text,expected',
    [
        ('1', (0, 1)),
        ('512', (0, 512)),
        ('513', (1, 1)),
        ('2.7', (1, 7)),
        ('32768.512', (32767, 512)),
    ],
)
def test_absolute_and_dotted_addresses_resolve_to_artnet_universe_and_address(text, expected):
    assert dmx_address(text) == expected


@pytest.mark.parametrize('text', ['0', '-4', '1.0', '1.513', '0.1', '32769.1', 'x', '', '1.2.3'])
def test_out_of_range_addresses_are_rejected(text):
    assert dmx_address(text) is None


# ------------------------------------------------------------------ round trips


def test_room_rig_round_trips_to_an_identical_model_and_identical_bytes():
    content = (ROOT / 'rigs/room.mvr').read_bytes()
    scene = mvr.read(content)
    first = mvr.draft(scene)
    saved = mvr.write(mvr.apply_draft(scene, first, LIBRARY))
    assert saved == content
    assert mvr._without_keys(mvr.draft(mvr.read(saved))) == mvr._without_keys(first)


def test_third_party_sample_round_trips_every_node_and_resource():
    scene = mvr.read(sample_archive())
    assert validate(scene) == []
    first = mvr.draft(scene)
    saved = mvr.write(mvr.apply_draft(scene, first, LIBRARY))
    reread = mvr.read(saved)
    assert validate(reread) == []
    assert comparable(mvr.draft(reread)) == comparable(first)
    assert {name: data for name, (_, data) in reread.entries.items()} == {
        name: data for name, (_, data) in scene.entries.items()
    }
    assert reread.root.get('provider') == mvr.PROVIDER
    assert reread.root.find('UserData/Data/Layout/Note').text == 'kept verbatim'
    assert mvr.write(mvr.apply_draft(reread, mvr.draft(reread), LIBRARY)) == saved


def test_sample_patch_includes_nested_and_multipatch_fixtures():
    fixtures = require_renderable(mvr.read(sample_archive()))
    assert [(f['name'], f['universe'], f['address'], f['class']) for f in fixtures] == [
        ('Dim 1', 1, 5, 'wash'),
        ('Dim 1 multipatch', 0, 20, ''),
    ]


def test_room_rig_is_spec_conforming_and_renderable():
    scene, fixtures = load_rig(ROOT / 'rigs/room.mvr')
    assert validate(scene) == []
    assert len(fixtures) == 14
    assert mvr.groups(fixtures)['movers'] == ['spot_left', 'spot_right']


# ------------------------------------------------------------------ validation

FIXTURE_UUID = 'uuid="44444444-0000-4000-8000-000000000001" name="Dim 1"'
WASH_CLASSING = (
    '<Classing>11111111-0000-4000-8000-000000000004</Classing>\n                <GDTFSpec>'
)


@pytest.mark.parametrize(
    'old,new,message',
    [
        (FIXTURE_UUID, 'name="Dim 1"', 'uuid is required'),
        ('<FixtureIDNumeric>101</FixtureIDNumeric>', '', 'needs exactly one FixtureIDNumeric'),
        (
            '<UnitNumber>1</UnitNumber>',
            '<UnitNumber>one</UnitNumber>',
            'UnitNumber must be a base-10',
        ),
        (
            '<Address break="0">2.5</Address>',
            '<Address break="0">2.513</Address>',
            'absolute address',
        ),
        ('<GDTFMode>16 bit</GDTFMode>', '<GDTFMode>32 bit</GDTFMode>', "'32 bit' does not exist"),
        ('<Address break="0">2.5</Address>', '<Address break="1">2.5</Address>', 'no DMXBreak 2'),
        (
            '<Address break="0">2.5</Address>',
            '<Address break="0">2.5</Address><Address break="0">3.5</Address>',
            'break values must be unique',
        ),
        ('{0,1,0}{-1,0,0}{0,0,1}{0,2000,0}', '{0,1,0}{-1,0,0}{0,0,1}', 'four {x,y,z} rows'),
        (
            WASH_CLASSING,
            WASH_CLASSING.replace('000000000004', '0000000000FF'),
            'does not resolve to a Class',
        ),
        (
            '<Focus>55555555-0000-4000-8000-000000000001</Focus>',
            '<Focus>11111111-0000-4000-8000-000000000004</Focus>',
            'is a Class, not a FocusPoint',
        ),
        (
            'uuid="55555555-0000-4000-8000-000000000001" name="Centre"',
            'uuid="44444444-0000-4000-8000-000000000001" name="Centre"',
            'uuid duplicates',
        ),
        (
            FIXTURE_UUID,
            FIXTURE_UUID.replace(
                '44444444-0000-4000-8000-000000000001', '0' * 8 + '-0000-0000-0000-' + '0' * 12
            ),
            'non-nil UUID',
        ),
        ('<CastShadow>true</CastShadow>', '<CastShadow>yes</CastShadow>', '"true" or "false"'),
        ('<Color>0.314303,0.328065,87.699166</Color>', '<Color>0.3,0.3</Color>', 'CIE xyY'),
        ('ipv4="192.168.11.5"', 'ipv4="192.168.11.500"', 'IPv4'),
        ('transmission="Unicast"', 'transmission="Sometimes"', 'one of Unicast'),
        ('<ChainLength>3500.5</ChainLength>', '', 'needs exactly one ChainLength'),
        (
            '<Function>Key light</Function>',
            '<Function>Key light</Function><Wiggle/>',
            'Wiggle is not allowed',
        ),
        ('name="Truss 1"', 'name="Truss 1" colour="red"', 'attribute colour is not defined'),
        (
            '<UnitNumber>2</UnitNumber>',
            '<UnitNumber>2</UnitNumber><FixtureID>9</FixtureID>',
            'multipatch children may not define FixtureID',
        ),
        ('<GDTFMode>16 bit</GDTFMode>', '', 'GDTFMode is mandatory when GDTFSpec'),
        ('<Gobo rotation="32.5">', '<Gobo rotation="fast">', 'rotation must be a finite'),
    ],
)
def test_spec_violations_are_reported_precisely(old, new, message):
    text = sample_text()
    assert old in text
    errors = validate(mvr.read(sample_archive(text.replace(old, new, 1))))
    assert any(message in error for error in errors), errors


def test_missing_resources_are_reported():
    errors = validate(mvr.read(sample_archive(drop='pipe.glb')))
    assert any('file pipe.glb is not in the MVR archive' in error for error in errors), errors
    errors = validate(mvr.read(sample_archive(drop=DIMMER_SPEC)))
    assert any(f'GDTFSpec {DIMMER_SPEC} is not in the MVR archive' in error for error in errors)


def test_unsafe_archives_are_rejected():
    for content, message in [
        (b'not a zip', 'Invalid MVR archive'),
        (sample_archive(sample_text().replace('verMajor="1"', 'verMajor="2"')), 'verMajor 1'),
    ]:
        with pytest.raises(ValueError, match=message):
            mvr.read(content)
    for extra in (('../escape.gdtf',), ('A.glb', 'a.glb')):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as package:
            package.writestr(mvr.SCENE_ENTRY, sample_text())
            for name in extra:
                package.writestr(name, b'')
        with pytest.raises(ValueError, match='unsafe or duplicate'):
            mvr.read(output.getvalue())


# ------------------------------------------------------------------ GDTF editing


def channel_function(draft):
    return next(
        item for item in mvr._nodes(draft['gdtf'][DIMMER_SPEC]) if item['tag'] == 'ChannelFunction'
    )


def test_gdtf_edits_are_limited_to_correctable_fields_and_add_a_revision():
    scene = mvr.read(sample_archive())
    edited = mvr.draft(scene)
    channel_function(edited)['attributes']['PhysicalTo'] = '95.5'
    saved = mvr.apply_draft(scene, edited, LIBRARY)
    description = mvr.gdtf_description(saved, DIMMER_SPEC)
    assert 'PhysicalTo="95.5"' in description
    assert f'Text="{mvr.REVISION_TEXT}"' in description
    assert validate(saved) == []
    reread = mvr.read(mvr.write(saved))
    assert channel_function(mvr.draft(reread))['attributes']['PhysicalTo'] == '95.5'

    for change, message in [
        (lambda item: item['attributes'].update(Attribute='Pan'), 'read-only'),
        (lambda item: item['attributes'].update(DMXFrom='300'), 'DMXValue'),
        (lambda item: item['attributes'].update(Name='Dimmer.Fine'), 'GDTF Name'),
        (lambda item: item['children'].append(copy.deepcopy(item)), 'structure is read-only'),
    ]:
        attempt = mvr.draft(scene)
        change(channel_function(attempt))
        with pytest.raises(ValueError, match=message):
            mvr.apply_draft(scene, attempt, LIBRARY)


def test_unchanged_gdtf_packages_are_kept_byte_for_byte():
    scene = mvr.read(sample_archive())
    saved = mvr.apply_draft(scene, mvr.draft(scene), LIBRARY)
    assert saved.entries[DIMMER_SPEC][1] == scene.entries[DIMMER_SPEC][1]


def test_library_gdtf_is_embedded_when_a_draft_starts_using_it():
    scene = mvr.read((ROOT / 'rigs/room.mvr').read_bytes())
    edited = mvr.draft(scene)
    spot = node(edited, 'Fixture', 'spot_left')
    child(spot, 'GDTFSpec')['text'] = 'Autolux@generic_dimmer.gdtf'
    child(spot, 'GDTFMode')['text'] = '1ch'
    saved = mvr.apply_draft(scene, edited, LIBRARY)
    assert 'Autolux@generic_dimmer.gdtf' in saved.entries
    assert require_renderable(saved)[0]['gdtf_spec'] == 'Autolux@generic_dimmer.gdtf'
