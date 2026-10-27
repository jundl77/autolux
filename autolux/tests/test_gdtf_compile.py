"""The GDTF compiler emits the DMX semantics Starfire encodes from, or refuses with a reason."""

import pytest

from autolux.lighting.gdtf_compile import DmxValue, compile_mode
from tools.compile_test_profiles import OUTPUT, compiled_profiles

ATTRIBUTES = ('Fog1', 'Gobo1', 'Zoom', 'Mode', 'Dimmer', 'Pan', 'ColorAdd_R')


def description(channels: str, wheels: str = '') -> str:
    attributes = ''.join(
        f'<Attribute Name="{name}" Pretty="{name}" Feature="Control.Control" PhysicalUnit="None"/>'
        for name in ATTRIBUTES
    )
    identity = 'FixtureTypeID="85176e68-73d4-4217-9bfd-7791b08b01ef"'
    return (
        '<GDTF DataVersion="1.2"><FixtureType Name="Test" ShortName="Test" LongName="Test" '
        f'Manufacturer="Generic" Description="Test" {identity}>'
        '<AttributeDefinitions><FeatureGroups><FeatureGroup Name="Control" Pretty="Control">'
        f'<Feature Name="Control"/></FeatureGroup></FeatureGroups><Attributes>{attributes}'
        f'</Attributes></AttributeDefinitions><Wheels>{wheels}</Wheels>'
        '<Geometries><Geometry Name="Base" Position="{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}">'
        '<Geometry Name="Head1" Position="{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}"/></Geometry>'
        '</Geometries><DMXModes><DMXMode Name="Control" Geometry="Base"><DMXChannels>'
        f'{channels}</DMXChannels></DMXMode></DMXModes></FixtureType></GDTF>'
    )


def channel(attribute: str, offsets: str, functions: str, extra: str = '') -> str:
    return (
        f'<DMXChannel Offset="{offsets}" Geometry="Base" {extra}>'
        f'<LogicalChannel Attribute="{attribute}">{functions}</LogicalChannel></DMXChannel>'
    )


def function(attribute: str, extra: str = '', name: str = 'Control') -> str:
    return (
        f'<ChannelFunction Name="{name}" Attribute="{attribute}" PhysicalFrom="0" '
        f'PhysicalTo="100" {extra}/>'
    )


def compiled(channels: str, **kwargs):
    return compile_mode(description(channels, **kwargs), 'Control')


@pytest.mark.parametrize(
    ('text', 'width', 'expected'),
    [('18/1', 2, 0x1212), ('18/1s', 2, 0x1200), ('255/1', 3, 0xFFFFFF), ('258/2', 1, 1)],
)
def test_dmx_values_mirror_or_shift_to_their_channel_resolution(text, width, expected):
    assert DmxValue.parse(text).widened(width) == expected


def test_defaults_come_from_the_initial_function_at_channel_resolution():
    for default, expected in (('18/1', 0x1212), ('18/1s', 0x1200)):
        mode = compiled(channel('Fog1', '1,2', function('Fog1', f'Default="{default}"')))
        assert mode.channels[0].default == expected
        assert list(mode.channels[0].offsets) == [1, 2]


@pytest.mark.parametrize('literal', ['256/1', '65536/2', '0/8', '12', 'x/1'])
def test_oversized_or_malformed_dmx_literals_are_refused(literal):
    with pytest.raises(ValueError):
        compiled(channel('Fog1', '1', function('Fog1', f'Default="{literal}"')))


@pytest.mark.parametrize(
    ('change', 'reason'),
    [
        ({'offsets': '0'}, 'outside 1..512'),
        ({'offsets': '1,1'}, 'overlapping'),
        ({'offsets': '513'}, 'outside 1..512'),
        ({'offsets': '1,2,3,4,5'}, '8, 16, 24 or 32 bits'),
        ({'offsets': 'None'}, 'virtual channels'),
        ({'extra': 'DMXBreak="2"'}, 'DMX breaks'),
    ],
)
def test_malformed_channels_are_refused_with_their_reason(change, reason):
    arguments = {'offsets': '1', 'extra': '', **change}
    xml = channel('Fog1', arguments['offsets'], function('Fog1'), arguments['extra'])
    with pytest.raises(ValueError, match=reason):
        compiled(xml)


def test_xml_hazards_and_unknown_modes_are_refused():
    xml = description(channel('Fog1', '1', function('Fog1')))
    with pytest.raises(ValueError, match='unknown GDTF DMX mode'):
        compile_mode(xml, 'missing')
    with pytest.raises(ValueError, match='DTD'):
        compile_mode('<!DOCTYPE x>' + xml, 'Control')
    nested = '<x>' * 70 + '</x>' * 70
    with pytest.raises(ValueError, match='depth'):
        compile_mode(xml.replace('<Wheels></Wheels>', f'<Wheels>{nested}</Wheels>'), 'Control')


def test_mode_masters_resolve_to_channel_and_function_indices_at_master_resolution():
    dependent = channel(
        'Fog1',
        '1',
        function('Fog1', 'ModeMaster="Base_Mode.Mode.Enabled" ModeFrom="1/1" ModeTo="2/1"'),
    )
    master = channel(
        'Mode',
        '2,3',
        function('Mode', name='Idle') + function('Mode', 'DMXFrom="128/1"', name='Enabled'),
    )
    mode = compiled(dependent + master)
    condition = mode.channels[0].functions[0].mode_master
    assert (condition.channel, condition.function) == (1, 1)
    assert (condition.dmx_from, condition.dmx_to) == (0x0101, 0x0202)
    assert [f.dmx_from for f in mode.channels[1].functions] == [0, 0x8080]
    assert [f.path for f in mode.channels[1].functions] == [
        'Base_Mode.Mode.Idle',
        'Base_Mode.Mode.Enabled',
    ]
    whole = channel(
        'Fog1', '1', function('Fog1', 'ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1"')
    )
    assert not compiled(whole + master).channels[0].functions[0].mode_master.HasField('function')
    with pytest.raises(ValueError, match='ModeMaster'):
        compiled(channel('Fog1', '1', function('Fog1', 'ModeMaster="Base_Missing"')))


def test_channel_sets_are_sorted_bounded_and_take_the_function_physical_range():
    sets = (
        '<ChannelFunction Name="Gobo" Attribute="Gobo1" PhysicalFrom="0" PhysicalTo="4" '
        'Wheel="Gobos"><ChannelSet Name="Stars" DMXFrom="128/1" WheelSlotIndex="2"/>'
        '<ChannelSet Name="Open" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="1" '
        'WheelSlotIndex="1"/></ChannelFunction>'
    )
    wheel = '<Wheel Name="Gobos"><Slot Name="Open" Color="0.3127,0.3290,100"/><Slot Name="Stars"/></Wheel>'
    [function_] = compiled(channel('Gobo1', '1', sets), wheels=wheel).channels[0].functions
    assert [(s.name, s.dmx_from, s.dmx_to) for s in function_.sets] == [
        ('Open', 0, 127),
        ('Stars', 128, 255),
    ]
    assert [(s.physical_from, s.physical_to) for s in function_.sets] == [(0, 1), (0, 4)]
    colour = function_.sets[0].wheel_slot_colour
    assert (colour.x, colour.y, colour.luminance) == (0.3127, 0.329, 100.0)
    filtered = wheel.replace('<Slot Name="Stars"/>', '<Slot Name="Stars" Filter="Red"/>')
    with pytest.raises(ValueError, match='spectral wheel filters'):
        compiled(channel('Gobo1', '1', sets), wheels=filtered)


def test_brightness_functions_must_be_one_continuous_zero_based_range():
    sets = function('Dimmer').replace(
        '/>', '><ChannelSet Name="Full" DMXFrom="0/1"/></ChannelFunction>'
    )
    with pytest.raises(ValueError, match='brightness attribute'):
        compiled(channel('Dimmer', '1', sets))


def test_committed_test_profiles_are_the_compiled_test_descriptions():
    assert OUTPUT.read_bytes() == compiled_profiles(), (
        'Regenerate with: uv run python tools/compile_test_profiles.py'
    )
