import math

import pytest

from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.telemetry_history import TelemetryHistory
from autolux.simulate.system_metrics import (
    build_metric_series,
)


def report(stamp=100, *, run_id=1):
    return {
        'sample_timestamp': stamp,
        'updated_at': stamp,
        'run_id': run_id,
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


def renderer_report(stamp, elapsed, frames, *, skipped=0, age=0.1, run_id=1):
    state = report(stamp, run_id=run_id)
    state['starfire'] = {
        'elapsed_seconds': elapsed,
        'rendered_frames': frames,
        'skipped_frames': skipped,
        'queued_commands': 2,
        'last_render_ms': 0.35,
        'age_seconds': age,
        'script_error': '',
    }
    return state


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


def test_new_run_clears_history_including_when_timestamp_moves_backwards():
    history = TelemetryHistory()
    history.append(report(100), {})
    history.append(report(90, run_id=2), {})
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


def test_renderer_rates_use_elapsed_counter_deltas_and_hold_identical_reports():
    history = TelemetryHistory()
    history.append(renderer_report(100, 0, 1), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] is None
    history.append(renderer_report(109, 0, 1, age=9), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] is None
    # Wall-time observations are uneven; the renderer measured exactly ten seconds.
    history.append(renderer_report(111, 10, 399, skipped=2), {})
    measured = history.snapshot()[-1]
    assert measured['starfire_measured_render_frames_per_second'] == pytest.approx(39.8)
    assert measured['starfire_missed_render_deadlines_per_second'] == pytest.approx(0.2)
    assert measured['starfire_skipped_frames'] == 2
    assert measured['starfire_queued_commands'] == 2
    assert measured['starfire_last_render_duration_ms'] == 0.35
    history.append(renderer_report(112, 10, 399, skipped=2, age=1.1), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] == pytest.approx(
        39.8
    )
    history.append(renderer_report(120, 20, 799, skipped=2), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] == 40
    assert history.snapshot()[-1]['starfire_missed_render_deadlines_per_second'] == 0


@pytest.mark.parametrize('missing', [True, False])
def test_renderer_outage_creates_gaps_and_requires_a_new_rate_interval(missing):
    history = TelemetryHistory()
    history.append(renderer_report(100, 10, 400), {})
    history.append(renderer_report(110, 20, 800), {})
    unavailable = report(111) if missing else renderer_report(111, 20, 800, age=30)
    history.append(unavailable, {})
    latest = history.snapshot()[-1]
    assert all(value is None for key, value in latest.items() if key.startswith('starfire_'))
    history.append(renderer_report(112, 30, 1200), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] is None
    history.append(renderer_report(122, 40, 1600), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] == 40


@pytest.mark.parametrize('elapsed,frames,skipped', [(1, 41, 0), (21, 1, 2), (21, 840, 0)])
def test_renderer_time_or_counter_resets_never_become_rate_spikes(elapsed, frames, skipped):
    history = TelemetryHistory()
    history.append(renderer_report(100, 10, 400, skipped=1), {})
    history.append(renderer_report(110, 20, 800, skipped=2), {})
    history.append(renderer_report(111, elapsed, frames, skipped=skipped), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] is None
    assert history.snapshot()[-1]['starfire_missed_render_deadlines_per_second'] is None
    history.append(renderer_report(121, elapsed + 10, frames + 400, skipped=skipped), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] == 40


def test_renderer_does_not_average_across_missing_observations_or_new_runs():
    history = TelemetryHistory()
    history.append(renderer_report(100, 10, 400), {})
    history.append(renderer_report(110, 20, 800), {})
    history.append(renderer_report(150, 30, 1200), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] is None
    history.append(renderer_report(160, 40, 1600), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] == 40
    history.append(renderer_report(161, 50, 2000, run_id=2), {})
    assert history.snapshot()[-1]['starfire_measured_render_frames_per_second'] is None


@pytest.mark.parametrize('bad', [None, math.nan, math.inf, -1, True])
def test_invalid_renderer_measurements_cannot_create_rates(bad):
    history = TelemetryHistory()
    history.append(renderer_report(100, 10, 400), {})
    state = renderer_report(110, 20, bad)
    state['starfire']['last_render_ms'] = bad
    state['starfire']['queued_commands'] = bad
    history.append(state, {})
    latest = history.snapshot()[-1]
    assert latest['starfire_measured_render_frames_per_second'] is None
    assert latest['starfire_last_render_duration_ms'] is None
    assert latest['starfire_queued_commands'] is None
