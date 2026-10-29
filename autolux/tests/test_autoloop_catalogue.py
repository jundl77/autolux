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
    rotation_autoloops,
)


def test_discovery_puts_the_preferred_look_first_then_the_rest_alphabetically(tmp_path):
    bank = tmp_path / 'drop'
    bank.mkdir()
    for name in ('slam', 'kick', 'chase', 'notes.txt'):
        (bank / (name if '.' in name else f'{name}.lua')).write_text('return {}')
    table = discover_autoloops(tmp_path)
    assert table[LightIntent.DROP] == (('drop', 'kick'), ('drop', 'chase'), ('drop', 'slam'))
    assert table[LightIntent.BUILDUP] == (('buildup', 'ramp'),)


def test_the_shipped_catalogue_covers_every_lua_file_with_the_originals_first():
    table = discover_autoloops()
    for intent, preferred in PREFERRED_AUTOLOOPS.items():
        assert table[intent][0] == (intent.value, preferred)
    on_disk = {(path.parent.name, path.stem) for path in AUTOLOOP_DIRECTORY.glob('*/*.lua')}
    assert required_autoloops() == on_disk


HEADER = '-- 8-beat loop, no strobe.\n-- energy: 2\n-- movement: snap\n-- texture: beam\n'
RETIRED_HEADER = '-- 8-beat loop.\n-- energy: 2\n-- family: warm\n-- movement: snap\n'


def test_the_header_block_after_the_loop_length_line_is_the_metadata():
    source = HEADER + '-- wash: primary kick.\nlocal palette = sf.palette("techno")\n'
    assert parse_autoloop_meta(source) == AutoloopMeta(2, 'snap', 'beam')


@pytest.mark.parametrize(
    ('header', 'message'),
    [
        (HEADER.replace('-- texture: beam\n', ''), 'missing texture'),
        (RETIRED_HEADER, 'missing movement, texture'),
        (HEADER.replace('-- energy: 2\n', '') + '-- wash: x\n-- energy: 2\n', 'missing energy'),
        (HEADER.replace('energy: 2', 'enrgy: 2'), 'missing energy'),
        (HEADER.replace('energy: 2', 'energy: 4'), "energy '4' is not one of 1, 2, 3"),
        (HEADER.replace('energy: 2', 'energy: 03'), "energy '03'"),
        (HEADER.replace('energy: 2', 'energy: 2 '), "energy '2 '"),
        (HEADER.replace('energy: 2', 'energy: high'), "energy 'high' is not one of"),
        (HEADER.replace('texture: beam', 'texture: strobe'), "texture 'strobe' is not one of wash"),
        (HEADER.replace('movement: snap', 'movement: snap  -- jumps'), 'movement'),
        (HEADER + '-- texture: wash\n', 'texture declared twice'),
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
    assert 'breakdown/wide_space: missing energy, movement, texture' in str(error.value)
    assert "drop/slam: energy '9'" in str(error.value)
    assert 'drop/kick' not in str(error.value)


def test_every_shipped_loop_declares_metadata_and_the_defaults_match_their_files():
    meta = read_autoloop_meta()
    assert set(meta) == rotation_autoloops()
    for loop, declared in DEFAULT_AUTOLOOP_META.items():
        assert meta[loop] == declared


def test_the_python_header_keys_match_the_starfire_parity_test():
    import re

    from autolux.lib.engine.effect_definitions import AUTOLOOP_META_VALUES

    rust = (AUTOLOOP_DIRECTORY.parents[1] / 'starfire/tests/autoloop_replay.rs').read_text(
        encoding='utf-8'
    )
    block = re.search(r'const META_KEYS[^=]*= \[(.*?)\];', rust, re.DOTALL).group(1)
    rust_keys = {
        key: tuple(re.findall(r'"([^"]+)"', values))
        for key, values in re.findall(r'\("(\w+)", &\[([^\]]*)\]\)', block)
    }
    assert rust_keys == AUTOLOOP_META_VALUES
