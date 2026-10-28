import re
from pathlib import Path

import pytest

from autolux.lighting.gdtf_compile import compile_mode
from autolux.lighting.palettes import (
    BLACK,
    BREAKDOWN_PALETTES,
    MAX_PALETTE_NAME_BYTES,
    MAX_PALETTES,
    MONO_COLD_LEVEL,
    PALETTES,
    VARIANT_SUFFIXES,
    Palette,
    PaletteError,
    base_palette_name,
    load_palettes,
    nearest_slot,
    palette_table,
    wheel_collisions,
    wheel_slots,
)
from cli.starfire_assets import palette_definitions
from proto.starfire import rig_pb2 as rig_wire

REPO = Path(__file__).resolve().parents[2]
GDTF_DIR = REPO / 'autolux' / 'tests' / 'data' / 'gdtf'


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


def palette(primary, secondary) -> Palette:
    return Palette('test', 0.0, 0.0, primary, secondary, (1.0, 1.0, 1.0), (0.5, 0.5, 1.0))


def rig(*patched: tuple[str, rig_wire.DmxMode]) -> rig_wire.RigDefinition:
    definition = rig_wire.RigDefinition()
    for index, (name, mode) in enumerate(patched):
        definition.profiles.add(profile_id=name, mode=mode)
        definition.fixtures.add(fixture_uuid=f'f{index}', profile_id=name, address=1 + 8 * index)
    return definition


def wheel_spinner() -> rig_wire.DmxMode:
    description = (GDTF_DIR / 'wheel_spinner.xml').read_text(encoding='utf-8')
    return compile_mode(description, '3ch')


def test_a_wheel_fixture_that_shows_primary_and_secondary_as_one_slot_is_reported():
    red, orange, blue = (1.0, 0.0, 0.0), (1.0, 0.5, 0.0), (0.0, 0.0, 1.0)
    two_tone, one_slot = palette(red, blue), palette(red, orange)
    [collision] = wheel_collisions(rig(('wheel', wheel_spinner())), (two_tone, one_slot))
    assert (
        collision
        == 'wheel: palette test shows primary and secondary on the same wheel slots (Red).'
    )
    assert wheel_collisions(rig(('wheel', wheel_spinner())), (two_tone,)) == []


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
    green, yellow = (0.0, 1.0, 0.0), (0.6, 1.0, 0.0)
    # Green and yellow share Color1's green slot but not Color2's slots.
    assert [nearest_slot(slots, yellow) for slots in wheel_slots(mode)] == ['Green', 'Red']
    assert [nearest_slot(slots, green) for slots in wheel_slots(mode)] == ['Green', 'Black']
    red, orange = (1.0, 0.0, 0.0), (1.0, 0.5, 0.0)
    assert wheel_collisions(rig(('wheels', mode)), (palette(green, yellow),)) == []
    assert wheel_collisions(rig(('wheels', mode)), (palette(red, orange),)) == [
        'wheels: palette test shows primary and secondary on the same wheel slots (Red, Red).'
    ]


def test_a_slot_starfire_would_refuse_is_skipped_rather_than_crashing_login():
    mode = wheel_spinner()
    mode.channels[0].functions[0].sets[1].wheel_slot_colour.y = 0.0
    [slots] = wheel_slots(mode)
    assert [name for name, _ in slots] == ['Black', 'Green', 'Blue']
    assert (
        wheel_collisions(rig(('wheel', mode)), (palette((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),)) == []
    )


def test_slot_colours_and_nearest_slots_match_starfires_encode():
    [slots] = wheel_slots(wheel_spinner())
    expected = {'Black': BLACK, 'Red': (1, 0, 0), 'Green': (0, 1, 0), 'Blue': (0, 0, 1)}
    for name, rgb in slots:
        assert rgb == pytest.approx(expected[name], abs=1e-6)
    rust = (REPO / 'starfire/tests/vocabulary_tests.rs').read_text(encoding='utf-8')
    block = re.search(r'const WHEEL_SPINNER_PICKS[^=]*= \[(.*?)\];', rust, re.DOTALL).group(1)
    picks = re.findall(r'\(\[([^\]]*)\], (\d+)\)', block)
    assert len(picks) == 9
    by_dmx = {s.dmx_from: s.name for s in wheel_spinner().channels[0].functions[0].sets}
    for colour, dmx in picks:
        requested = tuple(float(channel) for channel in colour.split(','))
        assert nearest_slot(slots, requested) == by_dmx[int(dmx)], colour


def test_mixing_and_unpatched_wheels_are_not_reported():
    mixing = wheel_spinner()
    channel = mixing.channels.add(geometry_path=['Base'], offsets=[4])
    channel.functions.add(path='Base_ColorAdd_R.ColorAdd_R.ColorAdd_R', attribute='ColorAdd_R')
    one_slot = palette((1.0, 0.0, 0.0), (1.0, 0.5, 0.0))
    assert wheel_collisions(rig(('mixing', mixing)), (one_slot,)) == []
    unpatched = rig()
    unpatched.profiles.add(profile_id='wheel', mode=wheel_spinner())
    assert wheel_collisions(unpatched, (one_slot,)) == []
