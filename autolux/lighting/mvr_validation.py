"""Check a whole MVR archive against the MVR 1.6 tables and the GDTF files it references."""

from xml.etree import ElementTree

from autolux.lighting import mvr
from autolux.lighting import mvr_schema as schema

MAX_ERRORS = 20


def _label(element: ElementTree.Element, path: str) -> str:
    name = element.get('name') or element.get('uuid')
    return f'{path}/{element.tag}' + (f' "{name}"' if name else '')


def _structure(element, key, path, errors, identities, references, files):
    node = schema.NODES[key]
    label = _label(element, path)
    declared = {attribute.name: attribute for attribute in node.attributes}
    for name, value in element.attrib.items():
        if name not in declared:
            errors.append(f'{label}: attribute {name} is not defined by MVR 1.6')
    for attribute in node.attributes:
        value = element.get(attribute.name)
        if value is None:
            if attribute.required:
                errors.append(f'{label}: attribute {attribute.name} is required')
            continue
        if problem := schema.value_error(attribute.kind, value, attribute.values):
            errors.append(f'{label}@{attribute.name} {problem}')
        elif attribute.kind == 'uuid':
            if attribute.name == 'uuid':
                identities.append((value.upper(), element.tag, label))
            elif attribute.target:
                references.append((value.upper(), attribute.target, f'{label}@{attribute.name}'))
        elif attribute.kind == 'filename':
            files.append((value, f'{label}@{attribute.name}'))
    if node.any_content:
        return
    text = (element.text or '').strip()
    if node.text:
        if problem := schema.value_error(node.text, text, node.values):
            errors.append(f'{label}: value {problem}')
        elif node.text == 'filename' or (key == 'Source' and element.get('type') == 'File'):
            files.append((text, label))
    elif text:
        errors.append(f'{label}: unexpected text {text[:40]!r}')
    allowed = {child.tag: child for child in node.children}
    counts = {}
    for child in element:
        definition = allowed.get(child.tag)
        if definition is None:
            errors.append(f'{label}: {child.tag} is not allowed here by MVR 1.6')
            continue
        counts[child.tag] = counts.get(child.tag, 0) + 1
        if definition.kind is not None:
            child_label = f'{label}/{child.tag}'
            if child.attrib or len(child):
                errors.append(f'{child_label}: value nodes have no attributes or children')
            value = (child.text or '').strip()
            if problem := schema.value_error(definition.kind, value, definition.values):
                errors.append(f'{child_label} {problem}')
            elif definition.target:
                references.append((value.upper(), definition.target, child_label))
            continue
        child_key = schema.child_schema(key, child.tag, dict(child.attrib)) or definition.key
        _structure(child, child_key, label, errors, identities, references, files)
    multipatch = bool(element.get('multipatch'))
    for definition in node.children:
        count = counts.get(definition.tag, 0)
        # MVR 1.6 forbids FixtureID/CustomId on multipatch children; they inherit the parent's.
        exempt = multipatch and definition.tag in ('FixtureID', 'FixtureIDNumeric')
        if definition.count == schema.ONE and count != 1 and not exempt:
            errors.append(f'{label}: needs exactly one {definition.tag}')
        if definition.count == schema.OPTIONAL and count > 1:
            errors.append(f'{label}: allows at most one {definition.tag}')
    if multipatch and (
        (element.findtext('FixtureID') or '').strip() or element.find('CustomId') is not None
    ):
        errors.append(f'{label}: multipatch children may not define FixtureID or CustomId')


def _patch(scene, element, label, errors, modes_cache):
    spec = (element.findtext('GDTFSpec') or '').strip()
    mode = (element.findtext('GDTFMode') or '').strip()
    addresses = element.findall('Addresses/Address')
    if not spec:
        if mode:
            errors.append(f'{label}: GDTFMode needs a GDTFSpec')
        return
    if element.find('GDTFMode') is None:
        errors.append(f'{label}: GDTFMode is mandatory when GDTFSpec is defined')
        return
    if spec not in modes_cache:
        try:
            modes_cache[spec] = mvr.gdtf_modes(mvr.gdtf_description(scene, spec), spec)
        except ValueError as error:
            modes_cache[spec] = None
            errors.append(f'{label}: {error}')
    modes = modes_cache[spec]
    if modes is None:
        return
    if mode not in modes:
        errors.append(
            f'{label}: GDTFMode {mode!r} does not exist in {spec}; available: {", ".join(modes)}'
        )
        return
    breaks = [address.get('break', '0') for address in addresses]
    if len(breaks) != len(set(breaks)):
        errors.append(f'{label}: Address break values must be unique')
    for value in breaks:
        # MVR break n addresses GDTF DMXBreak n + 1 (GDTF breaks start at 1).
        if value.lstrip('-').isdigit() and str(int(value) + 1) not in modes[mode]['breaks']:
            errors.append(
                f'{label}: break {value} has no DMXBreak {int(value) + 1} in mode {mode!r}'
            )


def validate(scene: mvr.Scene) -> list[str]:
    """Every spec rule this module checks; an empty list means the archive may be written."""
    root = scene.root
    errors, identities, references, files = [], [], [], []
    if root.tag != 'GeneralSceneDescription':
        return ['Root node must be GeneralSceneDescription']
    _structure(root, 'GeneralSceneDescription', '', errors, identities, references, files)
    seen = {}
    for identity, tag, label in identities:
        if identity in seen:
            errors.append(f'{label}: uuid duplicates {seen[identity][1]}')
        else:
            seen[identity] = (tag, label)
    for identity, targets, label in references:
        if identity not in seen:
            errors.append(f'{label}: {identity} does not resolve to a {"/".join(targets)}')
        elif seen[identity][0] not in targets:
            errors.append(
                f'{label}: {identity} is a {seen[identity][0]}, not a {"/".join(targets)}'
            )
    for name, label in files:
        if name not in scene.entries:
            errors.append(f'{label}: file {name} is not in the MVR archive root')
    modes = {}
    try:
        placed = [element for element, _ in mvr.placed_objects(root)]
    except ValueError:
        placed = []  # A malformed Matrix is already reported above.
    for element in placed:
        _patch(scene, element, _label(element, ''), errors, modes)
    for node in root.iter('Movement'):
        try:
            low, high = float(node.get('minimum', '0')), float(node.get('maximum', '1'))
        except ValueError:
            continue  # Reported as a value error above.
        if not 0 <= low <= high <= 1:
            errors.append(
                f'Autolux Movement {node.get("fixture")}: limits must satisfy 0 <= min <= max <= 1'
            )
    return errors[:MAX_ERRORS]


def require_valid(scene: mvr.Scene) -> None:
    if errors := validate(scene):
        raise ValueError('MVR is not spec-conforming: ' + '; '.join(errors))
