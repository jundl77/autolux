"""Source-analysis invariants and semantic lighting checks for fast simulation."""

import copy
import json
from pathlib import Path

import pytest

from autolux.tests.conftest import ANCHOR_YOUTUBE_ID
from autolux.training.pipeline_digest import (
    ANALYSIS_DIGEST_KEYS,
    TIMING_ACCURACY_MAX_MS,
    check_digest,
    degradation_digest,
    digest_report,
    held_start_to_end,
    informational_digest,
    relations_digest,
    survives_digest,
)

BASELINE = Path(__file__).parent / 'fixtures' / 'pipeline_digest_baseline.json'
SAMPLE_SONG_NAME = f'{ANCHOR_YOUTUBE_ID}.mp3'


def _beat(t=1.0, **over):
    return {'t': t, 'bpm': 128.0, 'change': False, 'rms': 0.1, **over}


def _report(beats=(), **over):
    return {
        'duration_sec': 10.0,
        'beats': list(beats),
        'effects': [],
        'intents': [],
        'timing_log': [],
        'metrics': {
            'beats_detected': len(beats),
            'bpm_last': 128.0,
            'intent_changes_count': 0,
            'effect_changes_count': 0,
            'unique_intents_count': 0,
            'dominant_intent': None,
            'intent_distribution_sec': {},
        },
        **over,
    }


def _event(t=1.0, **command):
    return {'at_seconds': t, 'command': command}


def _selection(t=1.0, bank='drop', autoloop='blue_impact'):
    return _event(t, kind='select', bank=bank, autoloop=autoloop)


def _accent(t=1.0):
    return _event(t, kind='set_override', value={'id': 'drop_arrival'})


def _streams(*events, sound=()):
    return {'sound': list(sound), 'lighting': list(events)}


def _drop_report():
    return _report(
        [_beat()],
        intents=[{'t': 1.0, 'end': 10.0, 'intent': 'drop'}],
        effects=[
            {'t': 1.0, 'name': 'drop/blue_impact', 'type': 'AUTOLOOP'},
            {'t': 1.0, 'name': 'drop_arrival', 'type': 'ACCENT'},
        ],
    )


@pytest.mark.parametrize(
    'field,value',
    [
        ('t', 2.0),
        ('bpm', 140.0),
        ('change', True),
        ('rms', 0.2),
    ],
)
def test_source_beat_changes_are_detected(field, value):
    original = survives_digest(_report([_beat()]))
    changed = survives_digest(_report([_beat(**{field: value})]))
    assert original['beat_columns_hash'] != changed['beat_columns_hash']


def test_analysis_digest_excludes_renderer_output():
    original = survives_digest(_report([_beat()]))
    changed = survives_digest(_drop_report(), _streams(_selection(), _accent()))
    assert original == changed


def test_sound_boundaries_remain_gated():
    original = survives_digest(_report(), _streams(sound=[{'t': 1.0, 'playing': False}]))
    changed = survives_digest(_report(), _streams(sound=[{'t': 2.0, 'playing': False}]))
    assert original != changed


def test_autoloop_and_accent_commands_match_report_in_order():
    relations = relations_digest(_drop_report(), _streams(_selection(), _accent()))
    assert all(relations.values())


def test_an_unreported_command_is_rejected():
    assert not relations_digest(_report(), _streams(_selection()))['lighting_matches_report']


def test_a_missing_command_is_rejected():
    assert not relations_digest(_drop_report(), _streams(_selection()))['lighting_matches_report']


def test_reversed_commands_are_rejected():
    assert not relations_digest(_drop_report(), _streams(_accent(), _selection()))[
        'lighting_matches_report'
    ]


def test_command_time_must_not_go_backwards():
    assert not relations_digest(_report(), _streams(_selection(2.0), _selection(1.0)))[
        'lighting_arrives_in_order'
    ]


@pytest.mark.parametrize('name', ['buildup/current', 'unknown/program'])
def test_autoloop_must_belong_to_committed_intent(name):
    report = _drop_report()
    report['effects'][0]['name'] = name
    assert not relations_digest(report)['autoloops_match_intents']


def test_non_effect_commands_do_not_add_reported_effects():
    events = _streams(
        _event(kind='beat', position_beats=1, bpm=128),
        _event(kind='set_normalized_rms_level', level=0.6),
        _event(kind='stop'),
    )
    assert relations_digest(_report(), events)['lighting_matches_report']


@pytest.mark.parametrize(
    'error_ms,passes',
    [
        (0.0, True),
        (TIMING_ACCURACY_MAX_MS - 0.1, True),
        (TIMING_ACCURACY_MAX_MS + 0.1, False),
    ],
)
def test_queue_delay_has_a_real_tolerance(error_ms, passes):
    report = _report(
        timing_log=[
            {
                'target_delta_sec': 14.0,
                'actual_delta_sec': 14.0 + error_ms / 1000.0,
            }
        ]
    )
    assert relations_digest(report)['queue_error_within_tolerance'] is passes


def test_energy_changes_are_visible_in_command_digest():
    low = informational_digest(
        _report(), _streams(_event(kind='set_normalized_rms_level', level=0.2))
    )
    high = informational_digest(
        _report(), _streams(_event(kind='set_normalized_rms_level', level=0.8))
    )
    assert low['lighting']['ordering_hash'] != high['lighting']['ordering_hash']


@pytest.mark.parametrize('field', ANALYSIS_DIGEST_KEYS)
def test_each_analysis_invariant_is_checked(field):
    digest = digest_report(_report([_beat()]))
    baseline = {'track': {'survives': copy.deepcopy(digest['survives'])}}
    digest['survives'][field] = 'changed'
    assert any(field in failure for failure in check_digest('track', digest, baseline))


def test_missing_analysis_evidence_is_not_a_pass():
    digest = digest_report(_report([_beat()]))
    baseline = {'track': {'survives': {}}}
    assert check_digest('track', digest, baseline)


def test_unknown_baseline_track_is_reported():
    assert check_digest('missing', {}, {})


def test_failed_semantic_relation_fails_even_with_identical_analysis():
    digest = digest_report(_report())
    baseline = {'track': {'survives': copy.deepcopy(digest['survives'])}}
    digest['relations']['lighting_arrives_in_order'] = False
    assert any(
        'lighting_arrives_in_order' in error for error in check_digest('track', digest, baseline)
    )


def test_degradation_distinguishes_held_from_changing_sections():
    held = _report(intents=[{'t': 0.0, 'intent': 'breakdown'}])
    held['metrics']['effect_changes_count'] = 1
    assert held_start_to_end(degradation_digest(held))
    held['intents'].append({'t': 5.0, 'intent': 'drop'})
    assert not held_start_to_end(degradation_digest(held))


def test_empty_show_is_not_mistaken_for_a_held_section():
    digest = digest_report(_report())
    assert digest['survives']['beats_detected'] == 0
    assert not held_start_to_end(digest['degradation'])


def test_wall_speed_does_not_change_analysis_digest():
    report = _report([_beat()])
    assert (
        digest_report(report, wall_elapsed=1)['survives']
        == digest_report(report, wall_elapsed=10)['survives']
    )


_digest_cache = {}


async def _anchor_digest(anchor_mp3):
    if not _digest_cache:
        from autolux.training.pipeline_digest import digest_track

        _digest_cache.update(await digest_track(anchor_mp3))
    return _digest_cache


@pytest.mark.integration
async def test_anchor_source_analysis_matches_committed_baseline(anchor_mp3):
    actual = await _anchor_digest(anchor_mp3)
    assert check_digest(SAMPLE_SONG_NAME, actual, json.loads(BASELINE.read_text())) == []


@pytest.mark.integration
async def test_anchor_drives_real_semantic_commands(anchor_mp3, nn_artifacts):
    actual = await _anchor_digest(anchor_mp3)
    assert all(actual['relations'].values())
    assert {'beat', 'select', 'set_normalized_rms_level'} <= set(
        actual['informational']['lighting']['kinds']
    )
    assert not held_start_to_end(actual['degradation'])


def _non_anchor_fixture_tracks():
    import run_eval_set

    data_dir = run_eval_set.corpus_dir()
    return [
        (name, str(run_eval_set.audio_path(data_dir, Path(name).stem)))
        for name in sorted(json.loads(BASELINE.read_text()))
        if Path(name).stem != ANCHOR_YOUTUBE_ID
    ]


@pytest.mark.integration
@pytest.mark.parametrize('name,mp3', _non_anchor_fixture_tracks())
async def test_every_fixture_retains_source_analysis_and_valid_lighting(name, mp3):
    from autolux.training.pipeline_digest import digest_track

    assert Path(mp3).exists(), f'Committed fixture audio missing: {mp3}'
    actual = await digest_track(mp3)
    assert check_digest(name, actual, json.loads(BASELINE.read_text())) == []
