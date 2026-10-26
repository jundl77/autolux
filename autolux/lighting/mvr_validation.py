from autolux.lighting import mvr
from autolux.lighting import mvr_schema as schema

MAX_ERRORS = 20


def _label(element, path: str) -> str:
    name = element.get('name') or element.get('uuid')
    return f'{path}/{element.tag}' + (f' "{name}"' if name else '')


class _Walk:
    def __init__(self):
        self.errors, self.identities, self.references, self.files = [], [], [], []

    def value(self, field, text, label, separator=' ', file=None):
        if problem := schema.value_error(field['kind'], text, field['values']):
            self.errors.append(f'{label}{separator}{problem}')
        elif file if file is not None else field['kind'] == 'filename':
            self.files.append((text, label))
        elif field['kind'] == 'uuid' and field['target']:
            self.references.append((text.upper(), field['target'], label))

    def node(self, element, key, path):
        node, label = schema.NODES[key], _label(element, path)
        declared = {field['name']: field for field in node['attributes']}
        self.errors.extend(
            f'{label}: attribute {name} is not defined by MVR 1.6'
            for name in element.attrib
            if name not in declared
        )
        for name, field in declared.items():
            if (value := element.get(name)) is not None:
                self.value(field, value, f'{label}@{name}')
                if name == 'uuid' and not schema.value_error('uuid', value):
                    self.identities.append((value.upper(), element.tag, label))
            elif field['count'] == schema.ONE:
                self.errors.append(f'{label}: attribute {name} is required')
        if node['any_content']:
            return
        text = (element.text or '').strip()
        if node['text']:
            source_file = key == 'Source' and element.get('type') == 'File'
            self.value(node['text'], text, label, ': value ', source_file or None)
        elif text:
            self.errors.append(f'{label}: unexpected text {text[:40]!r}')
        allowed = {field['name']: field for field in node['children']}
        counts = {}
        for child in element:
            field = allowed.get(child.tag)
            if field is None:
                self.errors.append(f'{label}: {child.tag} is not allowed here by MVR 1.6')
                continue
            counts[child.tag] = counts.get(child.tag, 0) + 1
            if field['kind'] is None:
                self.node(child, schema.child_key(key, child) or field['key'], label)
                continue
            if child.attrib or len(child):
                self.errors.append(
                    f'{label}/{child.tag}: value nodes have no attributes or children'
                )
            self.value(field, (child.text or '').strip(), f'{label}/{child.tag}', file=False)
        multipatch = bool(element.get('multipatch'))
        for name, field in allowed.items():
            count = counts.get(name, 0)
            exempt = multipatch and name in ('FixtureID', 'FixtureIDNumeric')
            if field['count'] == schema.ONE and count != 1 and not exempt:
                self.errors.append(f'{label}: needs exactly one {name}')
            if field['count'] == schema.OPTIONAL and count > 1:
                self.errors.append(f'{label}: allows at most one {name}')
        if multipatch and (
            (element.findtext('FixtureID') or '').strip() or element.find('CustomId') is not None
        ):
            self.errors.append(f'{label}: multipatch children may not define FixtureID or CustomId')


def _patch_errors(scene, element, modes_cache):
    label = _label(element, '')
    spec = (element.findtext('GDTFSpec') or '').strip()
    mode = (element.findtext('GDTFMode') or '').strip()
    if not spec:
        return [f'{label}: GDTFMode needs a GDTFSpec'] if mode else []
    if element.find('GDTFMode') is None:
        return [f'{label}: GDTFMode is mandatory when GDTFSpec is defined']
    if spec not in modes_cache:
        try:
            modes_cache[spec] = mvr.gdtf_modes(mvr.gdtf_description(scene, spec), spec)
        except ValueError as error:
            modes_cache[spec] = None
            return [f'{label}: {error}']
    if (modes := modes_cache[spec]) is None:
        return []
    if mode not in modes:
        return [
            f'{label}: GDTFMode {mode!r} does not exist in {spec}; available: {", ".join(modes)}'
        ]
    breaks = [address.get('break', '0') for address in element.findall('Addresses/Address')]
    errors = (
        [f'{label}: Address break values must be unique'] if len(breaks) != len(set(breaks)) else []
    )
    return errors + [
        f'{label}: break {value} has no DMXBreak {int(value) + 1} in mode {mode!r}'
        for value in breaks
        if value.lstrip('-').isdigit() and str(int(value) + 1) not in modes[mode]['breaks']
    ]


def validate(scene: mvr.Scene) -> list[str]:
    root = scene.root
    if root.tag != 'GeneralSceneDescription':
        return ['Root node must be GeneralSceneDescription']
    walk = _Walk()
    walk.node(root, 'GeneralSceneDescription', '')
    errors, seen = walk.errors, {}
    for identity, tag, label in walk.identities:
        if identity in seen:
            errors.append(f'{label}: uuid duplicates {seen[identity][1]}')
        else:
            seen[identity] = (tag, label)
    for identity, targets, label in walk.references:
        expected = '/'.join(targets)
        if identity not in seen:
            errors.append(f'{label}: {identity} does not resolve to a {expected}')
        elif seen[identity][0] not in targets:
            errors.append(f'{label}: {identity} is a {seen[identity][0]}, not a {expected}')
    errors.extend(
        f'{label}: file {name} is not in the MVR archive root'
        for name, label in walk.files
        if name not in scene.entries
    )
    try:
        placed = [element for element, _ in mvr.placed_objects(root)]
    except ValueError:
        placed = []
    modes = {}
    for element in placed:
        errors.extend(_patch_errors(scene, element, modes))
    for node in root.iter('Movement'):
        try:
            low, high = float(node.get('minimum', '0')), float(node.get('maximum', '1'))
        except ValueError:
            continue
        if not 0 <= low <= high <= 1:
            errors.append(
                f'Autolux Movement {node.get("fixture")}: limits must satisfy 0 <= min <= max <= 1'
            )
    return errors[:MAX_ERRORS]


def require_valid(scene: mvr.Scene) -> None:
    if errors := validate(scene):
        raise ValueError('MVR is not spec-conforming: ' + '; '.join(errors))
