"""Compile one GDTF DMX mode into the typed DmxMode Starfire encodes from.

pygdtf supplies the fixture type's structure: modes, channels, logical channels, functions,
sets, geometries (GeometryReference breaks included), wheels and attribute definitions.

pygdtf 1.4.5's DmxValue drops the byte-shifting "s" flag ("18/1s" reads as 18 in one byte),
accepts literals beyond their declared resolution and never mirrors bytes when a value widens
to its channel's resolution. Its ChannelSet also interpolates a missing PhysicalFrom/To, where
GDTF takes the function's. So DMXFrom, Default, ModeFrom, ModeTo and the sets' physical values
are re-read from the same description.xml, in pygdtf's element order, and widened here.
"""

import io
import math
from dataclasses import dataclass

import pygdtf

from autolux.lighting import mvr
from proto.starfire import rig_pb2 as rig_wire

MAX_CHANNEL_BYTES = 4
MAX_DMX_CHANNELS = 512
MAX_FUNCTIONS = 4096
DMX_BYTE_BITS = 8
NO_FEATURE = 'NoFeature'
BRIGHTNESS_PREFIXES = ('ColorAdd_', 'ColorSub_')
SNAP_ON = ('Yes', 'On')


@dataclass(frozen=True)
class DmxValue:
    value: int
    bytes: int
    shifting: bool

    @classmethod
    def parse(cls, text: str | None) -> 'DmxValue':
        if text is None:
            return cls(0, 1, False)
        value, separator, width = text.partition('/')
        if not separator:
            raise ValueError(f'GDTF DMX value {text!r} must be uint/n or uint/ns')
        shifting = width.endswith('s')
        try:
            number, count = int(value), max(1, int(width.removesuffix('s')))
        except ValueError as error:
            raise ValueError(f'GDTF DMX value {text!r} must be uint/n or uint/ns') from error
        if number < 0 or count > 255:
            raise ValueError(f'GDTF DMX value {text!r} is out of range')
        return cls(min(number, 2 ** min(count * DMX_BYTE_BITS, 64)), count, shifting)

    def widened(self, width: int) -> int:
        """The value at a channel's resolution: mirrored bytes, or shifted with "s"."""
        if not 1 <= width <= MAX_CHANNEL_BYTES:
            raise ValueError('GDTF target resolution must be 1..4 bytes')
        if not 1 <= self.bytes <= MAX_CHANNEL_BYTES:
            raise ValueError('GDTF DMX value resolution must be 1..4 bytes')
        if self.value > (1 << (self.bytes * DMX_BYTE_BITS)) - 1:
            raise ValueError('GDTF DMX value exceeds its declared resolution')
        output = 0
        for index in range(width):
            if self.shifting and index >= self.bytes:
                byte = 0
            else:
                shift = (self.bytes - 1 - index % self.bytes) * DMX_BYTE_BITS
                byte = (self.value >> shift) & 0xFF
            output = (output << DMX_BYTE_BITS) | byte
        return output


def _finite(*values: float) -> bool:
    return all(math.isfinite(value) for value in values)


def _geometry_paths(fixture: pygdtf.FixtureType, root_name: str):
    """Every geometry under the mode's root, with references expanded to their instances."""
    root = fixture.geometries.get_geometry_by_name(root_name)
    if root is None:
        raise ValueError('GDTF mode references missing geometry')
    paths, instances = {}, []

    def visit(node, parent):
        path = (*parent, node.name)
        if node.name in paths:
            raise ValueError('duplicate GDTF geometry name')
        paths[node.name] = path
        if isinstance(node, pygdtf.GeometryReference):
            instances.append((node, path))
            return
        for child in getattr(node, 'geometries', ()):
            visit(child, path)

    visit(root, ())
    return paths, instances


def _reference_offset(reference, dmx_break: int) -> int:
    for entry in reference.breaks:
        if entry.dmx_break == dmx_break:
            return entry.dmx_offset.address - 1
    raise ValueError(f'GDTF geometry reference {reference.name} has no break {dmx_break}')


def _channels(fixture, mode, element):
    """(pygdtf channel, XML element, geometry name, geometry path, offset shift) in mode order.

    A channel on a referenced template geometry is instantiated once per GeometryReference,
    shifted by that reference's Break offset and renamed after the reference.
    """
    paths, instances = _geometry_paths(fixture, mode.geometry)
    referenced = {reference.geometry for reference, _ in instances}
    nodes = element.findall('DMXChannels/DMXChannel')
    if len(nodes) != len(mode._dmx_channels):
        raise ValueError('GDTF DMX channels could not be read')
    result = []
    for channel, node in zip(mode._dmx_channels, nodes, strict=True):
        if channel.geometry in referenced:
            for reference, path in instances:
                if reference.geometry == channel.geometry:
                    shift = _reference_offset(reference, _dmx_break(node))
                    result.append((channel, node, reference.name, path, shift))
        elif channel.geometry in paths:
            result.append((channel, node, channel.geometry, paths[channel.geometry], 0))
        else:
            raise ValueError('GDTF channel references geometry outside its selected mode')
    return result


def _dmx_break(node) -> int:
    if node.get('DMXBreak', '1') != '1':
        raise ValueError('GDTF multiple/overwritten DMX breaks are not supported')
    return 1


def _offsets(node) -> list[int]:
    text = node.get('Offset', 'None')
    if text in ('None', ''):
        raise ValueError('GDTF virtual channels are not supported')
    try:
        offsets = [int(value) for value in text.split(',')]
    except ValueError as error:
        raise ValueError(f'GDTF channel offset {text!r} is not a list of integers') from error
    if not 1 <= len(offsets) <= MAX_CHANNEL_BYTES:
        raise ValueError('GDTF channel resolution must be 8, 16, 24 or 32 bits')
    if any(not 1 <= offset <= MAX_DMX_CHANNELS for offset in offsets):
        raise ValueError('GDTF channel offset outside 1..512')
    return offsets


def _attribute(node) -> str:
    attribute = node.get('Attribute', NO_FEATURE)
    if '.' in attribute:
        raise ValueError('GDTF function attribute must be a single name')
    return attribute


def _wheel_colour(fixture, wheels, function, set_node) -> rig_wire.CieColor | None:
    index = int(set_node.get('WheelSlotIndex', '0'))
    if index <= 0:
        return None
    wheel = next((w for w in fixture.wheels if w.name == str(function.wheel)), None)
    if wheel is None or wheel.name not in wheels:
        raise ValueError('GDTF wheel link is missing')
    slots = wheels[wheel.name].findall('Slot')
    if index > len(slots) or len(slots) != len(wheel.wheel_slots):
        raise ValueError('GDTF wheel slot is missing')
    slot = slots[index - 1]
    if slot.get('Filter'):
        raise ValueError('GDTF spectral wheel filters are unsupported')
    try:
        x, y, luminance = map(float, slot.get('Color', '0.3127,0.3290,100.0').split(','))
    except ValueError as error:
        raise ValueError('invalid GDTF CIE wheel colour') from error
    if not _finite(x, y, luminance) or y <= 0 or luminance < 0:
        raise ValueError('invalid GDTF CIE wheel colour')
    return rig_wire.CieColor(x=x, y=y, luminance=luminance)


def _functions(fixture, wheels, attributes, entry, names, width, entries):
    channel, node, geometry, _, _ = entry
    maximum = (1 << (width * DMX_BYTE_BITS)) - 1
    logical_nodes = node.findall('LogicalChannel')
    if not logical_nodes:
        raise ValueError('GDTF channel contains no functions')
    channel_name = f'{geometry}_{logical_nodes[0].get("Attribute", "")}'
    functions = []
    for logical, logical_node in zip(channel.logical_channels, logical_nodes, strict=True):
        if '.' in logical_node.get('Attribute', ''):
            raise ValueError('GDTF logical-channel attribute must be a single name')
        if logical.dmx_change_time_limit != 0 or logical.mib_fade != 0:
            raise ValueError('GDTF timed logical-channel constraints are unsupported')
        function_nodes = logical_node.findall('ChannelFunction')
        if not function_nodes:
            raise ValueError('GDTF logical channel has no functions')
        for index, (function, function_node) in enumerate(
            zip(logical.channel_functions, function_nodes, strict=True)
        ):
            if function_node.get('DMXProfile') or function_node.find('SubChannelSet') is not None:
                raise ValueError('GDTF DMX profiles and sub-channel sets are unsupported')
            physical_from = float(function_node.get('PhysicalFrom', '0'))
            physical_to = float(function_node.get('PhysicalTo', '1'))
            if not _finite(physical_from, physical_to):
                raise ValueError('GDTF physical range must be finite')
            attribute = _attribute(function_node)
            if attribute != NO_FEATURE and attribute not in attributes:
                raise ValueError(f'missing GDTF attribute definition {attribute}')
            first = DmxValue.parse(function_node.get('DMXFrom')).widened(width)
            name = function_node.get('Name') or f'{attribute}{index + 1}'
            compiled = rig_wire.ChannelFunction(
                path=f'{channel_name}.{logical_node.get("Attribute", "")}.{name}',
                attribute=attribute,
                qualified_attribute=f'{geometry}.{attribute}',
                dmx_from=first,
                physical_from=physical_from,
                physical_to=physical_to,
                snap=logical_node.get('Snap', 'No') in SNAP_ON,
            )
            if (master := function_node.get('ModeMaster')) is not None:
                compiled.mode_master.CopyFrom(_mode_master(master, function_node, names, entries))
            sets = sorted(
                (DmxValue.parse(set_node.get('DMXFrom')).widened(width), position, set_node)
                for position, set_node in enumerate(function_node.findall('ChannelSet'))
            )
            for position, (set_first, _, set_node) in enumerate(sets):
                if position + 1 < len(sets):
                    if sets[position + 1][0] <= set_first:
                        raise ValueError('GDTF channel sets must have increasing ranges')
                    set_last = sets[position + 1][0] - 1
                else:
                    set_last = maximum
                if not first <= set_first <= set_last:
                    raise ValueError('GDTF channel set exceeds function range')
                set_from = float(set_node.get('PhysicalFrom', physical_from))
                set_to = float(set_node.get('PhysicalTo', physical_to))
                if not _finite(set_from, set_to):
                    raise ValueError('nonfinite GDTF channel-set range')
                entry_set = compiled.sets.add(
                    name=set_node.get('Name', ''),
                    dmx_from=set_first,
                    dmx_to=set_last,
                    physical_from=set_from,
                    physical_to=set_to,
                )
                if (colour := _wheel_colour(fixture, wheels, function, set_node)) is not None:
                    entry_set.wheel_slot_colour.CopyFrom(colour)
            functions.append(compiled)
    paths = [function.path for function in functions]
    if len(paths) != len(set(paths)):
        raise ValueError('duplicate GDTF channel-function path')
    return functions


def _mode_master(link: str, function_node, names, entries) -> rig_wire.ModeMaster:
    parts = link.split('.')
    master = names.get(parts[0])
    if master is None:
        raise ValueError('GDTF ModeMaster references a missing channel/function')
    _, node, _, _, _ = entries[master]
    function = None
    if len(parts) > 1:
        if len(parts) != 3:
            raise ValueError('GDTF ModeMaster references a missing channel/function')
        position = 0
        for logical in node.findall('LogicalChannel'):
            for candidate in logical.findall('ChannelFunction'):
                if logical.get('Attribute') == parts[1] and candidate.get('Name') == parts[2]:
                    function = position if function is None else function
                position += 1
        if function is None:
            raise ValueError('GDTF ModeMaster references a missing channel/function')
    offsets = node.get('Offset', 'None')
    if offsets in ('None', ''):
        raise ValueError('virtual ModeMaster is unsupported')
    width = len(offsets.split(','))
    first = DmxValue.parse(function_node.get('ModeFrom', '0/1')).widened(width)
    last = DmxValue.parse(function_node.get('ModeTo', '0/1')).widened(width)
    if first > last:
        raise ValueError('invalid GDTF ModeMaster interval')
    compiled = rig_wire.ModeMaster(channel=master, dmx_from=first, dmx_to=last)
    if function is not None:
        compiled.function = function
    return compiled


def _initial_default(node, channel_name: str, width: int) -> int:
    logical_nodes = node.findall('LogicalChannel')
    initial = node.get('InitialFunction')
    if initial is None:
        functions = logical_nodes[0].findall('ChannelFunction') if logical_nodes else []
        if not functions:
            raise ValueError('GDTF initial function is missing')
        return DmxValue.parse(functions[0].get('Default')).widened(width)
    parts = initial.split('.')
    if len(parts) != 3 or parts[0] != channel_name:
        raise ValueError('GDTF initial function is missing')
    for logical in logical_nodes:
        if logical.get('Attribute') == parts[1]:
            for function in logical.findall('ChannelFunction'):
                if function.get('Name') == parts[2]:
                    return DmxValue.parse(function.get('Default')).widened(width)
    raise ValueError('GDTF initial function is missing')


def compile_mode(description: str, mode_name: str) -> rig_wire.DmxMode:
    """The selected mode as Starfire encodes it; raises ValueError naming what is unsupported."""
    root = mvr.gdtf_root(description, 'GDTF')
    version = root.get('DataVersion', '')
    major, _, minor = version.partition('.')
    if not (major == '1' and minor.isdigit() and int(minor) <= 2):
        raise ValueError('unsupported GDTF version')
    elements = {node.get('Name'): node for node in root.iter('DMXMode')}
    if mode_name not in elements:
        raise ValueError(f'unknown GDTF DMX mode {mode_name}')
    element = elements[mode_name]
    for node in element.findall('DMXChannels/DMXChannel'):
        _dmx_break(node)
        _offsets(node)
    try:
        fixture = pygdtf.FixtureType(dsc_file=io.BytesIO(description.encode('utf-8')))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f'GDTF description could not be read: {error!r}') from error
    mode = fixture.dmx_modes.get_mode_by_name(mode_name)
    if mode is None:
        raise ValueError(f'unknown GDTF DMX mode {mode_name}')
    if element.find('Relations/Relation') is not None:
        raise ValueError('GDTF channel relations are not supported')
    if element.find('FTMacros/FTMacro') is not None:
        raise ValueError('GDTF fixture macros are not supported')
    entries = _channels(fixture, mode, element)
    if not 1 <= len(entries) <= MAX_DMX_CHANNELS:
        raise ValueError('GDTF mode must contain 1..512 physical channels')
    names = {}
    for index, (_, node, geometry, _, _) in enumerate(entries):
        logical = node.find('LogicalChannel')
        name = f'{geometry}_{logical.get("Attribute", "") if logical is not None else ""}'
        if name in names:
            raise ValueError('duplicate GDTF channel name')
        names[name] = index
    attributes = {attribute.name for attribute in fixture.attributes}
    wheels = {node.get('Name'): node for node in root.findall('FixtureType/Wheels/Wheel')}
    compiled, occupied, function_count = rig_wire.DmxMode(), set(), 0
    for entry in entries:
        _, node, geometry, path, shift = entry
        offsets = [offset + shift for offset in _offsets(node)]
        for offset in offsets:
            if not 1 <= offset <= MAX_DMX_CHANNELS:
                raise ValueError('GDTF channel offset outside 1..512')
            if offset in occupied:
                raise ValueError('overlapping GDTF channel offsets')
            occupied.add(offset)
        logical = node.find('LogicalChannel')
        channel_name = f'{geometry}_{logical.get("Attribute", "") if logical is not None else ""}'
        default = _initial_default(node, channel_name, len(offsets))
        functions = _functions(fixture, wheels, attributes, entry, names, len(offsets), entries)
        for function in functions:
            brightness = function.attribute == 'Dimmer' or function.attribute.startswith(
                BRIGHTNESS_PREFIXES
            )
            continuous = (
                len(functions) == 1
                and not function.sets
                and not function.HasField('mode_master')
                and function.dmx_from == 0
                and function.physical_from == 0.0
                and function.physical_to > 0.0
            )
            if brightness and not continuous:
                raise ValueError(
                    f'GDTF brightness attribute {function.path} requires one continuous '
                    'zero-based function without ChannelSets or ModeMaster'
                )
        function_count += len(functions)
        if function_count > MAX_FUNCTIONS:
            raise ValueError('GDTF channel-function limit exceeded')
        compiled.channels.add(
            geometry_path=list(path), offsets=offsets, default=default, functions=functions
        )
    return compiled
