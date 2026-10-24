import json
import time

from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.system_health import health_summary, metrics
from autolux.lib.ui_bridge import SnapshotServer


def healthy():
    return {
        'updated_at': time.time(),
        'tx_age_seconds': 0.1,
        'rx_age_seconds': 0.3,
        'tx': {'up': True, 'link_up': True, 'dmx_input_age_seconds': 0.01},
        'rx': {
            'up': True,
            'link_up': True,
            'audio_buffer_seconds': 0.08,
            'buffer_target_seconds': 0.08,
            'audio_input_age_seconds': 0.01,
            'dmx_connected': True,
            'dmx_lateness_seconds': 0.006,
            'underruns_total': 2,
        },
        'recent': {},
    }


def test_old_counters_do_not_make_a_recovered_show_permanently_unhealthy():
    snapshot = {'system': healthy()}
    assert health_summary(snapshot)['state'] == 'healthy'
    snapshot['system']['recent'] = {'underruns': 1}
    assert health_summary(snapshot)['state'] == 'degraded'


def test_missing_buffer_measurements_do_not_invent_a_zero_or_default_target():
    state = healthy()
    del state['rx']['audio_buffer_seconds']
    del state['rx']['buffer_target_seconds']
    buffer = health_summary({'system': state})['cards'][3]
    assert buffer[1] == 'Unavailable'
    assert buffer[2] == 'Target unavailable'
    assert not buffer[3]


def test_stale_receiver_never_looks_connected_or_exports_a_zero_buffer():
    buffer = EventBuffer()
    state = healthy()
    state['rx_age_seconds'] = 5
    buffer.set_system_health(state)
    assert health_summary(buffer.snapshot())['state'] == 'down'
    text = metrics(buffer).decode()
    assert 'radius_up{node="rx"} 0.0' in text
    assert 'radius_audio_buffer_seconds{node="rx"}' not in text
    assert 'radius_up{node="tx"} 1.0' in text


def test_a_paused_show_with_fresh_transport_is_healthy():
    assert health_summary({'system': healthy(), 'is_playing': False})['state'] == 'healthy'


def test_missing_or_stale_controller_telemetry_is_down():
    assert health_summary({})['state'] == 'down'
    state = healthy()
    state['updated_at'] -= 10
    assert health_summary({'system': state})['state'] == 'down'


def test_a_lost_heartbeat_link_is_down():
    state = healthy()
    state['rx']['link_up'] = False
    assert health_summary({'system': state})['state'] == 'down'


def test_disconnected_lighting_degrades_a_working_audio_link():
    state = healthy()
    state['rx']['dmx_connected'] = False
    assert health_summary({'system': state})['state'] == 'degraded'


def test_shedding_without_a_fault_message_and_recent_skips_are_degraded():
    for shed in [{'level': 'NN_SHED'}, {'level': 'NONE', 'sheds_per_min': 2}]:
        assert health_summary({'system': healthy(), 'shed': shed})['state'] == 'degraded'


def test_historical_analysis_skips_do_not_keep_a_recovered_show_degraded():
    shed = {'level': 'NONE', 'sheds': 9, 'sheds_per_min': 0}
    assert health_summary({'system': healthy(), 'shed': shed})['state'] == 'healthy'


def test_queue_drops_and_latency_recovery_explain_the_audio_warning():
    state = healthy()
    state['recent'] = {'dropped_frames': 128, 'latency_recoveries': 3}
    audio = health_summary({'system': state})['cards'][2]
    assert audio[1] == 'Glitches'
    assert '128 dropped frames' in audio[2]
    assert '3 latency recovery drops' in audio[2]
    assert not audio[3]


def test_health_snapshot_is_detached_from_mutating_callers():
    buffer = EventBuffer()
    state = healthy()
    buffer.set_system_health(state)
    state['rx']['up'] = False
    returned = buffer.snapshot()
    returned['system']['rx']['up'] = False
    assert buffer.snapshot()['system']['rx']['up']


def test_audio_delivery_gaps_remain_distinct_from_lost_packets():
    buffer = EventBuffer()
    state = healthy()
    state['rx'].update(audio_max_gap_seconds=0.125, lost_frames_total=0, audio_signal_peak=0.5)
    buffer.set_system_health(state)
    text = metrics(buffer).decode()
    assert 'radius_audio_max_gap_seconds{node="rx"} 0.125' in text
    assert 'radius_lost_frames_total{node="rx"} 0.0' in text
    assert 'radius_audio_signal_peak{node="rx"} 0.5' in text
    assert 'peak -6 dBFS' in health_summary(buffer.snapshot())['cards'][2][2]


def test_metrics_are_exposed_through_the_existing_snapshot_server():
    import urllib.request

    buffer = EventBuffer()
    buffer.set_system_health(healthy())
    server = SnapshotServer(buffer, 0)
    server.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.port}/metrics') as response:
            text = response.read().decode()
            assert '# TYPE radius_underruns_total counter' in text
            assert 'radius_underruns_total{node="rx"} 2.0' in text
    finally:
        server.stop()


def test_dashboard_and_prometheus_share_the_same_health_source():
    from autolux.simulate.visualizer_app import build_app

    buffer = EventBuffer()
    buffer.set_system_health(healthy())
    response = build_app(buffer).server.test_client().get('/metrics')
    assert response.status_code == 200
    assert b'autolux_up 1.0' in response.data


def test_lighting_delivery_counter_is_available_to_grafana():
    buffer = EventBuffer()
    state = healthy()
    state['rx']['dmx_frames_total'] = 120
    buffer.set_system_health(state)
    assert 'radius_dmx_frames_total{node="rx"} 120.0' in metrics(buffer).decode()
