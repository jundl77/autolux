import json
import pytest
from cli.health_monitor import HealthMonitor


def test_stale_file_ages_and_wrong_session_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'session': 5, 'timestamp': 100, 'rx_age_seconds': .2}))
    monkeypatch.setattr('cli.health_monitor.time.time', lambda: 102)
    monitor = HealthMonitor(path, 5)
    assert monitor.snapshot()['rx_age_seconds'] == 2.2
    monkeypatch.setattr('cli.health_monitor.time.time', lambda: 110)
    assert monitor.snapshot()['rx_age_seconds'] == 10.2
    with pytest.raises(ValueError, match='different session'):
        HealthMonitor(path, 6).snapshot()


def test_recent_glitches_clear_after_ten_seconds(tmp_path, monkeypatch):
    path = tmp_path / 'health.json'
    monitor = HealthMonitor(path, 1)
    for second in range(100, 114):
        monkeypatch.setattr('cli.health_monitor.time.time', lambda: second)
        path.write_text(json.dumps({'session': 1, 'timestamp': second,
                                   'rx': {'underruns_total': int(second > 100)}}))
        snapshot = monitor.snapshot()
        if second == 101:
            assert snapshot['recent']['underruns'] == 1
    assert snapshot['recent']['underruns'] == 0
