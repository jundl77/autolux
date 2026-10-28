import pytest

from autolux.lighting.palettes import (
    BREAKDOWN_PALETTES,
    MONO_COLD_LEVEL,
    PALETTES,
    base_palette_name,
    palette_table,
)
from cli.starfire_assets import palette_definitions


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
