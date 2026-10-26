import copy
import json

import pytest

from autolux.lib.clock import VirtualClock
from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lighting.starfire_replay import report_to_replay


def report():
    return {
        'duration_sec': 20.0,
        'beats': [{'t': 1.0, 'bpm': 120.0}, {'t': 1.5, 'bpm': 120.0}],
        'intents': [{'t': 15.0, 'song_t': 1.0, 'intent': 'drop'}],
        'metrics': {'look_ahead_sec': 14.0},
    }


def selections(replay):
    return [event for event in replay['events'] if event['command']['kind'] == 'select']


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
    assert [(event['at_seconds'], event['command']['kind']) for event in replay['events']] == [
        (1.0, 'beat'),
        (1.0, 'set_normalized_rms_level'),
        (1.0, 'select'),
        (16.0, 'stop'),
    ]


def test_source_boundary_wins_over_late_delivery_and_legacy_subtracts_delay_once():
    source = report()
    source['intents'] = [
        {'t': 15.0, 'intent': 'atmospheric'},
        {'t': 19.0, 'song_t': 2.0, 'intent': 'drop'},
    ]
    result = selections(report_to_replay(source))
    assert [event['at_seconds'] for event in result] == [1.0, 2.0]


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
    assert selections(report_to_replay(saved))[0]['at_seconds'] == 4.0


def test_silence_stops_immediately_and_same_autoloop_can_resume_after_transport_stop():
    source = report()
    source['intents'] = [
        {'t': 15.0, 'song_t': 1.0, 'intent': 'drop'},
        {'t': 16.0, 'song_t': 2.0, 'intent': 'drop'},
        {'t': 17.0, 'song_t': 17.0, 'intent': 'atmospheric', 'trigger': 'silence'},
        {'t': 19.0, 'song_t': 18.0, 'intent': 'drop'},
    ]
    result = report_to_replay(source)
    assert [event['at_seconds'] for event in selections(result)] == [1.0, 18.0]
    assert {'at_seconds': 17.0, 'command': {'kind': 'stop'}} in result['events']
    assert result['events'][-1] == {'at_seconds': 20.0, 'command': {'kind': 'stop'}}


def test_all_four_intents_map_to_authored_autoloops_in_stable_order():
    source = report()
    source['intents'] = [
        {'t': 15.0, 'song_t': 1.0, 'intent': name}
        for name in ['atmospheric', 'breakdown', 'buildup', 'drop']
    ]
    result = report_to_replay(source)
    assert result['events'][0]['command']['kind'] == 'beat'
    assert [
        (event['command']['bank'], event['command']['autoloop']) for event in selections(result)
    ] == [
        ('atmospheric', 'cold_breathe'),
        ('breakdown', 'wide_space'),
        ('buildup', 'ramp'),
        ('drop', 'kick'),
    ]
    assert report_to_replay(source) == report_to_replay(copy.deepcopy(source))


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


def test_a_replay_embeds_the_login_palette_table_and_names_each_selection_palette():
    from autolux.lighting.palettes import DEFAULT_PALETTE, palette_table

    replay = report_to_replay(report())
    assert [palette['name'] for palette in replay['palettes']] == [
        palette.name for palette in palette_table()
    ]
    assert replay['palettes'][0]['secondary'] == {'red': 1.0, 'green': 140 / 255, 'blue': 26 / 255}
    assert {event['command']['palette'] for event in selections(replay)} == {DEFAULT_PALETTE}
