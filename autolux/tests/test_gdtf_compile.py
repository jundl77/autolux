"""The GDTF compiler emits the DMX semantics Starfire encodes from, or refuses with a reason."""

from pathlib import Path

import pytest

from autolux.lighting import mvr
from autolux.lighting.gdtf_compile import DmxValue, compile_mode
from tools.compile_test_profiles import OUTPUT, compiled_profiles, corpus_descriptions

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
        ({'offsets': 'None'}, '1..512 physical channels'),
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
    preset = function('Dimmer').replace(
        '/>',
        '><ChannelSet Name="Full" DMXFrom="0/1" PhysicalFrom="100" PhysicalTo="100"/>'
        '</ChannelFunction>',
    )
    with pytest.raises(ValueError, match='Control has channel sets'):
        compiled(channel('Dimmer', '1', preset))
    labelled = function('Dimmer').replace(
        '/>',
        '><ChannelSet Name="min" DMXFrom="0/1"/><ChannelSet Name="" DMXFrom="1/1"/>'
        '<ChannelSet Name="max" DMXFrom="255/1"/></ChannelFunction>',
    )
    [dimmer] = compiled(channel('Dimmer', '1', labelled)).channels[0].functions
    assert not dimmer.sets


def test_gdtf_1_0_channel_defaults_and_unnamed_functions_follow_the_standard():
    unnamed = (
        '<ChannelFunction Attribute="Mode" PhysicalFrom="0" PhysicalTo="1"/>'
        '<ChannelFunction Attribute="Mode" Name="" DMXFrom="128/1" PhysicalFrom="0" '
        'PhysicalTo="1"/>'
    )
    master = channel('Mode', '2', unnamed, 'Default="200/1"')
    dependent = channel(
        'Fog1', '1', function('Fog1', 'ModeMaster="Base_Mode.Mode.Mode 2" ModeTo="255/1"')
    )
    mode = compiled(dependent + master)
    assert mode.channels[1].default == 200
    assert [f.path for f in mode.channels[1].functions] == [
        'Base_Mode.Mode.Mode 1',
        'Base_Mode.Mode.Mode 2',
    ]
    assert mode.channels[0].functions[0].mode_master.function == 1


def test_virtual_channels_carry_no_dmx_and_cannot_be_mode_masters():
    virtual = channel('Mode', 'None', function('Mode'))
    assert len(compiled(channel('Fog1', '1', function('Fog1')) + virtual).channels) == 1
    dependent = channel('Fog1', '1', function('Fog1', 'ModeMaster="Base_Mode" ModeTo="255/1"'))
    with pytest.raises(ValueError, match='missing or virtual channel'):
        compiled(dependent + virtual)


def test_dmx_profiles_compile_to_their_curve_points():
    xml = (Path(__file__).parent / 'data/gdtf/curved_dimmer.xml').read_text(encoding='utf-8')
    [point] = compile_mode(xml, '8 bit').channels[0].functions[0].dmx_profile
    assert (point.dmx_percentage, point.cfc1, point.cfc2) == (0, 0, 0.01)
    with pytest.raises(ValueError, match='DMX profile Missing'):
        compile_mode(xml.replace('DMXProfile="Square"', 'DMXProfile="Missing"'), '8 bit')


CORPUS = Path(__file__).parents[2] / 'tests/data/corpus/gdtf'
# Each corpus mode compiles (None) or is refused for exactly this reason.
CORPUS_OUTCOMES = {
    ('allWorking', 'Standard Mode (32 ch)'): 'ColorSub_C 1 shares its channel',
    ('BugInChannelSetDmxJumps', 'Mode 1 - Standard 16 bit'): 'spectral wheel filters',
    ('BugInChannelSetDmxJumps', 'Mode 2 - Reduced 8 bit'): 'spectral wheel filters',
    ('BugInDmxWithHex', 'Standard Mode (32 ch)'): 'unsupported GDTF version',
    ('CrashWhileOpen', 'Extended'): 'channel relations are not supported',
    ('robe_bmfl_spot', 'Mode 1 - Standard 16 bit'): 'duplicate GDTF channel-function path',
    ('robe_bmfl_spot', 'Mode 2 - Reduced 8 bit'): None,
    ('virtual_channel', 'Default'): None,
    ('WrongDmxValue', 'mode 1'): 'channel relations are not supported',
    ('WrongDmxValue', 'mode 2'): 'ColorAdd_R shares its channel',
    ('WrongDmxValue', 'mode 3'): 'ColorAdd_R shares its channel',
    ('WrongDmxValue', 'mode 4'): None,
    ('WrongDmxValue', 'mode 5'): 'channel relations are not supported',
    ('WrongDmxValue', 'mode 6'): 'channel relations are not supported',
}


def test_every_corpus_mode_compiles_or_is_refused_with_its_reason():
    outcomes = {}
    for stem, description in corpus_descriptions().items():
        for mode in mvr.gdtf_modes(description, stem):
            try:
                compile_mode(description, mode)
                outcomes[stem, mode] = None
            except ValueError as error:
                outcomes[stem, mode] = str(error)
    assert outcomes.keys() == CORPUS_OUTCOMES.keys()
    for key, reason in CORPUS_OUTCOMES.items():
        if reason is None:
            assert outcomes[key] is None, (key, outcomes[key])
        else:
            assert outcomes[key] is not None and reason in outcomes[key], (key, outcomes[key])


def test_committed_test_profiles_are_the_compiled_test_descriptions():
    assert OUTPUT.read_bytes() == compiled_profiles(), (
        'Regenerate with: uv run python tools/compile_test_profiles.py'
    )
