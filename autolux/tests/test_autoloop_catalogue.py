from autolux.lib.engine.effect_definitions import PREFERRED_AUTOLOOPS, LightIntent
from autolux.lighting.autoloop_catalogue import (
    AUTOLOOP_DIRECTORY,
    discover_autoloops,
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
