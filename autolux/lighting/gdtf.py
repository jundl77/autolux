import io
import os
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import pygdtf

from autolux.lighting import mvr

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
MAX_DESCRIPTION_BYTES = 1024 * 1024
MAX_LIBRARY_ARCHIVES = 256
MAX_XML_NODES = 32768
MAX_XML_DEPTH = 64
MAX_DMX_CHANNELS = 512
MAX_CHANNEL_BYTES = 4


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open('rb') as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'{path.name} exceeds its {limit}-byte limit')
    return content


def read_description(archive: Path) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(read_bounded(archive, MAX_ARCHIVE_BYTES))) as package:
            members = package.infolist()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise ValueError('GDTF archive contains too many entries')
            names = set()
            for member in members:
                name = member.orig_filename
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or '..' in path.parts
                    or '\\' in name
                    or ':' in name
                    or '\x00' in name
                    or name in names
                ):
                    raise ValueError('GDTF archive contains an unsafe or duplicate path')
                names.add(name)
            description = package.getinfo('description.xml')
            if description.file_size > MAX_DESCRIPTION_BYTES:
                raise ValueError('GDTF description exceeds its size limit')
            with package.open(description) as source:
                content = source.read(MAX_DESCRIPTION_BYTES + 1)
            if len(content) > MAX_DESCRIPTION_BYTES:
                raise ValueError('GDTF description exceeds its size limit')
            return content.decode('utf-8')
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, RuntimeError) as error:
        raise ValueError(f'Invalid GDTF archive: {error}') from error


def _guard_description(xml: str) -> None:
    if len(xml.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
        raise ValueError('GDTF description exceeds its size limit')
    if '<!DOCTYPE' in xml or '<!ENTITY' in xml:
        raise ValueError('GDTF DTD and entity declarations are unsupported')
    root = ElementTree.fromstring(xml)
    if root.tag != 'GDTF' or len(root.findall('FixtureType')) != 1:
        raise ValueError('GDTF description must contain exactly one FixtureType')
    pending = [(root, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
            raise ValueError('GDTF XML exceeds its node or depth limit')
        if node.tag == 'GeometryReference':
            raise ValueError('GDTF geometry references are not supported by Starfire')
        if node.tag == 'DMXChannel':
            offsets = node.get('Offset', 'None')
            if offsets != 'None':
                values = [int(value) for value in offsets.split(',')]
                if not 1 <= len(values) <= MAX_CHANNEL_BYTES or any(
                    not 1 <= value <= MAX_DMX_CHANNELS for value in values
                ):
                    raise ValueError('GDTF channel offsets must be within 1..512')
            dmx_break = node.get('DMXBreak', '1')
            if dmx_break != 'Overwrite' and not 1 <= int(dmx_break) <= MAX_DMX_CHANNELS:
                raise ValueError('GDTF DMX break exceeds its limit')
        pending.extend((child, depth + 1) for child in node)


def _parse(xml: str):
    _guard_description(xml)
    return pygdtf.FixtureType(dsc_file=io.BytesIO(xml.encode('utf-8')))


def profile_metadata(xml: str, mode_name: str) -> dict:
    try:
        fixture = _parse(xml)
        modes = {mode.name: mode for mode in fixture.dmx_modes}
        if mode_name not in modes:
            raise ValueError(f'Unknown GDTF mode {mode_name!r}; available: {", ".join(modes)}')
        mode = modes[mode_name]
        if not mode.dmx_channels_count:
            raise ValueError('GDTF mode has no physical DMX channels')
        return {
            'name': fixture.name,
            'manufacturer': fixture.manufacturer,
            'dmx_mode': mode_name,
            'channels': mode.dmx_channels_count,
            'movement': sorted(
                {
                    str(logical.attribute).lower()
                    for channel in mode.dmx_channels
                    for logical in channel.logical_channels
                }
                & {'pan', 'tilt'}
            ),
            'notes': f'{fixture.manufacturer} {fixture.name} · {mode_name} · '
            f'{mode.dmx_channels_count} DMX channels. {fixture.description or ""}'.rstrip(),
        }
    except (ElementTree.ParseError, AttributeError, IndexError, KeyError, TypeError) as error:
        raise ValueError(f'Invalid GDTF description: {error}') from error


def _library_entry(archive: Path) -> dict:
    content = read_bounded(archive, MAX_ARCHIVE_BYTES)
    xml = read_description(archive)
    try:
        fixture = _parse(xml)
    except (ElementTree.ParseError, AttributeError, IndexError, KeyError, TypeError) as error:
        raise ValueError(f'Invalid GDTF description {archive.name}: {error}') from error
    spec = mvr.gdtf_spec_name(fixture.manufacturer, fixture.name)
    if archive.name != spec:
        # The file name is the GDTFSpec an MVR patch names it by.
        raise ValueError(f'{archive.name} describes {spec}; name the file {spec}')
    modes = mvr.gdtf_modes(xml, spec)
    if not modes:
        raise ValueError(f'{spec} has no DMX mode')
    return {
        'profile_id': fixture.name,
        'name': fixture.name,
        'manufacturer': fixture.manufacturer,
        'fixture_type_id': fixture.fixture_type_id,
        'gdtf_xml': xml,
        'archive': content,
        'default_mode': next(iter(modes)),
        'modes': modes,
    }


def fixture_library(directory: Path) -> dict:
    """The installed GDTF archives, keyed by the GDTFSpec file name an MVR patch uses."""
    archives = sorted(Path(directory).glob('*' + mvr.GDTF_SUFFIX))
    if len(archives) > MAX_LIBRARY_ARCHIVES:
        raise ValueError(f'Fixture library holds more than {MAX_LIBRARY_ARCHIVES} archives')
    return {archive.name: _library_entry(archive) for archive in archives}


def import_fixture(
    archive: Path,
    library: Path,
    mode_name: str,
    validate: Callable[[Path], None],
) -> dict:
    """Install a GDTF archive as is once Starfire renders the chosen mode."""
    content = read_bounded(archive, MAX_ARCHIVE_BYTES)
    xml = read_description(archive)
    metadata = profile_metadata(xml, mode_name)
    spec = mvr.gdtf_spec_name(metadata['manufacturer'], metadata['name'])
    target = library / spec
    before = target.read_bytes() if target.exists() else None
    library.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.gdtf-', dir=library) as temporary:
        staged = Path(temporary) / spec
        with staged.open('wb') as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        patch = Path(temporary) / 'rig.mvr'
        scene = mvr.new_scene()
        mvr.embed_archive(scene, spec, content)
        mvr.add_fixture(
            scene,
            name='imported',
            gdtf_spec=spec,
            gdtf_mode=mode_name,
            universe=0,
            address=1,
            fixture_id=1,
        )
        patch.write_bytes(mvr.write(scene))
        validate(patch)
        current = target.read_bytes() if target.exists() else None
        if current != before:
            raise ValueError(f'{spec} changed in the library during validation; retry')
        os.replace(staged, target)
    return metadata
