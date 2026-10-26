import pytest

from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    PREFERRED_AUTOLOOPS,
    AutoloopMeta,
    LightIntent,
)
from autolux.lighting.autoloop_catalogue import (
    AUTOLOOP_DIRECTORY,
    AutoloopMetaError,
    discover_autoloops,
    parse_autoloop_meta,
    read_autoloop_meta,
    required_autoloops,
)


def test_discovery_puts_the_preferred_look_first_then_the_rest_alphabetically(tmp_path):
    bank = tmp_path / 'drop'
    bank.mkdir()
    for name in ('slam', 'kick', 'chase', 'notes.txt'):
        (bank / (name if '.' in name else f'{name}.lua')).write_text('return {}')
    table = discover_autoloops(tmp_path)
    assert table[LightIntent.DROP] == (('drop', 'kick'), ('drop', 'chase'), ('drop', 'slam'))
    # An empty bank still lists its preferred look so readiness can name it missing.
    assert table[LightIntent.BUILDUP] == (('buildup', 'ramp'),)


def test_the_shipped_catalogue_covers_every_lua_file_with_the_originals_first():
    table = discover_autoloops()
    for intent, preferred in PREFERRED_AUTOLOOPS.items():
        assert table[intent][0] == (intent.value, preferred)
    on_disk = {(path.parent.name, path.stem) for path in AUTOLOOP_DIRECTORY.glob('*/*.lua')}
    assert required_autoloops() == on_disk


HEADER = '-- 8-beat loop, no strobe.\n-- energy: 2\n-- family: warm\n-- movement: snap\n'


def test_the_header_block_after_the_loop_length_line_is_the_metadata():
    source = HEADER + '-- wash: primary kick.\nlocal palette = sf.palette("techno")\n'
    assert parse_autoloop_meta(source) == AutoloopMeta(2, 'warm', 'snap')


@pytest.mark.parametrize(
    ('header', 'message'),
    [
        (HEADER.replace('-- family: warm\n', ''), 'missing family'),
        # A key after the group lines is not part of the header block.
        (HEADER.replace('-- energy: 2\n', '') + '-- wash: x\n-- energy: 2\n', 'missing energy'),
        (HEADER.replace('energy: 2', 'enrgy: 2'), 'missing energy'),
        (HEADER.replace('energy: 2', 'energy: 4'), "energy '4' is not one of 1, 2, 3"),
        # Exact spelling only, as the Starfire test reads it.
        (HEADER.replace('energy: 2', 'energy: 03'), "energy '03'"),
        (HEADER.replace('energy: 2', 'energy: 2 '), "energy '2 '"),
        (HEADER.replace('energy: 2', 'energy: high'), "energy 'high' is not one of"),
        (HEADER.replace('family: warm', 'family: hot'), "family 'hot' is not one of"),
        (HEADER.replace('movement: snap', 'movement: snap  -- jumps'), 'movement'),
        (HEADER + '-- family: cold\n', 'family declared twice'),
    ],
)
def test_a_bad_header_block_names_what_is_wrong(header, message):
    with pytest.raises(AutoloopMetaError, match=message):
        parse_autoloop_meta(header)


def test_reading_the_library_reports_every_bad_loop_at_once(tmp_path):
    for bank, name, source in [
        ('drop', 'kick', HEADER),
        ('drop', 'slam', HEADER.replace('energy: 2', 'energy: 9')),
        ('breakdown', 'wide_space', '-- 16-beat loop.\nreturn {}\n'),
    ]:
        (tmp_path / bank).mkdir(exist_ok=True)
        (tmp_path / bank / f'{name}.lua').write_text(source)
    with pytest.raises(AutoloopMetaError) as error:
        read_autoloop_meta(tmp_path)
    assert 'breakdown/wide_space: missing energy, family, movement' in str(error.value)
    assert "drop/slam: energy '9'" in str(error.value)
    assert 'drop/kick' not in str(error.value)


def test_every_shipped_loop_declares_metadata_and_the_defaults_match_their_files():
    meta = read_autoloop_meta()
    assert set(meta) == required_autoloops()
    for loop, declared in DEFAULT_AUTOLOOP_META.items():
        assert meta[loop] == declared
