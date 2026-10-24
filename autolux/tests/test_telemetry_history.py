import math

import pytest

from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.telemetry_history import TelemetryHistory
from autolux.simulate.system_metrics import (
    build_metric_series,
)


def report(stamp=100, *, session=1):
    return {
        'sample_timestamp': stamp,
        'updated_at': stamp,
        'session': session,
        'tx_age_seconds': 0.1,
        'rx_age_seconds': 0.3,
        'tx': {'link_up': True, 'dmx_input_age_seconds': 0.01, 'capture_overruns_total': 0},
        'rx': {
            'link_up': True,
            'dmx_connected': True,
            'audio_input_age_seconds': 0.01,
            'audio_buffer_seconds': 0.152,
            'buffer_target_seconds': 0.15,
            'audio_signal_peak': 0.5,
            'audio_max_gap_seconds': 0.02,
            'underruns_total': 2,
            'dmx_frames_total': 44,
        },
    }


def test_history_deduplicates_native_reports_and_rejects_out_of_order_samples():
    buffer = EventBuffer()
    for stamp in [100, 100, 99, 101]:
        buffer.set_system_health(report(stamp))
    history = buffer.snapshot()['system_history']
    assert [s['at'] for s in history] == [100, 101]
    assert history[-1]['buffer_ms'] == 152
    assert history[-1]['target_ms'] == 150
    assert history[-1]['capture_overruns'] == 0
    assert history[-1]['lost_frames'] is None
    history[-1]['buffer_ms'] = -1
    assert buffer.snapshot()['system_history'][-1]['buffer_ms'] == 152


def test_history_is_bounded_by_both_time_and_number_of_samples():
    history = TelemetryHistory()
    for i in range(4000):
        history.append(report(i / 10), {})
    assert len(history.samples) == 301
    history.append(report(1000), {})
    assert len(history.samples) == 1


def test_offline_report_omits_unmeasured_hardware_telemetry():
    report = EventBuffer().to_report()
    assert set(report) == {'duration_sec', 'beats', 'effects', 'intents', 'timing_log', 'metrics'}


def test_session_report_retains_health_history_without_exposing_mutable_state():
    buffer = EventBuffer()
    buffer.start()
    buffer.set_system_health(report(100))
    buffer.set_system_health(report(101))
    saved = buffer.to_report()
    assert saved['system']['sample_timestamp'] == 101
    assert [sample['at'] for sample in saved['system_history']] == [100, 101]
    saved['system']['rx']['audio_buffer_seconds'] = -1
    saved['system_history'][0]['buffer_ms'] = -1
    retained = buffer.to_report()
    assert retained['system']['rx']['audio_buffer_seconds'] == 0.152
    assert retained['system_history'][0]['buffer_ms'] == 152


def test_new_session_clears_history_including_when_timestamp_moves_backwards():
    history = TelemetryHistory()
    history.append(report(100), {})
    history.append(report(90, session=2), {})
    assert [s['at'] for s in history.samples] == [90]


def test_stale_and_missing_receiver_measurements_are_not_healthy_zeros():
    state = report()
    state['rx_age_seconds'] = 3
    history = TelemetryHistory()
    history.append(state, {'mean_error_ms': 4})
    sample = history.snapshot()[0]
    assert sample['buffer_ms'] is None
    assert sample['starvations'] is None
    assert sample['capture_overruns'] == 0
    assert sample['command_error_ms'] == 4


@pytest.mark.parametrize('bad', [None, math.nan, math.inf, '150', True])
def test_invalid_telemetry_does_not_reach_plotly(bad):
    state = report()
    state['rx']['audio_buffer_seconds'] = bad
    history = TelemetryHistory()
    history.append(state, {})
    assert history.snapshot()[0]['buffer_ms'] is None


def test_rates_handle_real_intervals_resets_and_outages_without_spikes():
    history = [
        {'at': t, 'count': n}
        for t, n in [
            (100, 20),
            (102, 24),
            (103, 0),
            (104, 2),
            (110, 8),
            (111, None),
            (112, 9),
            (113, 9),
        ]
    ]
    _, y = build_metric_series(history, 'count', rate=True)
    assert y == [None, 2, None, 2, None, None, None, None, 0]


def test_line_breaks_at_missing_reports_and_distinguishes_silence_from_no_signal_data():
    x, y = build_metric_series(
        [{'at': 100, 'signal_peak': 0.5}, {'at': 105, 'signal_peak': 0}, {'at': 106}], 'signal_peak'
    )
    assert len(x) == 4
    assert y[0] == pytest.approx(-6.0206)
    assert y[1:] == [None, -90, None]
