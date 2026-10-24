import json
import time

from autolux.lib.engine.event_buffer import EventBuffer
from autolux.lib.system_health import health_summary, metrics
from autolux.lib.ui_bridge import SnapshotServer


def healthy():
    return {'updated_at': time.time(), 'tx_age_seconds': .1, 'rx_age_seconds': .3,
            'tx': {'up': True, 'link_up': True, 'dmx_input_age_seconds': .01},
            'rx': {'up': True, 'link_up': True, 'audio_buffer_seconds': .08,
                   'buffer_target_seconds': .08, 'audio_input_age_seconds': .01,
                   'dmx_connected': True, 'dmx_lateness_seconds': .006,
                   'underruns_total': 2}, 'recent': {}}


def test_old_counters_do_not_make_a_recovered_show_permanently_unhealthy():
    snapshot = {'system': healthy()}
    assert health_summary(snapshot)['state'] == 'healthy'
    snapshot['system']['recent'] = {'underruns': 1}
    assert health_summary(snapshot)['state'] == 'attention'


def test_stale_receiver_never_looks_connected_or_exports_a_zero_buffer():
    buffer = EventBuffer()
    state = healthy()
    state['rx_age_seconds'] = 5
    buffer.set_system_health(state)
    assert health_summary(buffer.snapshot())['state'] == 'attention'
    text = metrics(buffer).decode()
    assert 'radius_up{node="rx"} 0.0' in text
    assert 'radius_audio_buffer_seconds{node="rx"}' not in text
    assert 'radius_up{node="tx"} 1.0' in text


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
    state['rx'].update(audio_max_gap_seconds=.125, lost_frames_total=0, audio_signal_peak=.5)
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
    from autolux.simulate.visualizer_app import build_app, _build_system_health
    buffer = EventBuffer()
    buffer.set_system_health(healthy())
    assert len(_build_system_health(buffer.snapshot())) == 6
    response = build_app(buffer).server.test_client().get('/metrics')
    assert response.status_code == 200
    assert b'autolux_up 1.0' in response.data
