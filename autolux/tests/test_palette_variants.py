from pathlib import Path

import pytest

from autolux.lighting.gdtf_compile import compile_mode
from autolux.lighting.palettes import (
    BREAKDOWN_PALETTES,
    MONO_COLD_LEVEL,
    PALETTES,
    Palette,
    base_palette_name,
    palette_table,
    wheel_collisions,
)
from cli.starfire_assets import palette_definitions
from proto.starfire import rig_pb2 as rig_wire

GDTF_DIR = Path(__file__).parent / 'data' / 'gdtf'


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
        collision == "wheel: palette test shows primary and secondary as the one wheel slot 'Red'."
    )
    assert wheel_collisions(rig(('wheel', wheel_spinner())), (two_tone,)) == []


def test_mixing_and_unpatched_wheels_are_not_reported():
    mixing = wheel_spinner()
    channel = mixing.channels.add(geometry_path=['Base'], offsets=[4])
    channel.functions.add(path='Base_ColorAdd_R.ColorAdd_R.ColorAdd_R', attribute='ColorAdd_R')
    one_slot = palette((1.0, 0.0, 0.0), (1.0, 0.5, 0.0))
    assert wheel_collisions(rig(('mixing', mixing)), (one_slot,)) == []
    unpatched = rig()
    unpatched.profiles.add(profile_id='wheel', mode=wheel_spinner())
    assert wheel_collisions(unpatched, (one_slot,)) == []
