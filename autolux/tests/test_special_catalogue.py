import pytest

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.special_policy import INTENSE_SPEED
from autolux.lighting.special_catalogue import (
    SPECIAL_DIRECTORY,
    VENUE_FILE,
    SpecialError,
    discover_specials,
    parse_special,
)

ROW = {
    'name': 'glow',
    'intents': ['atmospheric'],
    'group': 'fx',
    'preset': {'colour': 'secondary'},
    'effect': {'rate': 0.1, 'size': 0.2},
    'playback': {'attack_beats': 32, 'release_beats': 32},
}
ROW_TOML = '''name = "glow"
intents = ["atmospheric"]
group = "fx"

[preset]
colour = "secondary"

[effect]
rate = 0.1
size = 0.2

[playback]
attack_beats = 32
release_beats = 32
'''


def row(**sections) -> dict:
    """ROW with each named section updated; a None value deletes that key."""
    changed = {key: dict(value) if isinstance(value, dict) else value for key, value in ROW.items()}
    for section, values in sections.items():
        if not isinstance(values, dict):
            changed[section] = values
            continue
        target = changed.setdefault(section, {})
        for key, value in values.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value
    return changed


def test_the_shipped_specials_parse_and_skip_buildups():
    specials = discover_specials()
    files = {path.stem for path in SPECIAL_DIRECTORY.glob('*.toml') if path.name != VENUE_FILE}
    assert files == {s.name for s in specials}
    served = {intent for special in specials for intent in special.intents}
    assert served == {LightIntent.ATMOSPHERIC, LightIntent.BREAKDOWN, LightIntent.DROP}


def test_no_shipped_special_strobes():
    assert 'strobe_burst' not in {special.name for special in discover_specials()}


def test_only_drop_rows_are_intense_in_the_shipped_set():
    for special in discover_specials():
        assert special.intense == (special.intents == (LightIntent.DROP,)), special.name


def test_a_row_parses_into_the_wire_vocabulary():
    special = parse_special(row(effect={'form': {'pulse_beats': 2}, 'pattern': 0.5}), 'glow')
    assert special.wire() == {
        'colour_slot': 'secondary',
        'energy': 0.2,
        'speed': 0.1,
        'pulse_beats': 2,
        'attack_beats': 32.0,
        'release_beats': 32.0,
        'pattern': 0.5,
    }


def test_the_row_cooldown_defaults_to_none_and_stays_off_the_wire():
    assert parse_special(row(), 'glow').cooldown_seconds == 0.0
    special = parse_special(row(playback={'cooldown_seconds': 240}), 'glow')
    assert special.cooldown_seconds == 240.0
    assert 'cooldown_seconds' not in special.wire()


@pytest.mark.parametrize(
    ('speed', 'pulse', 'intense'),
    [
        (0.1, None, False),
        (INTENSE_SPEED, None, False),
        (INTENSE_SPEED + 0.05, None, True),
        (0.1, 4, True),
    ],
)
def test_intense_is_derived_from_pulse_or_speed(speed, pulse, intense):
    form = None if pulse is None else {'pulse_beats': pulse}
    special = parse_special(row(effect={'rate': speed, 'form': form}), 'glow')
    assert special.intense == intense


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        ({'intense': True}, 'unknown intense'),
        ({'effect': {'fixture': 'x'}}, 'unknown effect.fixture'),
        ({'preset': 'cold'}, 'unknown preset'),
        ({'effect': {'size': 1.5}}, 'effect.size 1.5 must be a number within 0..1'),
        ({'effect': {'rate': True}}, 'effect.rate True must be a number'),
        ({'preset': {'colour': 'pink'}}, "preset.colour 'pink' is not one of primary"),
        ({'intents': []}, 'intents must be a non-empty list'),
        ({'intents': ['chorus']}, "intent 'chorus' is not one of"),
        ({'intents': ['drop', 'drop']}, 'intents lists an intent twice'),
        ({'group': 'FX Bar'}, 'lower-case rig group name'),
        ({'name': 'other'}, "name 'other' must match the file name 'glow'"),
        (
            {'effect': {'form': {'pulse_beats': 0}}},
            'effect.form.pulse_beats 0 must be a whole number of beats',
        ),
        ({'effect': {'form': {'pulse_beats': 1.5}}}, 'effect.form.pulse_beats 1.5'),
        ({'effect': {'form': {'beats': 2}}}, 'unknown effect.form.beats'),
        ({'effect': {'pattern': 2}}, 'effect.pattern 2 must be a number within 0..1'),
        ({'playback': {'attack_beats': -1}}, 'playback.attack_beats -1'),
        ({'playback': {'cooldown_seconds': -1}}, 'playback.cooldown_seconds -1 must be a number'),
        ({'scaling': ['energy']}, 'unknown scaling'),
    ],
)
def test_invalid_rows_name_the_field_and_the_rule(change, message):
    with pytest.raises(SpecialError, match=message.replace('(', r'\(').replace('.', r'\.')):
        parse_special(row(**change), 'glow')


def test_a_row_selects_by_role_instead_of_naming_a_group():
    groupless = {key: value for key, value in ROW.items() if key != 'group'}
    special = parse_special(groupless | {'role': 'accent'}, 'glow')
    assert (special.group, special.role) == ('', 'accent')
    assert parse_special(ROW, 'glow').role is None
    zoned = parse_special(
        groupless | {'role': 'accent', 'zone': 'house_left', 'depth': 'upstage'}, 'glow'
    )
    assert (zoned.zone, zoned.depth) == ('house_left', 'upstage')
    for document, message in [
        (ROW | {'role': 'accent'}, 'give exactly one of group, role'),
        (groupless, 'give exactly one of group, role'),
        (groupless | {'role': 'laser'}, "role 'laser' is not one of loop, accent"),
        (ROW | {'zone': 'centre'}, 'zone and depth narrow a role row'),
        (
            groupless | {'role': 'accent', 'zone': 'left'},
            "zone 'left' is not one of house_left, centre, house_right",
        ),
        (groupless | {'role': 'accent', 'depth': 'front'}, "depth 'front' is not one of"),
    ]:
        with pytest.raises(SpecialError, match=message):
            parse_special(document, 'glow')


def test_a_missing_field_is_named_by_its_file_path():
    with pytest.raises(SpecialError, match=r'missing effect\.size, playback\.release_beats'):
        parse_special(row(effect={'size': None}, playback={'release_beats': None}), 'glow')


def test_a_special_never_strobes():
    strobe = {'name': 's', 'intents': ['drop'], 'group': 'all'}
    with pytest.raises(SpecialError, match=r'effect\.form\.strobe_hz'):
        parse_special(strobe | {'effect': {'form': {'strobe_hz': 8}}}, 's')


def test_discovery_reports_every_bad_file_by_name(tmp_path):
    (tmp_path / 'glow.toml').write_text(ROW_TOML)
    (tmp_path / 'broken.toml').write_text('name = ')
    (tmp_path / 'loud.toml').write_text(
        ROW_TOML.replace('"glow"', '"loud"').replace('size = 0.2', 'size = 2')
    )
    (tmp_path / 'pink.toml').write_text(
        ROW_TOML.replace('"glow"', '"pink"').replace('"secondary"', '"magenta"')
    )
    with pytest.raises(SpecialError) as error:
        discover_specials(tmp_path)
    assert 'broken.toml: not TOML' in str(error.value)
    assert 'loud.toml: effect.size 2 must be a number within 0..1' in str(error.value)
    assert "pink.toml: preset.colour 'magenta' is not one of primary" in str(error.value)
    assert 'glow.toml' not in str(error.value)


def test_the_file_parses_to_the_same_special_as_its_table(tmp_path):
    (tmp_path / 'glow.toml').write_text(ROW_TOML)
    assert discover_specials(tmp_path) == (parse_special(ROW, 'glow'),)
