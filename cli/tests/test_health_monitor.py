import json
import pytest
from cli.health_monitor import HealthMonitor


def ready_health():
    from autolux.tests.test_system_health import healthy

    return healthy()


def test_health_log_summarizes_periodically_and_reports_fault_and_recovery(caplog):
    import logging
    from cli.health_monitor import HealthLogger

    now = [0]
    logger = HealthLogger(clock=lambda: now[0])
    state = ready_health()
    with caplog.at_level(logging.INFO):
        for tick in range(300):
            now[0] = tick / 10
            logger.update(state)
        assert len(caplog.records) == 1
        assert 'buffer=80 ms' in caplog.text
        now[0] = 30
        logger.update(state)
        assert len(caplog.records) == 2
        state['recent'] = {'underruns': 1}
        now[0] = 31
        logger.update(state)
        assert caplog.records[-1].levelno == logging.WARNING
        assert '1 underruns' in caplog.records[-1].message
        logger.update(state)
        assert len(caplog.records) == 3
        state['recent'] = {}
        now[0] = 32
        logger.update(state)
        assert caplog.records[-1].levelno == logging.INFO
        assert 'audio=Stable' in caplog.records[-1].message


def test_missing_health_is_unavailable_and_stale_link_is_a_warning(caplog):
    import logging
    from cli.health_monitor import HealthLogger

    with caplog.at_level(logging.INFO):
        logger = HealthLogger()
        logger.update({'updated_at': __import__('time').time()})
        assert 'buffer=Unavailable' in caplog.text
        state = ready_health()
        logger.update(state)
        state['rx_age_seconds'] = 5
        logger.update(state)
    assert caplog.records[-1].levelno == logging.WARNING
    assert 'link=Waiting / offline' in caplog.records[-1].message


def test_individual_latency_samples_do_not_flood_the_log(caplog):
    import logging
    from cli.health_monitor import HealthLogger

    state = ready_health()
    logger = HealthLogger(clock=lambda: 0)
    with caplog.at_level(logging.INFO):
        for gap in [0.01, 0.06, 0.02, 0.07, 0.02]:
            state['rx']['dmx_lateness_seconds'] = gap
            logger.update(state)
    assert len(caplog.records) == 1


def test_stale_file_ages_and_wrong_session_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'session': 5, 'timestamp': 100, 'rx_age_seconds': 0.2}))
    monkeypatch.setattr('cli.health_monitor.time.time', lambda: 102)
    monitor = HealthMonitor(path, 5)
    assert monitor.snapshot()['sample_timestamp'] == 100
    assert monitor.snapshot()['session'] == 5
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
        path.write_text(
            json.dumps(
                {'session': 1, 'timestamp': second, 'rx': {'underruns_total': int(second > 100)}}
            )
        )
        snapshot = monitor.snapshot()
        if second == 101:
            assert snapshot['recent']['underruns'] == 1
    assert snapshot['recent']['underruns'] == 0
