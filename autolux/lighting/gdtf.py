"""Import GDTF fixture descriptions without extracting archive resources."""

import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import pygdtf

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
MAX_DESCRIPTION_BYTES = 1024 * 1024
MAX_CATALOGUE_BYTES = 1024 * 1024
MAX_XML_NODES = 32768
MAX_XML_DEPTH = 64
MAX_DMX_CHANNELS = 512
MAX_CHANNEL_BYTES = 4
PROFILE_ID = re.compile(r'[a-z0-9_-]{1,64}')
SCHEMA_VERSION = 1


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open('rb') as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'{path.name} exceeds its {limit}-byte limit')
    return content


def read_description(archive: Path) -> str:
    """Read exactly one root description.xml; never extract files or follow paths."""
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
    """Bound parser allocations; GDTF interpretation belongs to pygdtf/Starfire."""
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


def profile_metadata(xml: str, mode_name: str) -> dict:
    """Describe a selected GDTF mode using the standard parser."""
    try:
        _guard_description(xml)
        fixture = pygdtf.FixtureType(dsc_file=io.BytesIO(xml.encode('utf-8')))
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


def import_fixture(
    archive: Path,
    catalogue: Path,
    profile_id: str,
    mode_name: str,
    validate: Callable[[Path, Path], None],
) -> dict:
    """Validate a candidate registry and publish it atomically for the next session."""
    if PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError('Profile ID must contain 1..64 lowercase letters, digits, _ or -')
    xml = read_description(archive)
    metadata = profile_metadata(xml, mode_name)
    before = read_bounded(catalogue, MAX_CATALOGUE_BYTES) if catalogue.exists() else None
    registry = (
        json.loads(before)
        if before is not None
        else {'schema_version': SCHEMA_VERSION, 'profiles': {}}
    )
    if (
        not isinstance(registry, dict)
        or set(registry) != {'schema_version', 'profiles'}
        or type(registry['schema_version']) is not int
        or registry['schema_version'] != SCHEMA_VERSION
        or not isinstance(registry['profiles'], dict)
    ):
        raise ValueError('Invalid fixture profile catalogue')
    registry['profiles'][profile_id] = {'gdtf_xml': xml, 'dmx_mode': mode_name}
    content = (json.dumps(registry, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if len(content) > MAX_CATALOGUE_BYTES:
        raise ValueError('Fixture profile catalogue exceeds its size limit')
    catalogue.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.gdtf-', dir=catalogue.parent) as temporary:
        staged = Path(temporary) / 'profiles.json'
        with staged.open('wb') as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        patch = Path(temporary) / 'rig.json'
        patch.write_text(
            json.dumps(
                {
                    'schema_version': SCHEMA_VERSION,
                    'fixtures': [
                        {'id': 'imported', 'profile': profile_id, 'universe': 0, 'address': 1}
                    ],
                    'groups': {},
                }
            ),
            encoding='utf-8',
        )
        validate(patch, staged)
        current = read_bounded(catalogue, MAX_CATALOGUE_BYTES) if catalogue.exists() else None
        if current != before:
            raise ValueError('Fixture profile catalogue changed during validation; retry')
        os.replace(staged, catalogue)
    return metadata
