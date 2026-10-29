import json
import shutil

import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lighting.autoloop_catalogue import AUTOLOOP_DIRECTORY
from autolux.lighting.starfire_replay import report_to_replay


def report():
    return {
        'duration_sec': 20.0,
        'beats': [{'t': 1.0, 'bpm': 120.0}, {'t': 1.5, 'bpm': 120.0}],
        'intents': [{'t': 15.0, 'song_t': 1.0, 'intent': 'drop'}],
        'metrics': {'look_ahead_sec': 14.0},
    }


def sections(replay):
    """When each section starts: a cue at its start beat, a selection when it is sent."""
    beats = {
        event['command']['beat_index']: event['at_seconds']
        for event in replay['events']
        if event['command']['kind'] == 'beat'
    }
    starts = []
    for event in replay['events']:
        command = event['command']
        if command['kind'] == 'cue_section':
            starts.append(beats[command['value']['start_beat_index']])
        elif command['kind'] == 'select':
            starts.append(event['at_seconds'])
    return starts


def cues(replay):
    return [event for event in replay['events'] if event['command']['kind'] == 'cue_section']


async def test_actual_event_buffer_queue_delivery_is_aligned_to_source_time_once():
    from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue

    clock = VirtualClock()
    buffer = EventBuffer(clock=clock, look_ahead_sec=14.0)
    queue = DelayedCommandQueue(14.0, clock=clock)
    buffer.start()
    clock.advance(1.0)
    buffer.add_beat(120.0, False)

    async def apply_intent():
        buffer.set_intent('drop', song_sec=1.0)

    await queue.enqueue('intent', apply_intent)
    clock.advance(14.0)
    await queue.drain()
    clock.advance(1.0)
    saved = json.loads(json.dumps(buffer.to_report()))
    assert saved['beats'][0]['t'] == 1.0
    assert saved['intents'][0]['t'] == 15.0
    replay = report_to_replay(saved)
    # The cue goes out the look-ahead before its start beat, here clamped to the start.
    assert [(event['at_seconds'], event['command']['kind']) for event in replay['events']] == [
        (0.0, 'cue_section'),
        (1.0, 'beat'),
        (1.0, 'set_normalized_rms_level'),
        (16.0, 'stop'),
    ]
    assert sections(replay) == [1.0]


def test_source_boundary_wins_over_late_delivery_and_legacy_subtracts_delay_once():
    source = report()
    source['intents'] = [
        {'t': 15.0, 'intent': 'atmospheric'},
        {'t': 19.0, 'song_t': 2.0, 'intent': 'drop'},
    ]
    # 2.0 s has no beat within half a beat, so it switches by selection at its time.
    assert sections(report_to_replay(source)) == [1.0, 2.0]


def test_simulation_end_clamp_does_not_shift_a_recorded_source_boundary():
    clock = VirtualClock()
    buffer = EventBuffer(clock=clock, look_ahead_sec=14.0)
    buffer.start()
    clock.advance(4.0)
    buffer.add_beat(120.0, False)
    clock.advance(1.0)
    buffer.mark_end()
    clock.advance(14.0)
    buffer.set_intent('buildup', song_sec=4.0)
    saved = buffer.to_report()
    assert saved['intents'][0]['t'] == 5.0
    assert sections(report_to_replay(saved)) == [4.0]


def test_silence_stops_immediately_and_same_autoloop_can_resume_after_transport_stop():
    source = report()
    source['intents'] = [
        {'t': 15.0, 'song_t': 1.0, 'intent': 'drop'},
        {'t': 16.0, 'song_t': 2.0, 'intent': 'drop'},
        {'t': 17.0, 'song_t': 17.0, 'intent': 'atmospheric', 'trigger': 'silence'},
        {'t': 19.0, 'song_t': 18.0, 'intent': 'drop'},
    ]
    result = report_to_replay(source)
    assert sections(result) == [1.0, 18.0]
    silence = [event for event in result['events'] if event['at_seconds'] == 17.0]
    assert [event['command'] for event in silence] == [{'kind': 'clear_cues'}, {'kind': 'stop'}]
    assert result['events'][-1] == {'at_seconds': 20.0, 'command': {'kind': 'stop'}}


def overlay_catalogue(directory):
    """The shipped base banks with a generic overlay of every kind the director names."""
    shutil.copytree(AUTOLOOP_DIRECTORY, directory, ignore=shutil.ignore_patterns('*.md'))
    header = '-- overlay\n-- strobe: no\n-- intensity: htp\n-- movement: still\n-- texture: mixed\n'
    for bank, name in [
        ('dip', 'last_beat'),
        ('dip', 'fade'),
        ('hit', 'colour_bump'),
        ('hit', 'flash'),
        ('riser', 'swell'),
        ('fill', 'turnaround'),
        ('fill', 'roll'),
        ('accent', 'sparkle'),
        ('accent', 'shimmer'),
    ]:
        (directory / bank).mkdir(exist_ok=True)
        (directory / bank / f'{name}.lua').write_text(header + 'return {}\n', encoding='utf-8')
    return directory


def a_set():
    """Four sections at 120 bpm: an intro, a 16-bar build, a drop and a breakdown."""
    return {
        'duration_sec': 120.0,
        'beats': [
            {'t': 0.5 + index / 2, 'bpm': 120.0, 'rms': 0.1 + (index % 4) * 0.05}
            for index in range(230)
        ],
        'intents': [
            {'t': 10.0, 'song_t': 0.5, 'intent': 'atmospheric'},
            {'t': 40.0, 'song_t': 30.5, 'intent': 'buildup'},
            {'t': 72.0, 'song_t': 62.5, 'intent': 'drop'},
            {'t': 104.0, 'song_t': 94.5, 'intent': 'breakdown'},
        ],
        'metrics': {'look_ahead_sec': 9.0},
    }


def test_the_director_cues_every_section_with_its_overlays_deterministically(tmp_path):
    catalogue = overlay_catalogue(tmp_path / 'autoloops')
    first = report_to_replay(a_set(), seed=7, autoloops=catalogue)
    assert json.dumps(first) == json.dumps(report_to_replay(a_set(), seed=7, autoloops=catalogue))
    planned = [event['command']['value'] for event in cues(first)]
    assert [cue['intent'] for cue in planned] == ['atmospheric', 'buildup', 'drop', 'breakdown']
    # Each cue goes out the look-ahead before its start beat.
    assert [event['at_seconds'] for event in cues(first)] == [0.0, 21.5, 53.5, 85.5]
    assert sections(first) == [0.5, 30.5, 62.5, 94.5]
    drop_rows = {(row['bank'], row['autoloop']) for row in planned[2]['overlays']}
    assert ('dip', 'last_beat') in drop_rows
    assert {bank for bank, _ in drop_rows} >= {'dip', 'hit'}
    # The seed is the show's: another one plans the same sections with other draws.
    assert [cue['seed'] for cue in planned] != [
        event['command']['value']['seed']
        for event in cues(report_to_replay(a_set(), seed=8, autoloops=catalogue))
    ]


def test_replay_preserves_measured_loudness_instead_of_a_fixed_strength():
    from autolux.lib.clients.starfire_client import rms_to_normalized_level

    source = report()
    source['beats'][0]['rms'] = 0.01
    source['beats'][1]['rms'] = 0.1
    energies = [
        event
        for event in report_to_replay(source)['events']
        if event['command']['kind'] == 'set_normalized_rms_level'
    ]
    assert [event['at_seconds'] for event in energies] == [1.0, 1.5]
    assert [event['command']['level'] for event in energies] == [
        rms_to_normalized_level(0.01),
        rms_to_normalized_level(0.1),
    ]


def test_zero_tempo_warmup_is_skipped_without_losing_detected_beat_count():
    source = report()
    source['beats'] = [{'t': 0.5, 'bpm': 0.0}, {'t': 1.0, 'bpm': 120.0}, {'t': 1.5, 'bpm': 0.0}]
    beats = [
        event for event in report_to_replay(source)['events'] if event['command']['kind'] == 'beat'
    ]
    assert [event['command']['beat_index'] for event in beats] == [1, 2]
    assert {event['command']['bar_phase'] for event in beats} == {None}
    assert [event['command']['bpm'] for event in beats] == [120.0, 120.0]


@pytest.mark.parametrize('bpm', [-1.0, float('nan'), float('inf'), 1001.0, True, '120'])
def test_invalid_tempo_is_rejected(bpm):
    source = report()
    source['beats'][0]['bpm'] = bpm
    with pytest.raises((ValueError, TypeError), match='bpm'):
        report_to_replay(source)


@pytest.mark.parametrize('field,value', [('t', -1.0), ('song_t', 21.0), ('intent', 'unknown')])
def test_invalid_intent_cannot_be_silently_converted(field, value):
    source = report()
    source['intents'][0][field] = value
    with pytest.raises(ValueError):
        report_to_replay(source)


def test_missing_tempo_or_delay_and_backwards_beats_are_rejected():
    source = report()
    source['beats'] = [{'t': 1.0, 'bpm': 0.0}]
    with pytest.raises(ValueError, match='positive tempo'):
        report_to_replay(source)
    source = report()
    del source['metrics']['look_ahead_sec']
    with pytest.raises(TypeError, match='look_ahead_sec'):
        report_to_replay(source)
    source = report()
    source['beats'].reverse()
    with pytest.raises(ValueError, match='ordered'):
        report_to_replay(source)


def test_a_replay_embeds_the_login_palette_table_and_names_only_its_palettes():
    from autolux.lighting.palettes import palette_table

    replay = report_to_replay(a_set())
    assert [palette['name'] for palette in replay['palettes']] == [
        palette.name for palette in palette_table()
    ]
    assert replay['palettes'][0]['secondary'] == {'red': 1.0, 'green': 140 / 255, 'blue': 26 / 255}
    names = {palette['name'] for palette in replay['palettes']}
    assert {event['command']['value']['palette'] for event in cues(replay)} <= names
