import math
import re
from dataclasses import asdict, dataclass

MVR_VERSION = (1, 6)
AUTOLUX_PROVIDER = 'Autolux'
ONE, OPTIONAL, MANY = '1', '0..1', '0..n'
OBJECT_TAGS = (
    'SceneObject',
    'GroupObject',
    'FocusPoint',
    'Fixture',
    'Support',
    'Truss',
    'VideoScreen',
    'Projector',
)


@dataclass(frozen=True)
class Attribute:
    name: str
    kind: str
    required: bool = False
    default: str | None = None
    values: tuple[str, ...] = ()
    target: tuple[str, ...] = ()


@dataclass(frozen=True)
class Child:
    tag: str
    count: str
    kind: str | None = None
    node: str | None = None
    values: tuple[str, ...] = ()
    target: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.node or self.tag


@dataclass(frozen=True)
class Node:
    tag: str
    attributes: tuple[Attribute, ...] = ()
    children: tuple[Child, ...] = ()
    text: str | None = None
    values: tuple[str, ...] = ()
    any_content: bool = False
    extension: bool = False
    key: str | None = None


def _identity(*extra: Attribute) -> tuple[Attribute, ...]:
    return (
        Attribute('uuid', 'uuid', required=True),
        Attribute('name', 'string', default=''),
        *extra,
    )


MULTIPATCH = Attribute('multipatch', 'uuid', target=('Fixture',) + OBJECT_TAGS)
MATRIX = Child('Matrix', OPTIONAL, 'matrix')
CLASSING = Child('Classing', OPTIONAL, 'uuid', target=('Class',))
POSITION = Child('Position', OPTIONAL, 'uuid', target=('Position',))
GEOMETRIES = Child('Geometries', ONE)
FUNCTION = Child('Function', OPTIONAL, 'string')
GDTF_SPEC = Child('GDTFSpec', OPTIONAL, 'filename')
GDTF_MODE = Child('GDTFMode', OPTIONAL, 'string')
CAST_SHADOW = Child('CastShadow', OPTIONAL, 'bool')
CONTAINERS = (
    Child('Addresses', OPTIONAL),
    Child('Alignments', OPTIONAL),
    Child('CustomCommands', OPTIONAL),
    Child('Overwrites', OPTIONAL),
    Child('Connections', OPTIONAL),
)
FIXTURE_IDS = (
    Child('FixtureID', ONE, 'string'),
    Child('FixtureIDNumeric', ONE, 'integer'),
)
NUMBERING = (
    Child('UnitNumber', OPTIONAL, 'integer'),
    Child('CustomIdType', OPTIONAL, 'integer'),
    Child('CustomId', OPTIONAL, 'integer'),
)
CHILD_LIST = Child('ChildList', OPTIONAL)

NODES: dict[str, Node] = {
    (node.key or node.tag): node
    for node in (
        Node(
            'GeneralSceneDescription',
            (
                Attribute('verMajor', 'integer', required=True),
                Attribute('verMinor', 'integer', required=True),
                Attribute('provider', 'string', required=True),
                Attribute('providerVersion', 'string', required=True),
            ),
            (Child('UserData', OPTIONAL), Child('Scene', ONE)),
        ),
        Node('UserData', children=(Child('Data', MANY),)),
        Node(
            'Data',
            (
                Attribute('provider', 'string', required=True),
                Attribute('ver', 'string', default='1'),
            ),
            any_content=True,
        ),
        Node('Scene', children=(Child('AUXData', OPTIONAL), Child('Layers', ONE))),
        Node(
            'AUXData',
            children=(
                Child('Symdef', MANY),
                Child('Position', MANY, node='AUXPosition'),
                Child('MappingDefinition', MANY),
                Child('Class', MANY),
            ),
        ),
        Node('Symdef', _identity(), (Child('ChildList', ONE, node='SymdefChildList'),)),
        Node(
            'ChildList',
            children=(Child('Geometry3D', MANY), Child('Symbol', MANY)),
            key='SymdefChildList',
        ),
        Node('Position', _identity(), key='AUXPosition'),
        Node(
            'MappingDefinition',
            _identity(),
            (
                Child('SizeX', ONE, 'integer'),
                Child('SizeY', ONE, 'integer'),
                Child('Source', ONE),
                Child('ScaleHandeling', OPTIONAL),
            ),
        ),
        Node('Class', _identity()),
        Node('Layers', children=(Child('Layer', MANY),)),
        Node('Layer', _identity(), (MATRIX, CHILD_LIST)),
        Node('ChildList', children=tuple(Child(tag, MANY) for tag in OBJECT_TAGS)),
        Node(
            'SceneObject',
            _identity(MULTIPATCH),
            (
                MATRIX,
                CLASSING,
                GEOMETRIES,
                GDTF_SPEC,
                GDTF_MODE,
                CAST_SHADOW,
                *CONTAINERS,
                *FIXTURE_IDS,
                *NUMBERING,
                CHILD_LIST,
            ),
        ),
        Node('GroupObject', _identity(), (MATRIX, CLASSING, CHILD_LIST)),
        Node('FocusPoint', _identity(), (MATRIX, CLASSING, GEOMETRIES)),
        Node(
            'Fixture',
            _identity(MULTIPATCH),
            (
                MATRIX,
                CLASSING,
                GDTF_SPEC,
                GDTF_MODE,
                Child('Focus', OPTIONAL, 'uuid', target=('FocusPoint',)),
                CAST_SHADOW,
                Child('DMXInvertPan', OPTIONAL, 'bool'),
                Child('DMXInvertTilt', OPTIONAL, 'bool'),
                POSITION,
                FUNCTION,
                *FIXTURE_IDS,
                Child('UnitNumber', ONE, 'integer'),
                Child('ChildPosition', OPTIONAL, 'string'),
                Child('Addresses', OPTIONAL),
                Child('Protocols', OPTIONAL),
                Child('Alignments', OPTIONAL),
                Child('CustomCommands', OPTIONAL),
                Child('Overwrites', OPTIONAL),
                Child('Connections', OPTIONAL),
                Child('Color', OPTIONAL, 'cie_color'),
                Child('CustomIdType', OPTIONAL, 'integer'),
                Child('CustomId', OPTIONAL, 'integer'),
                Child('Mappings', OPTIONAL),
                Child('Gobo', OPTIONAL),
                CHILD_LIST,
            ),
        ),
        Node(
            'Truss',
            _identity(MULTIPATCH),
            (
                MATRIX,
                CLASSING,
                POSITION,
                GEOMETRIES,
                FUNCTION,
                GDTF_SPEC,
                GDTF_MODE,
                CAST_SHADOW,
                *CONTAINERS,
                Child('ChildPosition', OPTIONAL, 'string'),
                CHILD_LIST,
                *FIXTURE_IDS,
                *NUMBERING,
            ),
        ),
        Node(
            'Support',
            _identity(MULTIPATCH),
            (
                MATRIX,
                CLASSING,
                POSITION,
                GEOMETRIES,
                FUNCTION,
                Child('ChainLength', ONE, 'float'),
                GDTF_SPEC,
                GDTF_MODE,
                CAST_SHADOW,
                *CONTAINERS,
                *FIXTURE_IDS,
                *NUMBERING,
                CHILD_LIST,
            ),
        ),
        Node(
            'VideoScreen',
            _identity(MULTIPATCH),
            (
                MATRIX,
                CLASSING,
                GEOMETRIES,
                Child('Sources', OPTIONAL),
                FUNCTION,
                GDTF_SPEC,
                GDTF_MODE,
                CAST_SHADOW,
                *CONTAINERS,
                CHILD_LIST,
                *FIXTURE_IDS,
                *NUMBERING,
            ),
        ),
        Node(
            'Projector',
            _identity(MULTIPATCH),
            (
                MATRIX,
                CLASSING,
                GEOMETRIES,
                Child('Projections', ONE),
                GDTF_SPEC,
                GDTF_MODE,
                CAST_SHADOW,
                *CONTAINERS,
                CHILD_LIST,
                *FIXTURE_IDS,
                *NUMBERING,
            ),
        ),
        Node('Gobo', (Attribute('rotation', 'float', default='0'),), text='filename'),
        Node('Sources', children=(Child('Source', MANY),)),
        Node(
            'Source',
            (
                Attribute('linkedGeometry', 'string', required=True),
                Attribute(
                    'type', 'enum', required=True, values=('NDI', 'File', 'CITP', 'CaptureDevice')
                ),
            ),
            text='string',
        ),
        Node(
            'ScaleHandeling',
            text='enum',
            values=('ScaleKeepRatio', 'ScaleIgnoreRatio', 'KeepSizeCenter'),
        ),
        Node('Geometries', children=(Child('Geometry3D', MANY), Child('Symbol', MANY))),
        Node(
            'Symbol',
            (
                Attribute('uuid', 'uuid', required=True),
                Attribute('symdef', 'uuid', required=True, target=('Symdef',)),
            ),
            (MATRIX,),
        ),
        Node('Geometry3D', (Attribute('fileName', 'filename', required=True),), (MATRIX,)),
        Node('Projections', children=(Child('Projection', MANY),)),
        Node('Projection', children=(Child('Source', ONE), Child('ScaleHandeling', OPTIONAL))),
        Node('Addresses', children=(Child('Address', MANY), Child('Network', MANY))),
        Node('Address', (Attribute('break', 'integer', default='0'),), text='dmx_address'),
        Node(
            'Network',
            (
                Attribute('geometry', 'string', required=True),
                Attribute('ipv4', 'ipv4'),
                Attribute('subnetmask', 'ipv4'),
                Attribute('ipv6', 'ipv6'),
                Attribute('dhcp', 'bool', default='false'),
                Attribute('hostname', 'string'),
            ),
        ),
        Node('Protocols', children=(Child('Protocol', MANY),)),
        Node(
            'Protocol',
            (
                Attribute('geometry', 'string', default='NetworkInOut_1'),
                Attribute('name', 'string', default=''),
                Attribute('type', 'string', default=''),
                Attribute('version', 'string', default=''),
                Attribute(
                    'transmission',
                    'enum',
                    values=('Unicast', 'Multicast', 'Broadcast', 'Anycast'),
                ),
            ),
        ),
        Node('Alignments', children=(Child('Alignment', MANY),)),
        Node(
            'Alignment',
            (
                Attribute('geometry', 'string'),
                Attribute('up', 'vector', default='0,0,1'),
                Attribute('direction', 'vector', default='0,0,-1'),
            ),
        ),
        Node('CustomCommands', children=(Child('CustomCommand', MANY),)),
        Node('CustomCommand', text='string'),
        Node('Overwrites', children=(Child('Overwrite', MANY),)),
        Node(
            'Overwrite',
            (
                Attribute('universal', 'string', required=True),
                Attribute('target', 'string', default=''),
            ),
        ),
        Node('Connections', children=(Child('Connection', MANY),)),
        Node(
            'Connection',
            (
                Attribute('own', 'string', required=True),
                Attribute('other', 'string', required=True),
                Attribute('toObject', 'uuid', required=True, target=OBJECT_TAGS),
            ),
        ),
        Node('Mappings', children=(Child('Mapping', MANY),)),
        Node(
            'Mapping',
            (Attribute('linkedDef', 'uuid', required=True, target=('MappingDefinition',)),),
            (
                Child('ux', OPTIONAL, 'integer'),
                Child('uy', OPTIONAL, 'integer'),
                Child('ox', OPTIONAL, 'integer'),
                Child('oy', OPTIONAL, 'integer'),
                Child('rz', OPTIONAL, 'float'),
            ),
        ),
        Node(
            'Data',
            (
                Attribute('provider', 'string', required=True),
                Attribute('ver', 'string', default='1'),
            ),
            (Child('Movement', MANY),),
            extension=True,
            key='AutoluxData',
        ),
        Node(
            'Movement',
            (
                Attribute('fixture', 'uuid', required=True, target=('Fixture',)),
                Attribute('axis', 'enum', required=True, values=('Pan', 'Tilt')),
                Attribute('minimum', 'float', default='0'),
                Attribute('maximum', 'float', default='1'),
            ),
            extension=True,
        ),
    )
}

GDTF_EDITABLE = {
    'DMXMode': {'Name': 'name', 'Description': 'string'},
    'ChannelFunction': {
        'Name': 'name',
        'DMXFrom': 'dmx_value',
        'Default': 'dmx_value',
        'PhysicalFrom': 'float',
        'PhysicalTo': 'float',
        'RealFade': 'float',
        'RealAcceleration': 'float',
        'Min': 'float',
        'Max': 'float',
        'CustomName': 'string',
    },
    'ChannelSet': {
        'Name': 'name',
        'DMXFrom': 'dmx_value',
        'PhysicalFrom': 'float',
        'PhysicalTo': 'float',
    },
}

INTEGER = re.compile(r'-?\d+')
FLOAT = re.compile(r'[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?')
UUID = re.compile(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')
NIL_UUID = '00000000-0000-0000-0000-000000000000'
IPV4 = re.compile(r'((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)')
IPV6 = re.compile(r'[0-9A-Fa-f:]{2,39}')
DMX_VALUE = re.compile(r'\d+/[1-4]s?')
FILENAME = re.compile(r'[^<>:"/\\|?*\x00-\x1f]+')
GDTF_NAME = re.compile(r'''[ "#%'()*+\-/0-9:;<=>@A-Z_`a-z\u0080-\U0010ffff]+''')
MATRIX_ROW = re.compile(r'\{([^{}]*)\}')
MAX_DMX_ADDRESS = 32768 * 512


def _floats(text: str, count: int) -> list[float] | None:
    parts = text.split(',')
    if len(parts) != count or not all(FLOAT.fullmatch(part.strip()) for part in parts):
        return None
    values = [float(part) for part in parts]
    return values if all(math.isfinite(value) for value in values) else None


def parse_matrix(text: str) -> list[float] | None:
    rows = MATRIX_ROW.findall(text)
    if len(rows) != 4 or MATRIX_ROW.sub('', text).strip():
        return None
    values = [_floats(row, 3) for row in rows]
    return None if None in values else [value for row in values for value in row]


def dmx_address(text: str) -> tuple[int, int] | None:
    if INTEGER.fullmatch(text):
        absolute = int(text)
        if not 1 <= absolute <= MAX_DMX_ADDRESS:
            return None
        universe, address = divmod(absolute - 1, 512)
        return universe, address + 1
    parts = text.split('.')
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    universe, address = int(parts[0]), int(parts[1])
    if not 1 <= universe <= 32768 or not 1 <= address <= 512:
        return None
    return universe - 1, address


def value_error(kind: str, text: str, values: tuple[str, ...] = ()) -> str | None:
    checks = {
        'string': lambda: True,
        'integer': lambda: INTEGER.fullmatch(text) is not None,
        'float': lambda: FLOAT.fullmatch(text) is not None and math.isfinite(float(text)),
        'bool': lambda: text in ('true', 'false'),
        'uuid': lambda: UUID.fullmatch(text) is not None and text != NIL_UUID,
        'filename': lambda: FILENAME.fullmatch(text) is not None and text.strip() == text,
        'matrix': lambda: parse_matrix(text) is not None,
        'cie_color': lambda: _floats(text, 3) is not None,
        'vector': lambda: _floats(text, 3) is not None,
        'ipv4': lambda: IPV4.fullmatch(text) is not None,
        'ipv6': lambda: IPV6.fullmatch(text) is not None and text.count(':') >= 2,
        'dmx_address': lambda: dmx_address(text) is not None,
        'enum': lambda: text in values,
        'name': lambda: GDTF_NAME.fullmatch(text) is not None,
        'dmx_value': lambda: DMX_VALUE.fullmatch(text) is not None,
    }
    if checks[kind]():
        return None
    descriptions = {
        'integer': 'a base-10 integer',
        'float': 'a finite decimal number',
        'bool': '"true" or "false"',
        'uuid': 'a non-nil UUID XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX',
        'filename': 'a file name without folders or reserved characters',
        'matrix': 'four {x,y,z} rows of finite numbers',
        'cie_color': 'CIE xyY as x,y,Y',
        'vector': 'three numbers x,y,z',
        'ipv4': 'a dotted IPv4 address',
        'ipv6': 'an IPv6 address',
        'dmx_address': 'an absolute address 1..16777216 or Universe.Address',
        'enum': 'one of ' + ', '.join(values),
        'name': 'a GDTF Name (Annex C characters only)',
        'dmx_value': 'a GDTF DMXValue such as 128/1',
    }
    return f'must be {descriptions[kind]}'


def describe() -> dict:
    return {
        'nodes': {
            key: {
                **asdict(node),
                'children': [{**asdict(child), 'key': child.key} for child in node.children],
            }
            for key, node in NODES.items()
        },
        'gdtf_editable': GDTF_EDITABLE,
        'object_tags': list(OBJECT_TAGS),
        'autolux_provider': AUTOLUX_PROVIDER,
    }


def child_schema(parent_key: str, tag: str, attributes: dict | None = None) -> str | None:
    node = NODES.get(parent_key)
    if node is None:
        return None
    if (
        parent_key == 'UserData'
        and tag == 'Data'
        and (attributes or {}).get('provider') == AUTOLUX_PROVIDER
    ):
        return 'AutoluxData'
    for child in node.children:
        if child.tag == tag:
            return child.key if child.kind is None else None
    return None
