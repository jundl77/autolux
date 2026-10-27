import copy
import io
import zipfile
from pathlib import Path

import pymvr
import pytest

from autolux.lighting import mvr
from autolux.lighting.fixture_setup import load_rig, require_renderable
from autolux.lighting.gdtf import fixture_library
from autolux.lighting.mvr_schema import dmx_address
from autolux.lighting.mvr_validation import validate
from cli.starfire_assets import rig_definition

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).parent / 'data'
LIBRARY = fixture_library(ROOT / 'rig/library') | fixture_library(
    ROOT / 'autolux/tests/data/generic_rig'
)
DIMMER_SPEC = 'Generic@Test Dimmer@1.0.gdtf'
HANGING_HALF_TURN = [-1, 0, 0, 0, -1, 0, 0, 0, 1, 0, 0, 0]
STANDING = [1, 0, 0, 0, -1, 0, 0, 0, -1, 0, 0, 0]
FIXTURE_UUID = 'uuid="44444444-0000-4000-8000-000000000001" name="Dim 1"'
WASH_CLASSING = (
    '<Classing>11111111-0000-4000-8000-000000000004</Classing>\n                <GDTFSpec>'
)


def sample_text() -> str:
    return (DATA / 'mvr/sample_scene.xml').read_text(encoding='utf-8')


def zipped(entries: dict) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as package:
        for name, content in entries.items():
            package.writestr(name, content)
    return output.getvalue()


def sample_archive(scene_xml: str | None = None, *, drop: str | None = None, extra=None) -> bytes:
    resources = {
        mvr.SCENE_ENTRY: scene_xml or sample_text(),
        DIMMER_SPEC: mvr.gdtf_package((DATA / 'gdtf/dimmer.xml').read_text(encoding='utf-8')),
        'pipe.glb': b'glTF',
        'target.3ds': b'3ds',
        'gobo.png': b'png',
        'movie.mov': b'mov',
        **(extra or {}),
    }
    return zipped({name: content for name, content in resources.items() if name != drop})


def edited_sample(old: str, new: str, **options) -> mvr.Scene:
    text = sample_text()
    assert old in text
    return mvr.read(sample_archive(text.replace(old, new, 1), **options))


def node(draft, tag, name=None):
    return next(
        item
        for item in mvr._nodes(draft)
        if item['tag'] == tag and (name is None or item['attributes'].get('name') == name)
    )


def child(item, tag):
    return next(entry for entry in item['children'] if entry['tag'] == tag)


def comparable(draft):
    stripped = mvr._without_keys(draft)
    for attribute in ('provider', 'providerVersion'):
        stripped['scene']['attributes'].pop(attribute)
    return stripped


@pytest.mark.parametrize(
    'rotation,expected',
    [((yaw, 0, 0), (canonical, 0, 0)) for yaw, canonical in [(0, 0), (180, 180), (-180, 180)]]
    + [((yaw, 0, 0), (canonical, 0, 0)) for yaw, canonical in [(-90, -90), (270, -90), (540, 180)]]
    + [((-450, 0, 0), (-90, 0, 0))]
    + [(rotation, rotation) for rotation in [(30, 20, -40), (-120, -75, 170), (90, 45, 0)]]
    + [((60, 90, 0), (60, 90, 0))],
)
def test_matrix_decomposes_to_canonical_yaw_pitch_roll(rotation, expected):
    matrix = mvr.compose_matrix([0, 0, 0], rotation)
    assert mvr.rotation_degrees(matrix) == pytest.approx(expected, abs=1e-4)


def test_composed_matrix_keeps_position_and_exact_half_turn_is_positive():
    assert mvr.compose_matrix([1, 2, 3], [30, 20, -40])[9:] == [1, 2, 3]
    assert mvr.rotation_degrees(HANGING_HALF_TURN) == (180.0, 0.0, 0.0)


@pytest.mark.parametrize('matrix,reversed', [(HANGING_HALF_TURN, False), (STANDING, True)])
def test_hanging_and_standing_heads_differ_only_in_direction(matrix, reversed):
    assert mvr.home_orientation(matrix) == {
        'pan_offset_degrees': 180.0,
        'tilt_offset_degrees': 0.0,
        'reversed': reversed,
    }


@pytest.mark.parametrize('pitch,accepted', [(60, True), (-60, True), (60.5, False), (-75, False)])
def test_tilt_home_is_limited_to_near_level_bases(pitch, accepted):
    matrix = mvr.compose_matrix([0, 0, 0], [0, pitch, 0])
    if accepted:
        assert mvr.home_orientation(matrix)['tilt_offset_degrees'] == pytest.approx(pitch)
    else:
        with pytest.raises(ValueError, match='exceeds'):
            mvr.home_orientation(matrix)


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


@pytest.mark.parametrize(
    'text,expected',
    [('1', (0, 1)), ('512', (0, 512)), ('513', (1, 1)), ('2.7', (1, 7))]
    + [('32768.512', (32767, 512))]
    + [(text, None) for text in ['0', '-4', '1.0', '1.513', '0.1', '32769.1', 'x', '', '1.2.3']],
)
def test_addresses_resolve_to_artnet_universe_and_address_within_range(text, expected):
    assert dmx_address(text) == expected


def test_room_rig_round_trips_to_identical_bytes_and_is_renderable():
    content = (ROOT / 'rig/room.mvr').read_bytes()
    scene, fixtures = load_rig(ROOT / 'rig/room.mvr')
    assert validate(scene) == []
    assert len(fixtures) == 8
    assert mvr.groups(fixtures)['movers'] == ['spot_left', 'spot_right']
    first = mvr.draft(scene)
    saved = mvr.write(mvr.apply_draft(scene, first, LIBRARY))
    assert saved == content
    assert mvr._without_keys(mvr.draft(mvr.read(saved))) == mvr._without_keys(first)


def room_layers(move: dict[str, str] | None = None, rename: dict[str, str] | None = None):
    """The room scene with fixtures moved between layers and layers renamed."""
    scene = mvr.read((ROOT / 'rig/room.mvr').read_bytes())
    layers = {layer.get('name'): layer for layer in scene.root.iter('Layer')}
    for name, target in (move or {}).items():
        fixture = next(node for node in scene.root.iter('Fixture') if node.get('name') == name)
        source = next(layer for layer in layers.values() if fixture in layer.find('ChildList'))
        source.find('ChildList').remove(fixture)
        layers[target].find('ChildList').append(fixture)
    for old, new in (rename or {}).items():
        layers[old].set('name', new)
    return mvr.read(mvr.write(scene))


def test_room_groups_take_their_role_from_the_mvr_layer_and_round_trip():
    scene = room_layers()
    assert [layer.get('name') for layer in scene.root.iter('Layer')] == ['loop', 'accent']
    roles, errors, warnings = mvr.group_roles(mvr.patched_fixtures(scene))
    assert (roles, errors, warnings) == (
        {'movers': 'loop', 'wash': 'loop', 'strip': 'loop', 'fx': 'accent'},
        [],
        [],
    )
    groups = {group.group_id: group.role for group in rig_definition(ROOT / 'rig/room.mvr').groups}
    assert groups == {'movers': 0, 'wash': 0, 'strip': 0, 'fx': 1}
    content = mvr.write(scene)
    assert mvr.write(mvr.read(content)) == content == (ROOT / 'rig/room.mvr').read_bytes()


def test_a_class_split_across_layers_is_refused():
    scene = room_layers(move={'wash_left': 'accent'})
    with pytest.raises(ValueError, match='Group wash spans the layers accent, loop'):
        require_renderable(scene)


def test_a_group_outside_the_role_layers_defaults_to_loop_with_a_warning():
    roles, errors, warnings = mvr.group_roles(
        mvr.patched_fixtures(room_layers(rename={'accent': 'Room'}))
    )
    assert roles['fx'] == 'loop' and errors == []
    assert warnings == ["Group fx sits in layer 'Room', not loop or accent; treating it as loop."]


def test_a_new_fixture_joins_the_layer_of_its_class():
    scene = room_layers()
    mvr.add_fixture(
        scene,
        name='kinta_2',
        gdtf_spec='Chauvet DJ@Mini Kinta IRC@1.0.gdtf',
        gdtf_mode='4ch',
        universe=0,
        address=200,
        fixture_id=9,
        fixture_class='fx',
    )
    fixtures = mvr.patched_fixtures(scene)
    assert {f['name']: f['layer'] for f in fixtures}['kinta_2'] == 'accent'
    assert mvr.group_roles(fixtures)[0]['fx'] == 'accent'


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


def test_sample_patch_composes_nested_matrices_and_includes_multipatch_fixtures():
    fixtures = require_renderable(mvr.read(sample_archive()))
    assert [(f['name'], f['universe'], f['address'], f['class']) for f in fixtures] == [
        ('Dim 1', 1, 5, 'wash'),
        ('Dim 1 multipatch', 0, 20, ''),
    ]
    assert fixtures[0]['matrix'][9:] == pytest.approx([0, 2500, 1000])
    assert mvr.rotation_degrees(fixtures[0]['matrix'])[0] == pytest.approx(90)


def test_unpatched_fixtures_and_unreferenced_gdtf_files_are_planning_data():
    scene = edited_sample(
        '<Addresses><Address break="0">20</Address></Addresses>',
        '',
        extra={'unused.gdtf': zipped({'description.xml': 'invalid until selected'})},
    )
    assert [f['name'] for f in require_renderable(scene)] == ['Dim 1']


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
            FIXTURE_UUID.replace('44444444-0000-4000-8000-000000000001', str(mvr.uuid.UUID(int=0))),
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
    errors = validate(edited_sample(old, new))
    assert any(message in error for error in errors), errors


@pytest.mark.parametrize(
    'drop,message',
    [
        ('pipe.glb', 'file pipe.glb is not in the MVR archive'),
        (DIMMER_SPEC, f'GDTFSpec {DIMMER_SPEC} is not in the MVR archive'),
    ],
)
def test_missing_resources_are_reported(drop, message):
    errors = validate(mvr.read(sample_archive(drop=drop)))
    assert any(message in error for error in errors), errors


@pytest.mark.parametrize(
    'content,message',
    [
        (lambda: b'not a zip', 'Invalid MVR archive'),
        (
            lambda: sample_archive(sample_text().replace('verMajor="1"', 'verMajor="2"')),
            'verMajor 1',
        ),
        (
            lambda: zipped({mvr.SCENE_ENTRY: sample_text(), '../escape.gdtf': b''}),
            'unsafe or duplicate',
        ),
        (
            lambda: zipped({mvr.SCENE_ENTRY: sample_text(), 'A.glb': b'', 'a.glb': b''}),
            'unsafe or duplicate',
        ),
    ],
)
def test_unsafe_archives_are_rejected(content, message):
    with pytest.raises(ValueError, match=message):
        mvr.read(content())


def channel_function(draft):
    return node(draft['gdtf'][DIMMER_SPEC], 'ChannelFunction')


def test_gdtf_range_edit_gains_a_revision_and_survives_a_reload():
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
    unchanged = mvr.apply_draft(scene, mvr.draft(scene), LIBRARY)
    assert unchanged.entries[DIMMER_SPEC][1] == scene.entries[DIMMER_SPEC][1]


@pytest.mark.parametrize(
    'change,message',
    [
        (lambda item: item['attributes'].update(Attribute='Pan'), 'read-only'),
        (lambda item: item['attributes'].update(DMXFrom='300'), 'DMXValue'),
        (lambda item: item['attributes'].update(Name='Dimmer.Fine'), 'GDTF Name'),
        (lambda item: item['children'].append(copy.deepcopy(item)), 'structure is read-only'),
    ],
)
def test_gdtf_edits_are_limited_to_correctable_fields(change, message):
    scene = mvr.read(sample_archive())
    attempt = mvr.draft(scene)
    change(channel_function(attempt))
    with pytest.raises(ValueError, match=message):
        mvr.apply_draft(scene, attempt, LIBRARY)


def test_library_gdtf_is_embedded_when_a_draft_starts_using_it():
    scene = mvr.read((ROOT / 'rig/room.mvr').read_bytes())
    edited = mvr.draft(scene)
    spot = node(edited['scene'], 'Fixture', 'spot_left')
    child(spot, 'GDTFSpec')['text'] = 'Generic@Dimmer@1.0.gdtf'
    child(spot, 'GDTFMode')['text'] = '1ch'
    saved = mvr.apply_draft(scene, edited, LIBRARY)
    assert 'Generic@Dimmer@1.0.gdtf' in saved.entries
    assert require_renderable(saved)[0]['gdtf_spec'] == 'Generic@Dimmer@1.0.gdtf'


def test_the_room_rig_written_by_pymvr_reads_back_to_the_same_patch_and_dmx(tmp_path):
    shipped = ROOT / 'rig/room.mvr'
    scene = mvr.read(shipped.read_bytes())
    source = pymvr.GeneralSceneDescription(io.BytesIO(shipped.read_bytes()))
    writer = pymvr.GeneralSceneDescriptionWriter()
    writer.serialize_scene(source.scene)
    writer.serialize_user_data(getattr(source, 'user_data', None))
    for spec, (_, content) in scene.entries.items():
        archive = tmp_path / spec
        archive.write_bytes(content)
        writer.files_list.append((str(archive), spec))
    rewritten = tmp_path / 'room.mvr'
    writer.write_mvr(str(rewritten))
    _, fixtures = load_rig(rewritten)
    _, original = load_rig(shipped)
    assert fixtures == original
    assert mvr.groups(fixtures) == mvr.groups(original)
    exported = rig_definition(rewritten).SerializeToString()
    assert exported == (ROOT / 'rig/room.rig.pb').read_bytes()
