import copy
import io
import math
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from xml.etree import ElementTree

from autolux.lighting import mvr_schema as schema

SCENE_ENTRY = 'GeneralSceneDescription.xml'
GDTF_DESCRIPTION = 'description.xml'
GDTF_SUFFIX = '.gdtf'
PROVIDER = schema.AUTOLUX_PROVIDER
PROVIDER_VERSION = '0.1.0'
DEFAULT_LAYER = 'Room'
MAX_MVR_BYTES = 32 * 1024 * 1024
MAX_SCENE_BYTES = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
MAX_XML_NODES = 65536
MAX_XML_DEPTH = 64
DMX_UNIVERSE_SIZE = schema.UNIVERSE_SIZE
MATRIX_DECIMALS = 6
ORTHONORMAL_TOLERANCE = 1e-3
GIMBAL_TOLERANCE = 1e-9
MAX_PITCH_DEGREES = 60.0
IDENTITY_MATRIX = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
MOVEMENT_AXES = ('pan', 'tilt')
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REVISION_TEXT = 'Edited in Autolux Config'
VERSION_ATTRIBUTES = {
    'verMajor': str(schema.MVR_VERSION[0]),
    'verMinor': str(schema.MVR_VERSION[1]),
    'provider': PROVIDER,
    'providerVersion': PROVIDER_VERSION,
}


@dataclass
class Scene:
    root: ElementTree.Element
    entries: dict[str, tuple[zipfile.ZipInfo, bytes]] = field(default_factory=dict)


def _guard_xml(content: bytes, label: str) -> ElementTree.Element:
    if b'<!DOCTYPE' in content or b'<!ENTITY' in content:
        raise ValueError(f'{label}: DTD and entity declarations are unsupported')
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise ValueError(f'{label}: invalid XML: {error}') from error
    pending, count = [(root, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
            raise ValueError(f'{label}: XML exceeds its node or depth limit')
        pending.extend((child, depth + 1) for child in node)
    return root


def _unsafe(name: str, folded: set) -> bool:
    path = PurePosixPath(name)
    return (
        path.is_absolute()
        or '..' in path.parts
        or any(character in name for character in '\\:\x00')
        or name.casefold() in folded
    )


def _archive(content: bytes, label: str, only: str | None = None) -> dict:
    if len(content) > MAX_MVR_BYTES:
        raise ValueError(f'{label} exceeds its size limit')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            members = package.infolist()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(f'{label} contains too many entries')
            if sum(member.file_size for member in members) > MAX_MVR_BYTES:
                raise ValueError(f'{label} expands beyond its size limit')
            entries, folded = {}, set()
            for member in members:
                name = member.orig_filename
                if _unsafe(name, folded):
                    raise ValueError(f'{label} contains an unsafe or duplicate path {name!r}')
                if member.flag_bits & 0x1:
                    raise ValueError(f'{label} must not be encrypted')
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError(f'{label} entries must be STORE or DEFLATE compressed')
                folded.add(name.casefold())
                if only in (None, name):
                    entries[name] = (member, package.read(member))
            return entries
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError(f'Invalid {label}: {error}') from error


def read(content: bytes) -> Scene:
    entries = _archive(content, 'MVR archive')
    if SCENE_ENTRY not in entries:
        raise ValueError(f'MVR archive has no {SCENE_ENTRY}')
    _, scene = entries.pop(SCENE_ENTRY)
    if len(scene) > MAX_SCENE_BYTES:
        raise ValueError('MVR scene exceeds its size limit')
    root = _guard_xml(scene, SCENE_ENTRY)
    if (
        root.tag != 'GeneralSceneDescription'
        or root.get('verMajor') != VERSION_ATTRIBUTES['verMajor']
    ):
        raise ValueError('MVR scene must be a GeneralSceneDescription with verMajor 1')
    return Scene(root, entries)


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _xml_bytes(root: ElementTree.Element) -> bytes:
    root = copy.deepcopy(root)
    ElementTree.indent(root, space='  ')
    declaration = b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    return declaration + ElementTree.tostring(root, encoding='utf-8') + b'\n'


def _zip(first: tuple[str, bytes], others: dict) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(_info(first[0]), first[1])
        for name in sorted(others):
            package.writestr(_info(name), others[name][1])
    return output.getvalue()


def write(scene: Scene) -> bytes:
    xml = _xml_bytes(scene.root)
    if len(xml) > MAX_SCENE_BYTES:
        raise ValueError('MVR scene exceeds its size limit')
    return _zip((SCENE_ENTRY, xml), scene.entries)


def new_scene() -> Scene:
    root = ElementTree.Element('GeneralSceneDescription', VERSION_ATTRIBUTES)
    layers = ElementTree.SubElement(ElementTree.SubElement(root, 'Scene'), 'Layers')
    layer = ElementTree.SubElement(
        layers, 'Layer', uuid=stable_uuid('layer', DEFAULT_LAYER), name=DEFAULT_LAYER
    )
    ElementTree.SubElement(layer, 'ChildList')
    return Scene(root)


def stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'autolux-mvr:' + '/'.join(parts))).upper()


def gdtf_package(description: str, other: dict | None = None) -> bytes:
    return _zip((GDTF_DESCRIPTION, description.encode('utf-8')), other or {})


def gdtf_entries(scene: Scene, spec: str, only: str | None = None) -> dict:
    if spec not in scene.entries:
        raise ValueError(f'GDTFSpec {spec} is not in the MVR archive')
    return _archive(scene.entries[spec][1], f'GDTF {spec}', only)


def gdtf_description(scene: Scene, spec: str) -> str:
    entries = gdtf_entries(scene, spec, GDTF_DESCRIPTION)
    if GDTF_DESCRIPTION not in entries:
        raise ValueError(f'GDTF {spec} has no {GDTF_DESCRIPTION}')
    try:
        return entries[GDTF_DESCRIPTION][1].decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError(f'GDTF {spec} description is not UTF-8') from error


def embed_gdtf(scene: Scene, spec: str, description: str, other: dict | None = None) -> None:
    scene.entries[spec] = (_info(spec), gdtf_package(description, other))


def gdtf_spec_name(manufacturer: str, fixture_type: str) -> str:
    name = f'{manufacturer}@{fixture_type}{GDTF_SUFFIX}'
    return ''.join('_' if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in name)


def gdtf_root(description: str, label: str) -> ElementTree.Element:
    root = _guard_xml(description.encode('utf-8'), label)
    if root.tag != 'GDTF' or len(root.findall('FixtureType')) != 1:
        raise ValueError(f'{label}: must contain one GDTF FixtureType')
    return root


def gdtf_modes(description: str, label: str = 'GDTF') -> dict[str, dict]:
    modes = {}
    for mode in gdtf_root(description, label).iter('DMXMode'):
        offsets, breaks, attributes = [], set(), set()
        for channel in mode.iter('DMXChannel'):
            breaks.add(dmx_break := channel.get('DMXBreak', '1'))
            if dmx_break == '1' and (text := channel.get('Offset', 'None')) != 'None':
                offsets.extend(map(int, text.split(',')))
            attributes.update(
                node.get('Attribute', '').lower() for node in channel.iter('LogicalChannel')
            )
        modes[mode.get('Name', '')] = {
            'channels': max(offsets, default=0),
            'breaks': sorted(breaks),
            'movement': sorted(set(MOVEMENT_AXES) & attributes),
        }
    return modes


def to_tree(element: ElementTree.Element, prefix: str = 'n') -> dict:
    counter = iter(range(MAX_XML_NODES + 1))

    def convert(node):
        return {
            'key': f'{prefix}{next(counter)}',
            'tag': node.tag,
            'attributes': dict(node.attrib),
            'text': (node.text or '').strip(),
            'children': [convert(child) for child in node],
        }

    return convert(element)


def from_tree(tree: dict) -> ElementTree.Element:
    try:
        element = ElementTree.Element(
            tree['tag'], {str(k): str(v) for k, v in tree['attributes'].items()}
        )
        element.text = tree.get('text') or None
        element.extend(from_tree(child) for child in tree['children'])
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f'Malformed rig draft: {error}') from error
    return element


def _without_keys(tree):
    if isinstance(tree, dict):
        return {key: _without_keys(value) for key, value in tree.items() if key != 'key'}
    return [_without_keys(value) for value in tree] if isinstance(tree, list) else tree


def _nodes(tree: dict):
    yield tree
    for child in tree['children']:
        yield from _nodes(child)


def _gdtf_edit_errors(before: dict, after: dict, label: str) -> list[str]:
    errors = []

    def walk(before, after, path):
        where = f'{label} {path}/{after["tag"]}'
        if before['tag'] != after['tag'] or len(before['children']) != len(after['children']):
            return errors.append(f'{where}: GDTF structure is read-only')
        if before.get('text', '') != after.get('text', ''):
            errors.append(f'{where}: text is read-only')
        editable = schema.GDTF_EDITABLE.get(after['tag'], {})
        for name in sorted(set(before['attributes']) | set(after['attributes'])):
            old, new = before['attributes'].get(name), after['attributes'].get(name)
            if old == new:
                continue
            if name not in editable:
                errors.append(f'{where}@{name} is read-only')
            elif new is None or (problem := schema.value_error(editable[name], new)):
                errors.append(f'{where}@{name} {problem or "is required"}')
        for index, pair in enumerate(zip(before['children'], after['children'], strict=True)):
            walk(*pair, f'{path}/{after["tag"]}[{index}]')

    walk(before, after, '')
    names = [node['attributes'].get('Name') for node in _nodes(after) if node['tag'] == 'DMXMode']
    if len(names) != len(set(names)):
        errors.append(f'{label}: DMXMode names must be unique')
    return errors


def _with_revision(root: ElementTree.Element) -> ElementTree.Element:
    fixture_type = root.find('FixtureType')
    revisions = fixture_type.find('Revisions')
    if revisions is None:
        revisions = ElementTree.Element('Revisions')
        anchor = fixture_type.find('FTPresets')
        fixture_type.insert(
            len(fixture_type) if anchor is None else list(fixture_type).index(anchor), revisions
        )
    ElementTree.SubElement(
        revisions,
        'Revision',
        Text=REVISION_TEXT,
        Date=datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S'),
        ModifiedBy=f'{PROVIDER} {PROVIDER_VERSION}',
    )
    return root


def referenced_specs(root: ElementTree.Element) -> set[str]:
    return {text for node in root.iter('GDTFSpec') if (text := (node.text or '').strip())}


def draft(scene: Scene) -> dict:
    gdtf = {}
    for spec in sorted(referenced_specs(scene.root) & set(scene.entries)):
        gdtf[spec] = to_tree(gdtf_root(gdtf_description(scene, spec), spec), f'g{len(gdtf)}-')
    return {'scene': to_tree(scene.root, 's'), 'gdtf': gdtf}


def apply_draft(scene: Scene, edited: dict, library: dict) -> Scene:
    root = from_tree(edited['scene'])
    for name, value in VERSION_ATTRIBUTES.items():
        root.set(name, value)
    kept = {name: entry for name, entry in scene.entries.items() if not name.endswith(GDTF_SUFFIX)}
    result, errors = Scene(root, kept), []
    for spec in sorted(referenced_specs(root)):
        if spec in scene.entries:
            original = gdtf_description(scene, spec)
            other = {
                name: entry
                for name, entry in gdtf_entries(scene, spec).items()
                if name != GDTF_DESCRIPTION
            }
        elif spec in library:
            original, other = library[spec]['gdtf_xml'], {}
        else:
            errors.append(f'GDTFSpec {spec} is neither in the MVR archive nor installed')
            continue
        tree = edited.get('gdtf', {}).get(spec)
        before = _without_keys(to_tree(gdtf_root(original, spec)))
        if tree is None or _without_keys(tree) == before:
            if spec in scene.entries:
                result.entries[spec] = scene.entries[spec]
            else:
                embed_gdtf(result, spec, original)
        elif problems := _gdtf_edit_errors(before, _without_keys(tree), spec):
            errors.extend(problems)
        else:
            embed_gdtf(
                result, spec, _xml_bytes(_with_revision(from_tree(tree))).decode('utf-8'), other
            )
    if errors:
        raise ValueError('; '.join(errors[:5]))
    return result


def _number(value: float) -> str:
    text = f'{round(value, MATRIX_DECIMALS):.{MATRIX_DECIMALS}f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def format_matrix(matrix) -> str:
    return ''.join(
        '{' + ','.join(map(_number, matrix[row : row + 3])) + '}' for row in range(0, 12, 3)
    )


def parse_matrix(text: str | None) -> list[float]:
    if text is None or not text.strip():
        return list(IDENTITY_MATRIX)
    if (values := schema.parse_matrix(text)) is None:
        raise ValueError('MVR Matrix must contain four {x,y,z} rows of finite numbers')
    return values


def multiply(parent, child) -> list[float]:
    def apply(vector):
        return [sum(parent[3 * axis + row] * vector[axis] for axis in range(3)) for row in range(3)]

    axes = [value for start in (0, 3, 6) for value in apply(child[start : start + 3])]
    return axes + [a + b for a, b in zip(apply(child[9:12]), parent[9:12], strict=True)]


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _axes(matrix):
    axes = []
    for start in (0, 3, 6):
        axis = matrix[start : start + 3]
        if not (length := math.sqrt(_dot(axis, axis))) > ORTHONORMAL_TOLERANCE:
            raise ValueError('MVR Matrix axes must be nonzero')
        axes.append([value / length for value in axis])
    u, v, w = axes
    cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    if (
        max(abs(_dot(u, v)), abs(_dot(v, w)), abs(_dot(u, w))) > ORTHONORMAL_TOLERANCE
        or _dot(cross, w) < 1 - ORTHONORMAL_TOLERANCE
    ):
        raise ValueError('MVR Matrix rotation must be orthogonal and unmirrored')
    return u, v, w


def canonical_degrees(angle: float) -> float:
    wrapped = math.remainder(angle, 360.0)
    return (wrapped + 360.0 if wrapped <= -180.0 else wrapped) + 0.0


def rotation_degrees(matrix) -> tuple[float, float, float]:
    u, v, w = _axes(matrix)
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
    if abs(v[2]) > 1 - GIMBAL_TOLERANCE:
        yaw, roll = math.degrees(math.atan2(u[1], u[0])), 0.0
    else:
        yaw, roll = math.degrees(math.atan2(-v[0], v[1])), math.degrees(math.atan2(-u[2], w[2]))
    return canonical_degrees(yaw), pitch + 0.0, canonical_degrees(roll)


def home_orientation(matrix) -> dict:
    yaw, pitch, _ = rotation_degrees(matrix)
    if abs(pitch) > MAX_PITCH_DEGREES:
        raise ValueError(
            f'fixture pitch {pitch:.1f} exceeds +/-{MAX_PITCH_DEGREES:g} degrees; the tilt home '
            'is only approximated for near-level bases'
        )
    return {
        'pan_offset_degrees': yaw,
        'tilt_offset_degrees': pitch,
        'reversed': _axes(matrix)[2][2] < 0,
    }


def compose_matrix(position_mm, rotation) -> list[float]:
    (cy, sy), (cp, sp), (cr, sr) = ((math.cos(r), math.sin(r)) for r in map(math.radians, rotation))
    values = [
        cy * cr - sy * sp * sr,
        sy * cr + cy * sp * sr,
        -cp * sr,
        -sy * cp,
        cy * cp,
        sp,
        cy * sr + sy * sp * cr,
        sy * sr - cy * sp * cr,
        cp * cr,
        *position_mm,
    ]
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError('Fixture position and rotation must be finite numbers')
    return [round(float(value), MATRIX_DECIMALS) + 0.0 for value in values]


def placed_objects(root: ElementTree.Element):
    def walk(node, matrix):
        for child in node.find('ChildList') if node.find('ChildList') is not None else ():
            if child.tag in schema.OBJECT_TAGS:
                placed = multiply(matrix, parse_matrix(child.findtext('Matrix')))
                yield child, placed
                yield from walk(child, placed)

    for layer in root.findall('Scene/Layers/Layer'):
        yield from walk(layer, parse_matrix(layer.findtext('Matrix')))


def _text(node, tag: str) -> str:
    return (node.findtext(tag) or '').strip()


def _autolux_data(root: ElementTree.Element):
    return next(
        (node for node in root.findall('UserData/Data') if node.get('provider') == PROVIDER), None
    )


def patched_fixtures(scene: Scene) -> list[dict]:
    root = scene.root
    classes = {
        node.get('uuid', '').upper(): node.get('name', '')
        for node in root.findall('Scene/AUXData/Class')
    }
    limits = {
        (node.get('fixture', '').upper(), node.get('axis', '').lower()): (
            float(node.get('minimum', '0')),
            float(node.get('maximum', '1')),
        )
        for data in root.findall('UserData/Data')
        if data.get('provider') == PROVIDER
        for node in data.findall('Movement')
    }
    fixtures = []
    for node, matrix in placed_objects(root):
        addresses = node.findall('Addresses/Address')
        if node.tag != 'Fixture' or not _text(node, 'GDTFSpec') or not addresses:
            continue
        identity = node.get('uuid', '').upper()
        patch = []
        for address in addresses:
            if (location := schema.dmx_address((address.text or '').strip())) is None:
                raise ValueError(f'{node.get("name", identity)}: invalid DMX address')
            patch.append((int(address.get('break', '0')), *location))
        movement = {}
        for axis in MOVEMENT_AXES:
            low, high = limits.get((identity, axis), (0.0, 1.0))
            inverted = _text(node, f'DMXInvert{axis.title()}') == 'true'
            movement[axis] = {'min': low, 'max': high, 'inverted': inverted}
        fixtures.append(
            {
                'uuid': identity,
                'name': node.get('name', ''),
                'gdtf_spec': _text(node, 'GDTFSpec'),
                'gdtf_mode': _text(node, 'GDTFMode'),
                'addresses': patch,
                'universe': patch[0][1],
                'address': patch[0][2],
                'class': classes.get(_text(node, 'Classing').upper(), ''),
                'matrix': matrix,
                **movement,
            }
        )
    return fixtures


def groups(fixtures) -> dict[str, list[str]]:
    result = {}
    for fixture in fixtures:
        if fixture['class']:
            result.setdefault(fixture['class'], []).append(fixture['name'])
    return result


def add_fixture(
    scene: Scene,
    *,
    name: str,
    gdtf_spec: str,
    gdtf_mode: str,
    universe: int,
    address: int,
    fixture_id: int,
    fixture_class: str = '',
    matrix=IDENTITY_MATRIX,
    identity: str | None = None,
) -> ElementTree.Element:
    layer = scene.root.find('Scene/Layers/Layer')
    children = layer.find('ChildList')
    if children is None:
        children = ElementTree.SubElement(layer, 'ChildList')
    node = ElementTree.SubElement(
        children, 'Fixture', uuid=identity or str(uuid.uuid4()).upper(), name=name
    )
    values = [('Matrix', format_matrix(matrix))]
    if fixture_class:
        values.append(('Classing', class_uuid(scene, fixture_class)))
    values += [
        ('GDTFSpec', gdtf_spec),
        ('GDTFMode', gdtf_mode),
        ('FixtureID', str(fixture_id)),
        ('FixtureIDNumeric', str(fixture_id)),
        ('UnitNumber', '0'),
    ]
    for tag, text in values:
        ElementTree.SubElement(node, tag).text = text
    addresses = ElementTree.SubElement(node, 'Addresses')
    absolute = str(universe * DMX_UNIVERSE_SIZE + address)
    ElementTree.SubElement(addresses, 'Address', {'break': '0'}).text = absolute
    return node


def class_uuid(scene: Scene, name: str) -> str:
    auxdata = scene.root.find('Scene/AUXData')
    if auxdata is None:
        auxdata = ElementTree.Element('AUXData')
        scene.root.find('Scene').insert(0, auxdata)
    for node in auxdata.findall('Class'):
        if node.get('name') == name:
            return node.get('uuid')
    ElementTree.SubElement(
        auxdata, 'Class', uuid=(identity := stable_uuid('class', name)), name=name
    )
    return identity


def set_movement_limits(scene: Scene, identity: str, axis: str, low: float, high: float) -> None:
    root = scene.root
    if (user_data := root.find('UserData')) is None:
        root.insert(0, user_data := ElementTree.Element('UserData'))
    if (data := _autolux_data(root)) is None:
        data = ElementTree.SubElement(user_data, 'Data', provider=PROVIDER, ver='1')
    for node in data.findall('Movement'):
        if node.get('fixture') == identity and node.get('axis') == axis.title():
            data.remove(node)
    ElementTree.SubElement(
        data,
        'Movement',
        fixture=identity,
        axis=axis.title(),
        minimum=_number(low),
        maximum=_number(high),
    )
