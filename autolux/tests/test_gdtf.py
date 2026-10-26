"""GDTF import installs the archive as is and never publishes an unvalidated one."""

import zipfile
from pathlib import Path

import pytest

from autolux.lighting import gdtf, mvr
from autolux.lighting.fixture_setup import FixtureStore, require_renderable

DESCRIPTION = Path(__file__).parent / 'data' / 'gdtf' / 'dimmer.xml'
SPEC = 'Generic@Test Dimmer@1.0.gdtf'


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / 'dimmer.gdtf'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('description.xml', DESCRIPTION.read_bytes())
        package.writestr('models/unused.glb', b'no extraction required')
    return path


def test_import_installs_the_archive_byte_for_byte_after_validating_its_mode(archive, tmp_path):
    original_archive = archive.read_bytes()
    library = tmp_path / 'library'
    validations = []

    def validate(patch):
        assert not (library / SPEC).exists()
        scene = mvr.read(patch.read_bytes())
        [fixture] = require_renderable(scene)
        assert (fixture['gdtf_spec'], fixture['gdtf_mode']) == (SPEC, '16 bit')
        assert scene.entries[SPEC][1] == original_archive
        validations.append(patch)

    metadata = gdtf.import_fixture(archive, library, '16 bit', validate)
    assert metadata['channels'] == 2
    assert archive.read_bytes() == original_archive
    assert (library / SPEC).read_bytes() == original_archive
    assert len(validations) == 1
    assert not validations[0].exists()
    installed = FixtureStore(tmp_path / 'rig.mvr', library, lambda _: None).library()
    assert installed[SPEC]['profile_id'] == 'Test Dimmer'
    assert installed[SPEC]['archive'] == original_archive
    assert installed[SPEC]['default_mode'] == '8 bit'
    assert {mode: entry['channels'] for mode, entry in installed[SPEC]['modes'].items()} == {
        '8 bit': 1,
        '16 bit': 2,
    }


def test_a_library_archive_must_be_named_by_its_gdtf_spec(archive, tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    (library / 'dimmer.gdtf').write_bytes(archive.read_bytes())
    with pytest.raises(ValueError, match=f'dimmer.gdtf describes {SPEC}; name the file {SPEC}'):
        gdtf.fixture_library(library)


ROOT = Path(__file__).parents[2]
SAMPLE_RIG = Path(__file__).parent / 'data' / 'generic_rig'


def test_each_rig_embeds_the_archives_it_names_byte_for_byte():
    library = gdtf.fixture_library(ROOT / 'rig/library')
    sample = library | gdtf.fixture_library(SAMPLE_RIG)
    for rig, available in ((ROOT / 'rig/room.mvr', library), (SAMPLE_RIG / 'generic.mvr', sample)):
        scene = mvr.read(rig.read_bytes())
        for spec in mvr.referenced_specs(scene.root):
            assert scene.entries[spec][1] == available[spec]['archive'], spec


def test_archives_are_named_by_their_manufacturer_fixture_and_revision():
    library = gdtf.fixture_library(ROOT / 'rig/library') | gdtf.fixture_library(SAMPLE_RIG)
    assert {spec: (entry['manufacturer'], entry['name']) for spec, entry in library.items()} == {
        'Chauvet DJ@Intimidator Spot 110@1.0.gdtf': ('Chauvet DJ', 'Intimidator Spot 110'),
        'Chauvet DJ@Mini Kinta IRC@1.0.gdtf': ('Chauvet DJ', 'Mini Kinta IRC'),
        'Fun Generation@SePar Quad LED RGB UV IR@1.0.gdtf': (
            'Fun Generation',
            'SePar Quad LED RGB UV IR',
        ),
        'UKING@ZQ06017@1.0.gdtf': ('UKING', 'ZQ06017'),
        'Generic@RGB@1.0.gdtf': ('Generic', 'RGB'),
        'Generic@Dimmer@1.0.gdtf': ('Generic', 'Dimmer'),
        'Generic@RGBW@1.0.gdtf': ('Generic', 'RGBW'),
    }
    # The renaming kept each fixture type's identity.
    assert {entry['name']: entry['fixture_type_id'] for entry in library.values()} == {
        'Intimidator Spot 110': 'C4520A76-CB18-59DA-BD76-1A8D7895F792',
        'Mini Kinta IRC': '25DB1C80-423E-5249-AF69-F5327A2C525D',
        'SePar Quad LED RGB UV IR': 'D21247BD-52DC-5D43-85C4-C8FCA5E3DB64',
        # The bar was patched with the synthetic RGB, so both keep that type's identity.
        'ZQ06017': 'EA49AFD9-9175-566F-99D8-99BE8413B9C9',
        'RGB': 'EA49AFD9-9175-566F-99D8-99BE8413B9C9',
        'Dimmer': 'C0534927-F91B-58EC-A113-98381DCF491F',
        'RGBW': '3DECEF94-E414-57FC-ABA3-4132A883A51E',
    }


@pytest.mark.parametrize(
    ('edit', 'message'),
    [
        (('LongName="Test Dimmer"', 'LongName="Dimmer 2"'), "LongName 'Dimmer 2' must match"),
        (('<Revision Text="1.0"', '<Revision'), 'needs a Revision with Text'),
        (('Manufacturer="Generic"', 'Manufacturer="Gen@eric"'), 'without @'),
    ],
)
def test_an_archive_without_a_share_name_is_refused_on_import(tmp_path, edit, message):
    source = tmp_path / 'source.gdtf'
    source.write_bytes(mvr.gdtf_package(DESCRIPTION.read_text(encoding='utf-8').replace(*edit)))
    with pytest.raises(ValueError, match=message):
        gdtf.import_fixture(source, tmp_path / 'library', '8 bit', lambda *_: pytest.fail())


@pytest.mark.parametrize('mode,channels', [('8 bit', 1), ('16 bit', 2)])
def test_standard_parser_discovers_modes_and_resolution(mode, channels):
    metadata = gdtf.profile_metadata(DESCRIPTION.read_text(encoding='utf-8'), mode)
    assert metadata['channels'] == channels
    assert metadata['manufacturer'] == 'Generic'


def test_unknown_mode_reports_available_modes_without_publishing(archive, tmp_path):
    library = tmp_path / 'library'
    with pytest.raises(ValueError, match='available: 8 bit, 16 bit'):
        gdtf.import_fixture(archive, library, 'unknown', lambda *_: pytest.fail())
    assert not library.exists()


@pytest.mark.parametrize('external_edit', [False, True])
def test_rejected_or_stale_import_does_not_overwrite_the_library(archive, tmp_path, external_edit):
    library = tmp_path / 'library'
    library.mkdir()
    before, external = b'installed', b'edited elsewhere'
    (library / SPEC).write_bytes(before)

    def validate(_patch):
        if external_edit:
            (library / SPEC).write_bytes(external)
        else:
            raise ValueError('native validation rejected unsupported fixture mode')

    with pytest.raises(ValueError):
        gdtf.import_fixture(archive, library, '8 bit', validate)
    assert (library / SPEC).read_bytes() == (external if external_edit else before)
    assert not list(library.glob('.gdtf-*'))


def test_failed_replace_preserves_the_library_and_cleans_staging(archive, tmp_path, monkeypatch):
    library = tmp_path / 'library'

    def denied(*_):
        raise PermissionError('locked')

    monkeypatch.setattr(gdtf.os, 'replace', denied)
    with pytest.raises(PermissionError, match='locked'):
        gdtf.import_fixture(archive, library, '8 bit', lambda *_: None)
    assert not (library / SPEC).exists()
    assert not list(library.glob('.gdtf-*'))


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/drive', 'models\\escape'])
def test_unsafe_archive_paths_are_rejected_without_extraction(archive, name):
    with zipfile.ZipFile(archive, 'a') as package:
        package.writestr(name, b'ignored')
    if '\\' in name:
        # Windows' ZIP writer normalizes separators; simulate an external archive.
        archive.write_bytes(
            archive.read_bytes().replace(name.replace('\\', '/').encode(), name.encode())
        )
    with pytest.raises(ValueError, match='unsafe'):
        gdtf.read_description(archive)


def test_duplicate_description_is_rejected(archive):
    with zipfile.ZipFile(archive, 'a') as package, pytest.warns(UserWarning, match='Duplicate'):
        package.writestr('description.xml', DESCRIPTION.read_bytes())
    with pytest.raises(ValueError, match='duplicate'):
        gdtf.read_description(archive)


@pytest.mark.parametrize(
    'limit', ['MAX_ARCHIVE_BYTES', 'MAX_DESCRIPTION_BYTES', 'MAX_ARCHIVE_ENTRIES']
)
def test_archive_limits_are_checked_before_parser(archive, monkeypatch, limit):
    monkeypatch.setattr(gdtf, limit, 1)
    with pytest.raises(ValueError, match='limit|too many'):
        gdtf.read_description(archive)


@pytest.mark.parametrize(
    'xml',
    [
        '<GDTF>',
        '<GDTF/>',
        '<!DOCTYPE GDTF [<!ENTITY bomb "x">]><GDTF/>',
        DESCRIPTION.read_text(encoding='utf-8').replace('Offset="1"', 'Offset="999999999"'),
        DESCRIPTION.read_text(encoding='utf-8').replace('DMXBreak="1"', 'DMXBreak="999999999"'),
    ],
)
def test_invalid_or_unbounded_xml_never_reaches_standard_parser(xml, monkeypatch):
    monkeypatch.setattr(gdtf.pygdtf, 'FixtureType', lambda **_: pytest.fail('unsafe parse'))
    with pytest.raises(ValueError):
        gdtf.profile_metadata(xml, '8 bit')


def test_archive_crc_damage_is_rejected(tmp_path):
    archive = tmp_path / 'corrupt.gdtf'
    source = DESCRIPTION.read_bytes()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as package:
        package.writestr('description.xml', source)
    archive.write_bytes(archive.read_bytes().replace(b'Test Dimmer', b'Bad! Dimmer', 1))
    with pytest.raises(ValueError, match='CRC'):
        gdtf.read_description(archive)


def test_metadata_reports_movement_axes():
    library = gdtf.fixture_library(ROOT / 'rig/library')
    movement = {
        entry['name']: gdtf.profile_metadata(entry['gdtf_xml'], entry['default_mode'])['movement']
        for entry in library.values()
    }
    assert movement['Intimidator Spot 110'] == ['pan', 'tilt']
    assert movement['ZQ06017'] == []
