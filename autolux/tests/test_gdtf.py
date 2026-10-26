"""GDTF import installs the archive as is and never publishes an unvalidated one."""

import zipfile
from pathlib import Path

import pytest

from autolux.lighting import gdtf, mvr
from autolux.lighting.fixture_setup import FixtureStore, require_renderable

DESCRIPTION = Path(__file__).parent / 'data' / 'gdtf' / 'dimmer.xml'
SPEC = 'Autolux@Test Dimmer.gdtf'


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


def test_the_shipped_library_is_the_gdtf_the_room_rig_embeds():
    root = Path(__file__).parents[2]
    library = gdtf.fixture_library(root / 'rigs/library')
    for rig in ('room.mvr', 'generic.mvr'):
        scene = mvr.read((root / 'rigs' / rig).read_bytes())
        for spec in mvr.referenced_specs(scene.root):
            assert scene.entries[spec][1] == library[spec]['archive'], spec


@pytest.mark.parametrize('mode,channels', [('8 bit', 1), ('16 bit', 2)])
def test_standard_parser_discovers_modes_and_resolution(mode, channels):
    metadata = gdtf.profile_metadata(DESCRIPTION.read_text(encoding='utf-8'), mode)
    assert metadata['channels'] == channels
    assert metadata['manufacturer'] == 'Autolux'


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
    library = gdtf.fixture_library(Path(__file__).parents[2] / 'rigs/library')
    movement = {
        entry['name']: gdtf.profile_metadata(entry['gdtf_xml'], entry['default_mode'])['movement']
        for entry in library.values()
    }
    assert movement['chauvet_intimidator_spot_110_12ch'] == ['pan', 'tilt']
    assert movement['generic_rgb'] == []
