import json

import pytest

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.special_policy import DROP_STROBE, INTENSE_SPEED
from autolux.lighting.special_catalogue import (
    SPECIAL_DIRECTORY,
    SpecialError,
    discover_specials,
    parse_special,
)

ROW = {
    'name': 'glow',
    'intents': ['atmospheric'],
    'group': 'fx',
    'colour_slot': 'secondary',
    'energy': 0.2,
    'speed': 0.1,
    'attack_beats': 32,
    'release_beats': 32,
}


def test_the_shipped_specials_parse_and_serve_every_intent_but_breakdown():
    specials = discover_specials()
    assert {path.stem for path in SPECIAL_DIRECTORY.glob('*.json')} == {s.name for s in specials}
    served = {intent for special in specials for intent in special.intents}
    assert served == {LightIntent.ATMOSPHERIC, LightIntent.BUILDUP, LightIntent.DROP}


def test_the_drop_strobe_row_is_the_built_in_strobe_burst():
    [strobe] = [special for special in discover_specials() if special.strobe_hz is not None]
    assert strobe == DROP_STROBE


def test_only_drop_rows_are_intense_in_the_shipped_set():
    for special in discover_specials():
        assert special.intense == (special.intents == (LightIntent.DROP,)), special.name


def test_a_row_parses_into_the_wire_vocabulary():
    special = parse_special(ROW | {'pulse_beats': 2, 'pattern': 0.5, 'scaling': ['energy']}, 'glow')
    assert special.wire() == {
        'colour_slot': 'secondary',
        'energy': 0.2,
        'speed': 0.1,
        'pulse_beats': 2,
        'attack_beats': 32.0,
        'release_beats': 32.0,
        'pattern': 0.5,
    }
    assert special.scaled(0.5).energy == pytest.approx(0.1)
    assert special.scaled(0.5).speed == 0.1


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
    assert parse_special(ROW | {'speed': speed, 'pulse_beats': pulse}, 'glow').intense == intense


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        ({'intense': True}, 'unknown intense'),
        ({'fixture': 'x'}, 'unknown fixture'),
        ({'energy': None}, "energy None must be a number within 0..1"),
        ({'energy': 1.5}, 'energy 1.5 must be a number within 0..1'),
        ({'speed': True}, 'speed True must be a number'),
        ({'colour_slot': 'pink'}, "colour_slot 'pink' is not one of primary"),
        ({'intents': []}, 'intents must be a non-empty list'),
        ({'intents': ['chorus']}, "intent 'chorus' is not one of"),
        ({'intents': ['drop', 'drop']}, 'intents lists an intent twice'),
        ({'group': 'FX Bar'}, 'lower-case rig group name'),
        ({'name': 'other'}, "name 'other' must match the file name 'glow'"),
        ({'pulse_beats': 0}, 'pulse_beats 0 must be a whole number of beats'),
        ({'pulse_beats': 1.5}, 'pulse_beats 1.5'),
        ({'pattern': 2}, 'pattern 2 must be a number within 0..1'),
        ({'attack_beats': -1}, 'attack_beats -1'),
        ({'scaling': ['strobe_hz']}, "scaling 'strobe_hz' is not one of energy, speed"),
        ({'scaling': 'energy'}, 'scaling must be a list'),
    ],
)
def test_invalid_rows_name_the_field_and_the_rule(change, message):
    with pytest.raises(SpecialError, match=message.replace('(', r'\(').replace('.', r'\.')):
        parse_special(ROW | change, 'glow')


def test_a_missing_field_is_named():
    row = dict(ROW)
    del row['release_beats']
    with pytest.raises(SpecialError, match='missing release_beats'):
        parse_special(row, 'glow')


def test_a_strobe_row_carries_only_its_rate():
    with pytest.raises(SpecialError, match='unknown colour_slot'):
        parse_special(
            {'name': 's', 'intents': ['drop'], 'group': 'all', 'strobe_hz': 8, 'colour_slot': 'x'},
            's',
        )
    with pytest.raises(SpecialError, match='strobe_hz 20 must be a number within 0..10'):
        parse_special({'name': 's', 'intents': ['drop'], 'group': 'all', 'strobe_hz': 20}, 's')


def test_discovery_reports_every_bad_file_by_name(tmp_path):
    (tmp_path / 'glow.json').write_text(json.dumps(ROW))
    (tmp_path / 'broken.json').write_text('{')
    (tmp_path / 'loud.json').write_text(json.dumps(ROW | {'name': 'loud', 'energy': 2}))
    with pytest.raises(SpecialError) as error:
        discover_specials(tmp_path)
    assert 'broken.json: not JSON' in str(error.value)
    assert 'loud.json: energy 2 must be a number within 0..1' in str(error.value)
    assert 'glow.json' not in str(error.value)
