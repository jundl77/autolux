"""GDTF import preserves source and never publishes an unvalidated registry."""

import json
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


def test_import_preserves_exact_xml_and_selected_mode_before_atomic_publish(archive, tmp_path):
    original_archive = archive.read_bytes()
    catalogue = tmp_path / 'profiles.json'
    before = b'{"schema_version":1,"profiles":{}}\n'
    catalogue.write_bytes(before)
    validations = []

    def validate(patch, staged):
        assert catalogue.read_bytes() == before
        profile = json.loads(staged.read_bytes())['profiles']['test_dimmer']
        assert profile == {
            'gdtf_xml': DESCRIPTION.read_bytes().decode('utf-8'),
            'dmx_mode': '16 bit',
        }
        scene = mvr.read(patch.read_bytes())
        [fixture] = require_renderable(scene)
        assert (fixture['gdtf_spec'], fixture['gdtf_mode']) == (SPEC, '16 bit')
        assert mvr.gdtf_description(scene, SPEC) == profile['gdtf_xml']
        validations.append(staged)

    metadata = gdtf.import_fixture(archive, catalogue, 'test_dimmer', '16 bit', validate)
    assert metadata['channels'] == 2
    assert archive.read_bytes() == original_archive
    assert len(validations) == 1
    assert not validations[0].exists()
    library = FixtureStore(tmp_path / 'rig.mvr', catalogue, lambda _: None).library()
    assert library[SPEC]['profile_id'] == 'test_dimmer'
    assert library[SPEC]['default_mode'] == '16 bit'
    assert {mode: entry['channels'] for mode, entry in library[SPEC]['modes'].items()} == {
        '8 bit': 1,
        '16 bit': 2,
    }


@pytest.mark.parametrize('mode,channels', [('8 bit', 1), ('16 bit', 2)])
def test_standard_parser_discovers_modes_and_resolution(mode, channels):
    metadata = gdtf.profile_metadata(DESCRIPTION.read_text(encoding='utf-8'), mode)
    assert metadata['channels'] == channels
    assert metadata['manufacturer'] == 'Autolux'


def test_unknown_mode_reports_available_modes_without_publishing(archive, tmp_path):
    catalogue = tmp_path / 'profiles.json'
    with pytest.raises(ValueError, match='available: 8 bit, 16 bit'):
        gdtf.import_fixture(archive, catalogue, 'test', 'unknown', lambda *_: pytest.fail())
    assert not catalogue.exists()


@pytest.mark.parametrize('external_edit', [False, True])
def test_rejected_or_stale_import_does_not_overwrite_registry(archive, tmp_path, external_edit):
    catalogue = tmp_path / 'profiles.json'
    before = b'{"schema_version":1,"profiles":{}}'
    catalogue.write_bytes(before)
    external = b'{"schema_version":1,"profiles":{},"changed":true}'

    def validate(*_):
        if external_edit:
            catalogue.write_bytes(external)
        else:
            raise ValueError('native validation rejected unsupported fixture mode')

    with pytest.raises(ValueError):
        gdtf.import_fixture(archive, catalogue, 'test', '8 bit', validate)
    assert catalogue.read_bytes() == (external if external_edit else before)
    assert not list(tmp_path.glob('.gdtf-*'))


def test_failed_replace_preserves_registry_and_cleans_staging(archive, tmp_path, monkeypatch):
    catalogue = tmp_path / 'profiles.json'
    before = b'{"schema_version":1,"profiles":{}}'
    catalogue.write_bytes(before)

    def denied(*_):
        raise PermissionError('locked')

    monkeypatch.setattr(gdtf.os, 'replace', denied)
    with pytest.raises(PermissionError, match='locked'):
        gdtf.import_fixture(archive, catalogue, 'test', '8 bit', lambda *_: None)
    assert catalogue.read_bytes() == before
    assert not list(tmp_path.glob('.gdtf-*'))


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
    profiles = json.loads((Path(__file__).parents[2] / 'rigs/profiles.json').read_bytes())
    movement = {
        name: gdtf.profile_metadata(profile['gdtf_xml'], profile['dmx_mode'])['movement']
        for name, profile in profiles['profiles'].items()
    }
    assert movement['chauvet_intimidator_spot_110_12ch'] == ['pan', 'tilt']
    assert movement['generic_rgb'] == []
