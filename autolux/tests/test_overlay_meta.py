import pytest

from autolux.lib.engine.effect_definitions import OverlayMeta
from autolux.lighting.autoloop_catalogue import (
    AutoloopMetaError,
    overlay_autoloops,
    parse_overlay_meta,
    read_autoloop_meta,
    read_overlay_meta,
    required_autoloops,
)

HEADER = '-- Washes at full white, decaying.\n-- strobe: no\n-- intensity: htp\n'


@pytest.mark.parametrize(
    ('header', 'meta'),
    [
        (HEADER, OverlayMeta(strobe=False, intensity_htp=True)),
        (
            '-- x\n-- intensity: ltp\n-- strobe: yes\n',
            OverlayMeta(strobe=True, intensity_htp=False),
        ),
        (HEADER.replace('\n', '\r\n'), OverlayMeta(strobe=False, intensity_htp=True)),
    ],
)
def test_the_header_block_after_the_first_line_is_the_overlay_metadata(header, meta):
    assert parse_overlay_meta(header + '-- why: the hit.\nreturn {}\n') == meta


@pytest.mark.parametrize(
    ('header', 'message'),
    [
        (HEADER.replace('-- intensity: htp\n', ''), 'missing intensity'),
        ('-- strobe: no\n-- intensity: htp\n', 'missing strobe'),
        (HEADER.replace('strobe: no', 'strobe: maybe'), "strobe 'maybe' is not one of yes, no"),
        (HEADER.replace('intensity: htp', 'intensity: HTP'), "intensity 'HTP' is not one of"),
        (HEADER + '-- strobe: yes\n', 'strobe declared twice'),
        (HEADER.replace('-- strobe: no\n', '-- energy: 2\n-- strobe: no\n'), 'missing strobe'),
    ],
)
def test_a_bad_overlay_header_names_what_is_wrong(header, message):
    with pytest.raises(AutoloopMetaError, match=message):
        parse_overlay_meta(header)


def test_overlay_banks_are_required_read_on_their_own_and_reported_at_once(tmp_path):
    for bank, name, source in [
        ('hit', 'blinder', HEADER),
        ('riser', 'strobe_ramp', HEADER.replace('strobe: no', 'strobe: yes')),
        ('dip', 'last_beat', HEADER.replace('intensity: htp', 'intensity: max')),
        ('fill', 'wipe', '-- A wipe.\nreturn {}\n'),
    ]:
        (tmp_path / bank).mkdir()
        (tmp_path / bank / f'{name}.lua').write_text(source)
    loops = {('hit', 'blinder'), ('riser', 'strobe_ramp'), ('dip', 'last_beat'), ('fill', 'wipe')}
    assert overlay_autoloops(tmp_path) == loops
    assert loops <= required_autoloops(tmp_path)
    assert read_autoloop_meta(tmp_path) == {}
    with pytest.raises(AutoloopMetaError) as error:
        read_overlay_meta(tmp_path)
    assert "dip/last_beat: intensity 'max'" in str(error.value)
    assert 'fill/wipe: missing strobe, intensity' in str(error.value)
    assert 'hit/blinder' not in str(error.value)
    (tmp_path / 'dip' / 'last_beat.lua').unlink()
    (tmp_path / 'fill' / 'wipe.lua').unlink()
    assert read_overlay_meta(tmp_path) == {
        ('hit', 'blinder'): OverlayMeta(strobe=False, intensity_htp=True),
        ('riser', 'strobe_ramp'): OverlayMeta(strobe=True, intensity_htp=True),
    }


def test_every_shipped_overlay_declares_metadata():
    assert set(read_overlay_meta()) == overlay_autoloops()
