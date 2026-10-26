"""MVR rigs: GeneralSceneDescription.xml plus the GDTF files it references, in one zip.

The editable model is a generic tree of the XML, so every MVR 1.6 node survives and
mvr_schema validates it. Orientation and break conventions: docs/starfire/rig-mvr.md.
"""

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
PROVIDER = 'Autolux'
PROVIDER_VERSION = '0.1.0'
DEFAULT_LAYER = 'Room'
MAX_MVR_BYTES = 32 * 1024 * 1024
MAX_SCENE_BYTES = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
MAX_XML_NODES = 65536
MAX_XML_DEPTH = 64
DMX_UNIVERSE_SIZE = 512
MATRIX_DECIMALS = 6
ORTHONORMAL_TOLERANCE = 1e-3
GIMBAL_TOLERANCE = 1e-9
FULL_TURN_DEGREES = 360.0
HALF_TURN_DEGREES = 180.0
IDENTITY_MATRIX = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
MOVEMENT_AXES = ('pan', 'tilt')
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REVISION_TEXT = 'Edited in Autolux Config'


@dataclass
class Scene:
    """A parsed MVR archive: scene tree plus every other entry, byte for byte."""

    root: ElementTree.Element
    entries: dict[str, tuple[zipfile.ZipInfo, bytes]] = field(default_factory=dict)


# ---------------------------------------------------------------- archive and XML


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


def _archive(content: bytes, label: str) -> dict[str, tuple[zipfile.ZipInfo, bytes]]:
    """Read every entry without extracting; reject unsafe, encrypted or case-clashing names."""
    if len(content) > MAX_MVR_BYTES:
        raise ValueError(f'{label} exceeds its size limit')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            members = package.infolist()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(f'{label} contains too many entries')
            # zipfile stops at each declared size, so this bounds decompression.
            if sum(member.file_size for member in members) > MAX_MVR_BYTES:
                raise ValueError(f'{label} expands beyond its size limit')
            entries, folded = {}, set()
            for member in members:
                name = member.orig_filename
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or '..' in path.parts
                    or '\\' in name
                    or ':' in name
                    or '\x00' in name
                    or name.casefold() in folded
                ):
                    raise ValueError(f'{label} contains an unsafe or duplicate path {name!r}')
                if member.flag_bits & 0x1:
                    raise ValueError(f'{label} must not be encrypted')
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError(f'{label} entries must be STORE or DEFLATE compressed')
                folded.add(name.casefold())
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
    if root.tag != 'GeneralSceneDescription' or root.get('verMajor') != str(schema.MVR_VERSION[0]):
        raise ValueError('MVR scene must be a GeneralSceneDescription with verMajor 1')
    return Scene(root, entries)


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _xml_bytes(root: ElementTree.Element) -> bytes:
    root = copy.deepcopy(root)
    ElementTree.indent(root, space='  ')
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        + ElementTree.tostring(root, encoding='utf-8')
        + b'\n'
    )


def write(scene: Scene) -> bytes:
    """Serialise deterministically: pretty XML, fixed timestamps, other entries untouched."""
    xml = _xml_bytes(scene.root)
    if len(xml) > MAX_SCENE_BYTES:
        raise ValueError('MVR scene exceeds its size limit')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(_info(SCENE_ENTRY), xml)
        # Normalised headers and name order make unchanged content byte-identical.
        for name in sorted(scene.entries):
            package.writestr(_info(name), scene.entries[name][1])
    return output.getvalue()


def new_scene() -> Scene:
    root = ElementTree.Element(
        'GeneralSceneDescription',
        verMajor=str(schema.MVR_VERSION[0]),
        verMinor=str(schema.MVR_VERSION[1]),
        provider=PROVIDER,
        providerVersion=PROVIDER_VERSION,
    )
    scene = ElementTree.SubElement(root, 'Scene')
    layers = ElementTree.SubElement(scene, 'Layers')
    ElementTree.SubElement(
        layers, 'Layer', uuid=stable_uuid('layer', DEFAULT_LAYER), name=DEFAULT_LAYER
    )
    return Scene(root)


def new_uuid() -> str:
    return str(uuid.uuid4()).upper()


def stable_uuid(*parts: str) -> str:
    """Deterministic IDs keep converted rigs diff-able across reruns."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'autolux-mvr:' + '/'.join(parts))).upper()


# ---------------------------------------------------------------- GDTF packages


def gdtf_package(description: str, other: dict | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(_info(GDTF_DESCRIPTION), description.encode('utf-8'))
        for name in sorted(other or {}):
            package.writestr(_info(name), other[name][1])
    return output.getvalue()


def gdtf_entries(scene: Scene, spec: str) -> dict:
    if spec not in scene.entries:
        raise ValueError(f'GDTFSpec {spec} is not in the MVR archive')
    return _archive(scene.entries[spec][1], f'GDTF {spec}')


def gdtf_description(scene: Scene, spec: str) -> str:
    entries = gdtf_entries(scene, spec)
    if GDTF_DESCRIPTION not in entries:
        raise ValueError(f'GDTF {spec} has no {GDTF_DESCRIPTION}')
    try:
        return entries[GDTF_DESCRIPTION][1].decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError(f'GDTF {spec} description is not UTF-8') from error


def embed_gdtf(scene: Scene, spec: str, description: str, other: dict | None = None) -> None:
    scene.entries[spec] = (_info(spec), gdtf_package(description, other))


def gdtf_spec_name(manufacturer: str, fixture_type: str) -> str:
    """GDTF names archives <Manufacturer>@<FixtureTypeName>; MVR forbids reserved characters."""
    name = f'{manufacturer}@{fixture_type}{GDTF_SUFFIX}'
    return ''.join(
        '_' if character in '<>:"/\\|?*' or ord(character) < 32 else character for character in name
    )


def gdtf_root(description: str, label: str) -> ElementTree.Element:
    root = _guard_xml(description.encode('utf-8'), label)
    if root.tag != 'GDTF' or len(root.findall('FixtureType')) != 1:
        raise ValueError(f'{label}: must contain one GDTF FixtureType')
    return root


def gdtf_modes(description: str, label: str = 'GDTF') -> dict[str, dict]:
    """DMX modes with Starfire's footprint rule: highest break-1 offset, and the breaks used."""
    modes = {}
    for mode in gdtf_root(description, label).iter('DMXMode'):
        offsets, breaks, attributes = [], set(), set()
        for channel in mode.iter('DMXChannel'):
            dmx_break = channel.get('DMXBreak', '1')
            breaks.add(dmx_break)
            text = channel.get('Offset', 'None')
            if dmx_break == '1' and text != 'None':
                offsets.extend(int(value) for value in text.split(','))
            attributes.update(
                logical.get('Attribute', '') for logical in channel.iter('LogicalChannel')
            )
        modes[mode.get('Name', '')] = {
            'channels': max(offsets, default=0),
            'breaks': sorted(breaks),
            'movement': sorted({'pan', 'tilt'} & {name.lower() for name in attributes}),
        }
    return modes


def _without_keys(tree):
    if isinstance(tree, dict):
        return {key: _without_keys(value) for key, value in tree.items() if key != 'key'}
    if isinstance(tree, list):
        return [_without_keys(value) for value in tree]
    return tree


def _gdtf_edit_errors(original: dict, edited: dict, label: str) -> list[str]:
    """Only GDTF_EDITABLE attributes may change; structure and other values stay as shipped."""
    errors = []

    def walk(before, after, path):
        where = f'{label} {path}/{after["tag"]}'
        if before['tag'] != after['tag'] or len(before['children']) != len(after['children']):
            errors.append(f'{where}: GDTF structure is read-only')
            return
        if before.get('text', '') != after.get('text', ''):
            errors.append(f'{where}: text is read-only')
        editable = schema.GDTF_EDITABLE.get(after['tag'], {})
        for name in set(before['attributes']) | set(after['attributes']):
            old, new = before['attributes'].get(name), after['attributes'].get(name)
            if old == new:
                continue
            if name not in editable:
                errors.append(f'{where}@{name} is read-only')
            elif new is None or (problem := schema.value_error(editable[name], new)):
                errors.append(f'{where}@{name} {problem or "is required"}')
        for index, (first, second) in enumerate(
            zip(before['children'], after['children'], strict=True)
        ):
            walk(first, second, f'{path}/{after["tag"]}[{index}]')

    walk(original, edited, '')
    names = [node['attributes'].get('Name') for node in _nodes(edited) if node['tag'] == 'DMXMode']
    if len(names) != len(set(names)):
        errors.append(f'{label}: DMXMode names must be unique')
    return errors


def _with_revision(root: ElementTree.Element) -> ElementTree.Element:
    # MVR 1.6: a changed GDTF shall carry a revision reflecting the change.
    fixture_type = root.find('FixtureType')
    revisions = fixture_type.find('Revisions')
    if revisions is None:
        revisions = ElementTree.Element('Revisions')
        anchor = fixture_type.find('FTPresets')
        position = len(fixture_type) if anchor is None else list(fixture_type).index(anchor)
        fixture_type.insert(position, revisions)
    ElementTree.SubElement(
        revisions,
        'Revision',
        Text=REVISION_TEXT,
        Date=datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S'),
        ModifiedBy=f'{PROVIDER} {PROVIDER_VERSION}',
    )
    return root


# ---------------------------------------------------------------- generic tree


def to_tree(element: ElementTree.Element, prefix: str = 'n') -> dict:
    """XML as JSON nodes {tag, attributes, text, children, key}; keys are draft-local IDs."""
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
        for child in tree['children']:
            element.append(from_tree(child))
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f'Malformed rig draft: {error}') from error
    return element


def _nodes(tree: dict):
    yield tree
    for child in tree['children']:
        yield from _nodes(child)


def draft(scene: Scene) -> dict:
    """The editable model: the scene tree and each referenced GDTF description tree."""
    gdtf = {}
    for spec in sorted(referenced_specs(scene.root)):
        if spec in scene.entries:
            gdtf[spec] = to_tree(gdtf_root(gdtf_description(scene, spec), spec), f'g{len(gdtf)}-')
    return {'scene': to_tree(scene.root, 's'), 'gdtf': gdtf}


def referenced_specs(root: ElementTree.Element) -> set[str]:
    return {
        (node.text or '').strip() for node in root.iter('GDTFSpec') if (node.text or '').strip()
    }


def apply_draft(scene: Scene, edited: dict, library: dict) -> Scene:
    """Build the archive a draft describes; unchanged GDTF packages are kept byte for byte."""
    root = from_tree(edited['scene'])
    root.set('verMajor', str(schema.MVR_VERSION[0]))
    root.set('verMinor', str(schema.MVR_VERSION[1]))
    root.set('provider', PROVIDER)
    root.set('providerVersion', PROVIDER_VERSION)
    result = Scene(
        root,
        {name: entry for name, entry in scene.entries.items() if not name.endswith(GDTF_SUFFIX)},
    )
    errors = []
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
            continue
        if problems := _gdtf_edit_errors(before, _without_keys(tree), spec):
            errors.extend(problems)
            continue
        description = _xml_bytes(_with_revision(from_tree(tree))).decode('utf-8')
        embed_gdtf(result, spec, description, other)
    if errors:
        raise ValueError('; '.join(errors[:5]))
    return result


# ---------------------------------------------------------------- matrices


def _number(value: float) -> str:
    text = f'{round(value, MATRIX_DECIMALS):.{MATRIX_DECIMALS}f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def format_matrix(matrix) -> str:
    return ''.join(
        '{' + ','.join(_number(value) for value in matrix[row : row + 3]) + '}'
        for row in range(0, 12, 3)
    )


def parse_matrix(text: str | None) -> list[float]:
    """MVR Matrix: local X, Y, Z axes in parent space, then position in millimetres."""
    if text is None or not text.strip():
        return list(IDENTITY_MATRIX)
    values = schema.parse_matrix(text)
    if values is None:
        raise ValueError('MVR Matrix must contain four {x,y,z} rows of finite numbers')
    return values


def multiply(parent, child) -> list[float]:
    """Child placement expressed in the parent's space: R = Rp*Rc, o = Rp*oc + op."""

    def apply(vector):
        return [sum(parent[3 * axis + row] * vector[axis] for axis in range(3)) for row in range(3)]

    axes = [value for start in (0, 3, 6) for value in apply(child[start : start + 3])]
    origin = [a + b for a, b in zip(apply(child[9:12]), parent[9:12], strict=True)]
    return axes + origin


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _axes(matrix):
    axes = []
    for start in (0, 3, 6):
        axis = matrix[start : start + 3]
        length = math.sqrt(_dot(axis, axis))
        if not length > ORTHONORMAL_TOLERANCE:
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
    """Wrap to (-180, 180]; +180 is canonical because -180 would select other DMX."""
    wrapped = math.remainder(angle, FULL_TURN_DEGREES)
    if wrapped <= -HALF_TURN_DEGREES:
        wrapped += FULL_TURN_DEGREES
    return wrapped + 0.0


def rotation_degrees(matrix) -> tuple[float, float, float]:
    """Decompose R = Rz(yaw) * Rx(pitch) * Ry(roll); yaw is the pan home offset."""
    u, v, w = _axes(matrix)
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
    if abs(v[2]) > 1 - GIMBAL_TOLERANCE:
        yaw, roll = math.degrees(math.atan2(u[1], u[0])), 0.0
    else:
        yaw = math.degrees(math.atan2(-v[0], v[1]))
        roll = math.degrees(math.atan2(-u[2], w[2]))
    return canonical_degrees(yaw), pitch + 0.0, canonical_degrees(roll)


def compose_matrix(position_mm, rotation) -> list[float]:
    """Inverse of rotation_degrees plus the position row, rounded for stable files."""
    yaw, pitch, roll = (math.radians(value) for value in rotation)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
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


# ---------------------------------------------------------------- scene objects


def _children(node, tag):
    children = node.find('ChildList')
    return [] if children is None else [child for child in children if child.tag == tag]


def placed_objects(root: ElementTree.Element):
    """Yield (element, scene-space matrix) for every object, composing parent matrices."""

    def walk(node, matrix):
        children = node.find('ChildList')
        for child in [] if children is None else children:
            if child.tag in schema.OBJECT_TAGS:
                placed = multiply(matrix, parse_matrix(child.findtext('Matrix')))
                yield child, placed
                yield from walk(child, placed)

    for layer in root.findall('Scene/Layers/Layer'):
        yield from walk(layer, parse_matrix(layer.findtext('Matrix')))


def class_names(root: ElementTree.Element) -> dict[str, str]:
    return {
        node.get('uuid', '').upper(): node.get('name', '')
        for node in root.findall('Scene/AUXData/Class')
    }


def movement_limits(root: ElementTree.Element) -> dict[tuple[str, str], tuple[float, float]]:
    limits = {}
    for data in root.findall('UserData/Data'):
        if data.get('provider') == schema.AUTOLUX_PROVIDER:
            for node in data.findall('Movement'):
                limits[(node.get('fixture', '').upper(), node.get('axis', '').lower())] = (
                    float(node.get('minimum', '0')),
                    float(node.get('maximum', '1')),
                )
    return limits


def patched_fixtures(scene: Scene) -> list[dict]:
    """Fixtures Starfire renders: every Fixture with a GDTFSpec and at least one Address."""
    root = scene.root
    classes, limits, fixtures = class_names(root), movement_limits(root), []
    for node, matrix in placed_objects(root):
        spec = (node.findtext('GDTFSpec') or '').strip()
        addresses = node.findall('Addresses/Address')
        if node.tag != 'Fixture' or not spec or not addresses:
            continue
        identity = node.get('uuid', '').upper()
        patch = []
        for address in addresses:
            location = schema.dmx_address((address.text or '').strip())
            if location is None:
                raise ValueError(f'{node.get("name", identity)}: invalid DMX address')
            patch.append((int(address.get('break', '0')), *location))
        fixtures.append(
            {
                'uuid': identity,
                'name': node.get('name', ''),
                'gdtf_spec': spec,
                'gdtf_mode': (node.findtext('GDTFMode') or '').strip(),
                'addresses': patch,
                'universe': patch[0][1],
                'address': patch[0][2],
                'class': classes.get((node.findtext('Classing') or '').strip().upper(), ''),
                'matrix': matrix,
                **{
                    axis: {
                        'min': limits.get((identity, axis), (0.0, 1.0))[0],
                        'max': limits.get((identity, axis), (0.0, 1.0))[1],
                        'inverted': (node.findtext(f'DMXInvert{axis.title()}') or '').strip()
                        == 'true',
                    }
                    for axis in MOVEMENT_AXES
                },
            }
        )
    return fixtures


def groups(fixtures) -> dict[str, list[str]]:
    """Classes become Starfire groups; member order is MVR document order."""
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
    """Append a spec-complete Fixture to the first layer, creating its Class if needed."""
    root = scene.root
    children = root.find('Scene/Layers/Layer/ChildList')
    if children is None:
        children = ElementTree.SubElement(root.find('Scene/Layers/Layer'), 'ChildList')
    node = ElementTree.SubElement(children, 'Fixture', uuid=identity or new_uuid(), name=name)
    ElementTree.SubElement(node, 'Matrix').text = format_matrix(matrix)
    if fixture_class:
        ElementTree.SubElement(node, 'Classing').text = class_uuid(scene, fixture_class)
    ElementTree.SubElement(node, 'GDTFSpec').text = gdtf_spec
    ElementTree.SubElement(node, 'GDTFMode').text = gdtf_mode
    ElementTree.SubElement(node, 'FixtureID').text = str(fixture_id)
    ElementTree.SubElement(node, 'FixtureIDNumeric').text = str(fixture_id)
    ElementTree.SubElement(node, 'UnitNumber').text = '0'
    addresses = ElementTree.SubElement(node, 'Addresses')
    ElementTree.SubElement(addresses, 'Address', {'break': '0'}).text = str(
        universe * DMX_UNIVERSE_SIZE + address
    )
    return node


def class_uuid(scene: Scene, name: str) -> str:
    root = scene.root
    auxdata = root.find('Scene/AUXData')
    if auxdata is None:
        auxdata = ElementTree.Element('AUXData')
        root.find('Scene').insert(0, auxdata)
    for node in auxdata.findall('Class'):
        if node.get('name') == name:
            return node.get('uuid')
    identity = stable_uuid('class', name)
    ElementTree.SubElement(auxdata, 'Class', uuid=identity, name=name)
    return identity


def set_movement_limits(scene: Scene, identity: str, axis: str, low: float, high: float) -> None:
    root = scene.root
    user_data = root.find('UserData')
    if user_data is None:
        user_data = ElementTree.Element('UserData')
        root.insert(0, user_data)
    data = next(
        (
            node
            for node in user_data.findall('Data')
            if node.get('provider') == schema.AUTOLUX_PROVIDER
        ),
        None,
    )
    if data is None:
        data = ElementTree.SubElement(user_data, 'Data', provider=schema.AUTOLUX_PROVIDER, ver='1')
    ElementTree.SubElement(
        data,
        'Movement',
        fixture=identity,
        axis=axis.title(),
        minimum=_number(low),
        maximum=_number(high),
    )
