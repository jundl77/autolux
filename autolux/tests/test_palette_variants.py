import colorsys
import math
import re
from pathlib import Path

import pytest

from autolux.lighting.gdtf_compile import compile_mode
from autolux.lighting.palettes import (
    BLACK,
    BREAKDOWN_PALETTES,
    DROP_PALETTES,
    MAX_PALETTE_NAME_BYTES,
    MAX_PALETTES,
    MIN_COLOUR_SATURATION,
    MONO_COLD_LEVEL,
    PALETTES,
    SAME_HUE_DEGREES,
    VARIANT_SUFFIXES,
    WHITE,
    Palette,
    PaletteError,
    base_palette_name,
    load_palettes,
    nearest_slot,
    palette_table,
    wheel_slots,
    wheel_warnings,
)
from cli.starfire_assets import palette_definitions
from proto.starfire import rig_pb2 as rig_wire

REPO = Path(__file__).resolve().parents[2]
GDTF_DIR = REPO / 'autolux' / 'tests' / 'data' / 'gdtf'
STAGE_RIG = REPO / 'autolux' / 'tests' / 'data' / 'generic_rig' / 'stage.rig.pb'


@pytest.mark.parametrize('palette', PALETTES + BREAKDOWN_PALETTES, ids=lambda p: p.name)
def test_every_base_and_breakdown_palette_has_a_mono_variant_of_its_primary(palette):
    mono = {variant.name: variant for variant in palette_table()}[f'{palette.name}_mono']
    assert mono.secondary == mono.accent == mono.primary == palette.primary
    assert mono.cold == tuple(channel * MONO_COLD_LEVEL for channel in palette.primary)
    assert mono.black == palette.black
    assert (mono.weight, mono.drive) == (palette.weight, palette.drive)
    assert base_palette_name(mono.name) == base_palette_name(palette.name)


def test_login_carries_every_mono_variant():
    sent = [palette.name for palette in palette_definitions()]
    assert sent == [palette.name for palette in palette_table()]
    for palette in PALETTES + BREAKDOWN_PALETTES:
        assert f'{palette.name}_mono' in sent
    assert not any(name.endswith('_drop_mono') for name in sent)


PALETTE_TOML = """number = {number}

[slots]
primary = "#ffd199"
secondary = "#ff8c1a"
accent = "#bf0005"
cold = "#bfd9ff"
black = "#000000"

[mood]
weight = 0.2
drive = 0.9
"""


def test_the_login_table_fits_starfires_palette_limits():
    names = [palette.name for palette in palette_table()]
    assert len(names) <= MAX_PALETTES
    assert all(len(name.encode()) <= MAX_PALETTE_NAME_BYTES for name in names)
    assert names[: len(VARIANT_SUFFIXES)] == [PALETTES[0].name + s for s in VARIANT_SUFFIXES]


def test_a_name_whose_longest_variant_overflows_starfire_is_refused(tmp_path):
    longest = 'x' * (MAX_PALETTE_NAME_BYTES - len(max(VARIANT_SUFFIXES, key=len)))
    (tmp_path / f'{longest}.toml').write_text(PALETTE_TOML.format(number=1))
    assert len(load_palettes(tmp_path)[0]) == 1
    (tmp_path / f'{longest}.toml').rename(tmp_path / f'{longest}y.toml')
    with pytest.raises(PaletteError, match=f'{longest}y_breakdown_mono is longer than 64 bytes'):
        load_palettes(tmp_path)


def test_more_palettes_than_starfires_table_holds_are_refused(tmp_path):
    fitting = MAX_PALETTES // len(VARIANT_SUFFIXES)
    for number in range(1, fitting + 1):
        (tmp_path / f'p{number}.toml').write_text(PALETTE_TOML.format(number=number))
    assert len(load_palettes(tmp_path)[0]) == fitting
    (tmp_path / 'extra.toml').write_text(PALETTE_TOML.format(number=fitting + 1))
    with pytest.raises(PaletteError, match=f'Starfire takes at most {MAX_PALETTES}'):
        load_palettes(tmp_path)


RED, ORANGE, GREEN, YELLOW, BLUE = (
    (1.0, 0.0, 0.0),
    (1.0, 0.5, 0.0),
    (0.0, 1.0, 0.0),
    (0.6, 1.0, 0.0),
    (0.0, 0.0, 1.0),
)


def palette(name, primary, secondary, accent=(1.0, 0.0, 0.5)) -> Palette:
    return Palette(name, 0.0, 0.0, primary, secondary, accent, (0.5, 0.5, 1.0))


def rig(*patched: tuple[str, rig_wire.DmxMode]) -> rig_wire.RigDefinition:
    definition = rig_wire.RigDefinition()
    for index, (name, mode) in enumerate(patched):
        definition.profiles.add(profile_id=name, mode=mode)
        definition.fixtures.add(fixture_uuid=f'f{index}', profile_id=name, address=1 + 8 * index)
    return definition


def wheel_spinner() -> rig_wire.DmxMode:
    description = (GDTF_DIR / 'wheel_spinner.xml').read_text(encoding='utf-8')
    return compile_mode(description, '3ch')


def white_for_green() -> rig_wire.DmxMode:
    """wheel_spinner with an open white slot in place of green."""
    mode = wheel_spinner()
    open_slot = mode.channels[0].functions[0].sets[2]
    open_slot.name = 'Open'
    open_slot.wheel_slot_colour.CopyFrom(rig_wire.CieColor(x=0.3127, y=0.329, luminance=100.0))
    return mode


def test_a_wheel_that_shows_primary_and_secondary_as_one_slot_is_reported():
    palettes = (palette('two_tone', RED, BLUE), palette('one_slot', RED, ORANGE))
    assert wheel_warnings(rig(('wheel', wheel_spinner())), palettes) == [
        'wheel: primary and secondary share one wheel slot in one_slot (Red).'
    ]


def test_a_palette_of_one_hue_on_purpose_is_not_reported():
    pale_red = (1.0, 0.6, 0.6)
    mono = palette('mono', RED, pale_red)
    assert wheel_warnings(rig(('wheel', wheel_spinner())), (mono,)) == []


def test_a_pale_colour_takes_its_nearest_coloured_slot_and_only_white_takes_open():
    [slots] = wheel_slots(white_for_green())
    pale = (1.0, 0.95, 0.9)
    assert nearest_slot(slots, pale) == 'Red'
    assert nearest_slot(slots, WHITE) == nearest_slot(slots, (0.5, 0.5, 0.5)) == 'Open'
    # A wheel with no coloured slot keeps the plain nearest rule: a pale colour shows white.
    assert nearest_slot([('Black', BLACK), ('Open', WHITE)], pale) == 'Open'
    palettes = (palette('pale_red', pale, RED), palette('fire', RED, ORANGE))
    patched = rig(('wheel', white_for_green()), ('plain', wheel_spinner()))
    assert wheel_warnings(patched, palettes) == [
        'wheel: primary and secondary share one wheel slot in pale_red (Red), fire (Red).',
        'plain: primary and secondary share one wheel slot in pale_red (Red), fire (Red).',
    ]


def rust_table(name: str) -> list[tuple[tuple[float, ...], int]]:
    rust = (REPO / 'starfire/tests/vocabulary_tests.rs').read_text(encoding='utf-8')
    block = re.search(rf'const {name}[^=]*= \[(.*?)\];', rust, re.DOTALL).group(1)
    return [
        (tuple(float(channel) for channel in colour.split(',')), int(dmx))
        for colour, dmx in re.findall(r'\(\[([^\]]*)\], (\d+)\)', block)
    ]


def test_the_stage_spot_takes_the_slots_starfires_encode_picks():
    stage = rig_wire.RigDefinition.FromString((STAGE_RIG).read_bytes())
    spot = next(p for p in stage.profiles if 'Intimidator Spot 110' in p.profile_id)
    [slots] = wheel_slots(spot.mode)
    by_dmx = {
        s.dmx_from: s.name
        for channel in spot.mode.channels
        for function in channel.functions
        if function.attribute == 'Color1'
        for s in function.sets
    }
    picks = rust_table('SPOT_PICKS')
    assert len(picks) == 9
    for colour, dmx in picks:
        assert nearest_slot(slots, colour) == by_dmx[dmx], colour


def test_the_white_slot_threshold_matches_starfires():
    gdtf = (REPO / 'starfire/src/gdtf.rs').read_text(encoding='utf-8')
    threshold = re.search(r'const WHITE_SLOT_SATURATION: f64 = ([0-9.]+);', gdtf).group(1)
    assert float(threshold) == MIN_COLOUR_SATURATION


def two_wheels() -> rig_wire.DmxMode:
    mode = wheel_spinner()
    second = rig_wire.DmxChannel()
    second.CopyFrom(mode.channels[0])
    second.offsets[:] = [4]
    for function in second.functions:
        function.attribute = 'Color2'
        function.path = function.path.replace('Color1', 'Color2')
        function.qualified_attribute = function.qualified_attribute.replace('Color1', 'Color2')
    # A second wheel without green, its red slot reaching up to blue.
    second.functions[0].sets[1].dmx_to = second.functions[0].sets[2].dmx_to
    del second.functions[0].sets[2]
    mode.channels.append(second)
    return mode


def test_every_wheel_is_checked_and_only_a_collision_on_all_of_them_is_reported():
    mode = two_wheels()
    assert [[name for name, _ in slots] for slots in wheel_slots(mode)] == [
        ['Black', 'Red', 'Green', 'Blue'],
        ['Black', 'Red', 'Blue'],
    ]
    # Green and yellow share Color1's green slot but not Color2's slots.
    assert [nearest_slot(slots, YELLOW) for slots in wheel_slots(mode)] == ['Green', 'Red']
    assert [nearest_slot(slots, GREEN) for slots in wheel_slots(mode)] == ['Green', 'Black']
    assert wheel_warnings(rig(('wheels', mode)), (palette('lime', GREEN, YELLOW),)) == []
    assert wheel_warnings(rig(('wheels', mode)), (palette('fire', RED, ORANGE),)) == [
        'wheels: primary and secondary share one wheel slot in fire (Red, Red).'
    ]


def test_a_slot_starfire_would_refuse_is_skipped_rather_than_crashing_login():
    mode = wheel_spinner()
    mode.channels[0].functions[0].sets[1].wheel_slot_colour.y = 0.0
    [slots] = wheel_slots(mode)
    assert [name for name, _ in slots] == ['Black', 'Green', 'Blue']
    assert wheel_warnings(rig(('wheel', mode)), (palette('two_tone', RED, BLUE),)) == []


def test_slot_colours_and_nearest_slots_match_starfires_encode():
    [slots] = wheel_slots(wheel_spinner())
    expected = {'Black': BLACK, 'Red': (1, 0, 0), 'Green': (0, 1, 0), 'Blue': (0, 0, 1)}
    for name, rgb in slots:
        assert rgb == pytest.approx(expected[name], abs=1e-6)
    picks = rust_table('WHEEL_SPINNER_PICKS')
    assert len(picks) == 9
    by_dmx = {s.dmx_from: s.name for s in wheel_spinner().channels[0].functions[0].sets}
    for colour, dmx in picks:
        assert nearest_slot(slots, colour) == by_dmx[dmx], colour


def test_mixing_and_unpatched_wheels_are_not_reported():
    mixing = wheel_spinner()
    channel = mixing.channels.add(geometry_path=['Base'], offsets=[4])
    channel.functions.add(path='Base_ColorAdd_R.ColorAdd_R.ColorAdd_R', attribute='ColorAdd_R')
    one_slot = palette('one_slot', RED, ORANGE)
    assert wheel_warnings(rig(('mixing', mixing)), (one_slot,)) == []
    unpatched = rig()
    unpatched.profiles.add(profile_id='wheel', mode=wheel_spinner())
    assert wheel_warnings(unpatched, (one_slot,)) == []


@pytest.mark.parametrize(
    ('section', 'slot', 'colour'),
    [
        ('slots', 'accent', '#ffffff'),
        ('slots', 'primary', '#808080'),
        ('drop', 'accent', '#fcfcff'),
    ],
)
def test_a_white_or_grey_palette_colour_is_refused_at_load(tmp_path, section, slot, colour):
    document = PALETTE_TOML.format(number=1) + '\n[drop]\naccent = "#ff0008"\n'
    if section == 'slots':
        document = re.sub(
            rf'^{slot} = .*$', f'{slot} = "{colour}"', document, flags=re.MULTILINE, count=1
        )
    else:
        document = document.replace('accent = "#ff0008"', f'accent = "{colour}"')
    (tmp_path / 'glow.toml').write_text(document)
    with pytest.raises(PaletteError, match=f'{section}.{slot} {colour} is white or grey'):
        load_palettes(tmp_path)


def test_every_shipped_palette_colour_clears_the_saturation_floor():
    for variant in PALETTES + BREAKDOWN_PALETTES + DROP_PALETTES:
        for role in ('primary', 'secondary', 'accent'):
            colour = getattr(variant, role)
            assert colorsys.rgb_to_hsv(*colour)[1] >= MIN_COLOUR_SATURATION, (variant.name, role)


def _chroma_distance(first, second) -> float:
    """Distance on the hue-saturation disc: hue counts as much as the colour carries it."""
    (h1, s1, _), (h2, s2, _) = colorsys.rgb_to_hsv(*first), colorsys.rgb_to_hsv(*second)
    angle = 2 * math.pi * (h1 - h2)
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - 2 * s1 * s2 * math.cos(angle)))


# Calm's colour bump paints the accent and a drop turns to the secondary; either must read as a
# change from the primary, and the secondary must not pass for the white of a hit.
MIN_CHROMA_DISTANCE = 0.3
MIN_SECONDARY_SATURATION = 0.35


@pytest.mark.parametrize('palette', PALETTES + DROP_PALETTES, ids=lambda p: p.name)
def test_accent_and_secondary_stand_apart_from_the_primary_and_from_white(palette):
    if _hue_degrees(palette.primary, palette.secondary) < SAME_HUE_DEGREES:
        pytest.skip('one hue on purpose')
    assert _chroma_distance(palette.primary, palette.accent) >= MIN_CHROMA_DISTANCE
    assert _chroma_distance(palette.primary, palette.secondary) >= MIN_CHROMA_DISTANCE
    assert colorsys.rgb_to_hsv(*palette.secondary)[1] >= MIN_SECONDARY_SATURATION


def _hue_degrees(first, second) -> float:
    turn = abs(colorsys.rgb_to_hsv(*first)[0] - colorsys.rgb_to_hsv(*second)[0])
    return 360 * min(turn, 1 - turn)
