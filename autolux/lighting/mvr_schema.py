import math
import re

MVR_VERSION = (1, 6)
AUTOLUX_PROVIDER = 'Autolux'
ONE, OPTIONAL, MANY = '1', '0..1', '0..n'
OBJECT_TAGS = ('SceneObject', 'GroupObject', 'FocusPoint', 'Fixture')
OBJECT_TAGS += ('Support', 'Truss', 'VideoScreen', 'Projector')
OBJECTS = '|'.join(OBJECT_TAGS)
IDENTITY = 'uuid!:uuid name:string'
PATCHABLE = f'{IDENTITY} multipatch:uuid>{OBJECTS}'
PLACED = 'Matrix?:matrix Classing?:uuid>Class'
SPEC = 'GDTFSpec?:filename GDTFMode?:string'
CONTAINERS = 'CastShadow?:bool Addresses? Alignments? CustomCommands? Overwrites? Connections?'
IDS = 'FixtureID:string FixtureIDNumeric:integer'
NUMBERING = f'{IDS} UnitNumber?:integer CustomIdType?:integer CustomId?:integer'
TABLE = f'''
GeneralSceneDescription verMajor!:integer verMinor!:integer provider!:string
    providerVersion!:string / UserData? Scene
UserData / Data*
Data provider!:string ver:string / ...
Scene / AUXData? Layers
AUXData / Symdef* Position=AUXPosition* MappingDefinition* Class*
Symdef {IDENTITY} / ChildList=SymdefChildList
SymdefChildList / Geometry3D* Symbol*
AUXPosition {IDENTITY}
MappingDefinition {IDENTITY} / SizeX:integer SizeY:integer Source ScaleHandeling?
Class {IDENTITY}
Layers / Layer*
Layer {IDENTITY} / Matrix?:matrix ChildList?
ChildList / {'* '.join(OBJECT_TAGS)}*
SceneObject {PATCHABLE} / {PLACED} Geometries {SPEC} {CONTAINERS} {NUMBERING} ChildList?
GroupObject {IDENTITY} / {PLACED} ChildList?
FocusPoint {IDENTITY} / {PLACED} Geometries
Fixture {PATCHABLE} / {PLACED} {SPEC} Focus?:uuid>FocusPoint CastShadow?:bool DMXInvertPan?:bool
    DMXInvertTilt?:bool Position?:uuid>Position Function?:string {IDS} UnitNumber:integer
    ChildPosition?:string Addresses? Protocols? Alignments? CustomCommands? Overwrites?
    Connections? Color?:cie_color CustomIdType?:integer CustomId?:integer Mappings? Gobo? ChildList?
Truss {PATCHABLE} / {PLACED} Position?:uuid>Position Geometries Function?:string {SPEC}
    {CONTAINERS} ChildPosition?:string ChildList? {NUMBERING}
Support {PATCHABLE} / {PLACED} Position?:uuid>Position Geometries Function?:string
    ChainLength:float {SPEC} {CONTAINERS} {NUMBERING} ChildList?
VideoScreen {PATCHABLE} / {PLACED} Geometries Sources? Function?:string {SPEC} {CONTAINERS}
    ChildList? {NUMBERING}
Projector {PATCHABLE} / {PLACED} Geometries Projections {SPEC} {CONTAINERS} ChildList? {NUMBERING}
Gobo rotation:float / / filename
Sources / Source*
Source linkedGeometry!:string type!:enum(NDI|File|CITP|CaptureDevice) / / string
ScaleHandeling / / enum(ScaleKeepRatio|ScaleIgnoreRatio|KeepSizeCenter)
Geometries / Geometry3D* Symbol*
Symbol uuid!:uuid symdef!:uuid>Symdef / Matrix?:matrix
Geometry3D fileName!:filename / Matrix?:matrix
Projections / Projection*
Projection / Source ScaleHandeling?
Addresses / Address* Network*
Address break:integer / / dmx_address
Network geometry!:string ipv4:ipv4 subnetmask:ipv4 ipv6:ipv6 dhcp:bool hostname:string
Protocols / Protocol*
Protocol geometry:string name:string type:string version:string
    transmission:enum(Unicast|Multicast|Broadcast|Anycast)
Alignments / Alignment*
Alignment geometry:string up:vector direction:vector
CustomCommands / CustomCommand*
CustomCommand / / string
Overwrites / Overwrite*
Overwrite universal!:string target:string
Connections / Connection*
Connection own!:string other!:string toObject!:uuid>{OBJECTS}
Mappings / Mapping*
Mapping linkedDef!:uuid>MappingDefinition / ux?:integer uy?:integer ox?:integer oy?:integer rz?:float
AutoluxData provider!:string ver:string / Movement*
Movement fixture!:uuid>Fixture axis!:enum(Pan|Tilt) minimum:float maximum:float
'''
FIELD = re.compile(r'(\w+)(?:=(\w+))?([!?*]?)(?::(\w+))?(?:\(([^)]*)\))?(?:>(\S+))?')
COUNTS = {'': ONE, '!': ONE, '?': OPTIONAL, '*': MANY}


def _field(token: str, attribute: bool) -> dict:
    name, key, mark, kind, values, target = FIELD.fullmatch(token).groups()
    return {
        'name': name,
        'key': key or name,
        'count': (ONE if mark == '!' else OPTIONAL) if attribute else COUNTS[mark],
        'kind': kind,
        'values': values.split('|') if values else [],
        'target': target.split('|') if target else [],
    }


def _node(attributes: str, children: str = '', text: str = '') -> dict:
    opaque = children.strip() == '...'
    return {
        'attributes': [_field(token, True) for token in attributes.split()],
        'children': [] if opaque else [_field(token, False) for token in children.split()],
        'any_content': opaque,
        'text': _field(f'text:{text.strip()}', True) if text.strip() else None,
    }


NODES = {
    key: _node(*rest.split('/'))
    for key, _, rest in (line.partition(' ') for line in TABLE.replace('\n    ', ' ').split('\n'))
    if key
}
FUNCTION_RANGES = ('PhysicalFrom', 'PhysicalTo', 'RealFade', 'RealAcceleration', 'Min', 'Max')
GDTF_EDITABLE = {
    'DMXMode': {'Name': 'name', 'Description': 'string'},
    'ChannelFunction': {'Name': 'name', 'DMXFrom': 'dmx_value', 'Default': 'dmx_value'}
    | dict.fromkeys(FUNCTION_RANGES, 'float')
    | {'CustomName': 'string'},
    'ChannelSet': {'Name': 'name', 'DMXFrom': 'dmx_value'}
    | dict.fromkeys(FUNCTION_RANGES[:2], 'float'),
}
INTEGER = re.compile(r'-?\d+')
FLOAT = re.compile(r'[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?')
UUID = re.compile(
    r'(?!0{8}-0{4}-0{4}-0{4}-0{12})[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}'
)
IPV4 = re.compile(r'((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)')
FILENAME = re.compile(r'[^<>:"/\\|?*\x00-\x1f]+')
MATRIX_ROW = re.compile(r'\{([^{}]*)\}')
MAX_UNIVERSES = 32768
UNIVERSE_SIZE = 512


def _floats(text: str, count: int) -> list[float] | None:
    parts = text.split(',')
    if len(parts) != count or not all(FLOAT.fullmatch(part.strip()) for part in parts):
        return None
    values = [float(part) for part in parts]
    return values if all(map(math.isfinite, values)) else None


def parse_matrix(text: str) -> list[float] | None:
    rows = MATRIX_ROW.findall(text)
    if len(rows) != 4 or MATRIX_ROW.sub('', text).strip():
        return None
    values = [_floats(row, 3) for row in rows]
    return None if None in values else [value for row in values for value in row]


def dmx_address(text: str) -> tuple[int, int] | None:
    if INTEGER.fullmatch(text):
        offset = int(text) - 1
        if offset not in range(MAX_UNIVERSES * UNIVERSE_SIZE):
            return None
        return offset // UNIVERSE_SIZE, offset % UNIVERSE_SIZE + 1
    parts = text.split('.')
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    universe, address = map(int, parts)
    valid = 1 <= universe <= MAX_UNIVERSES and 1 <= address <= UNIVERSE_SIZE
    return (universe - 1, address) if valid else None


KINDS = {
    'string': (re.compile(r".*", re.DOTALL), ''),
    'integer': (INTEGER, 'a base-10 integer'),
    'float': (lambda text: FLOAT.fullmatch(text) and _floats(text, 1), 'a finite decimal number'),
    'bool': (re.compile('true|false'), '"true" or "false"'),
    'uuid': (UUID, 'a non-nil UUID XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX'),
    'filename': (
        lambda text: FILENAME.fullmatch(text) and text.strip() == text,
        'a file name without folders or reserved characters',
    ),
    'matrix': (parse_matrix, 'four {x,y,z} rows of finite numbers'),
    'cie_color': (lambda text: _floats(text, 3), 'CIE xyY as x,y,Y'),
    'vector': (lambda text: _floats(text, 3), 'three numbers x,y,z'),
    'ipv4': (IPV4, 'a dotted IPv4 address'),
    'ipv6': (re.compile(r'(?=(.*:){2})[0-9A-Fa-f:]{2,39}'), 'an IPv6 address'),
    'dmx_address': (dmx_address, 'an absolute address 1..16777216 or Universe.Address'),
    'name': (
        re.compile(r'''[ "#%'()*+\-/0-9:;<=>@A-Z_`a-z\u0080-\U0010ffff]+'''),
        'a GDTF Name (Annex C characters only)',
    ),
    'dmx_value': (re.compile(r'\d+/[1-4]s?'), 'a GDTF DMXValue such as 128/1'),
}


def value_error(kind: str, text: str, values=()) -> str | None:
    if kind == 'enum':
        return None if text in values else f'must be one of {", ".join(values)}'
    check, description = KINDS[kind]
    accepted = check.fullmatch(text) if isinstance(check, re.Pattern) else check(text)
    return None if accepted else f'must be {description}'


def describe() -> dict:
    return {
        'nodes': NODES,
        'gdtf_editable': GDTF_EDITABLE,
        'object_tags': OBJECT_TAGS,
        'autolux_provider': AUTOLUX_PROVIDER,
    }


def child_key(parent_key: str, element) -> str | None:
    if (parent_key, element.tag, element.get('provider')) == ('UserData', 'Data', AUTOLUX_PROVIDER):
        return 'AutoluxData'
    fields = NODES[parent_key]['children'] if parent_key in NODES else ()
    field = next((field for field in fields if field['name'] == element.tag), None)
    return field['key'] if field and not field['kind'] else None
