import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from autolux.lib.main import AutoluxController


class _StopRun(Exception): ...


def _app(enable_ui: bool, event_buffer):
    from autolux.lib.delayed_monitor import DelayedMonitor
    from autolux.lib.main import PLAYBACK_DELAY_SEC

    app = object.__new__(AutoluxController)
    app._monitor = DelayedMonitor(PLAYBACK_DELAY_SEC, lambda audio: None)
    app.enable_ui = enable_ui
    app._ui_port = 8050
    app._ui = None
    app._fixture_setup = None
    app._enable_playback = False
    app.event_buffer = event_buffer
    app.audio_client = MagicMock()
    app.audio_client.read.side_effect = _StopRun
    app.lighting_client = MagicMock()
    app.section = None
    app.is_running = False
    app._stop_requested = False
    app.health_check = None
    app.control = MagicMock()
    app.light_engine = MagicMock()
    app.media_session = MagicMock()
    return app


def _threads_started(monkeypatch) -> list:
    started = []

    def record(**kwargs):
        started.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(threading, 'Thread', record)
    return started


def _run(app):
    with pytest.raises(_StopRun):
        asyncio.run(app.run())


def _bridge_started(monkeypatch) -> list:
    from autolux.lib import ui_bridge

    started = []
    monkeypatch.setattr(
        ui_bridge,
        'start',
        lambda buffer, port, **kwargs: started.append((buffer, port)) or MagicMock(),
    )
    return started


def test_report_without_ui_starts_no_viewer(monkeypatch):
    started = _bridge_started(monkeypatch)
    _run(_app(enable_ui=False, event_buffer=MagicMock()))
    assert started == []


def test_ui_hands_the_viewer_to_its_own_process(monkeypatch):
    started = _bridge_started(monkeypatch)
    app = _app(enable_ui=True, event_buffer=MagicMock())
    _run(app)
    assert started == [(app.event_buffer, 8050)]


def test_the_viewer_is_stopped_when_the_show_stops(monkeypatch):
    _bridge_started(monkeypatch)
    app = _app(enable_ui=True, event_buffer=MagicMock())
    _run(app)
    app._ui.stop.assert_called_once()


def test_no_dash_thread_shares_the_show_process(monkeypatch):
    started = _threads_started(monkeypatch)
    _bridge_started(monkeypatch)
    _run(_app(enable_ui=True, event_buffer=MagicMock()))
    assert started == []


def test_report_without_ui_still_starts_the_event_buffer(monkeypatch):
    _threads_started(monkeypatch)
    buffer = MagicMock()
    _run(_app(enable_ui=False, event_buffer=buffer))
    buffer.start.assert_called_once()


def test_no_buffer_and_no_ui_starts_neither(monkeypatch):
    started = _threads_started(monkeypatch)
    _run(_app(enable_ui=False, event_buffer=None))
    assert started == []


def test_a_raise_in_the_loop_still_blanks_the_rig_and_stops_every_thread(monkeypatch):
    _threads_started(monkeypatch)
    app = _app(enable_ui=False, event_buffer=None)
    app.section = MagicMock()

    _run(app)

    app.audio_client.close.assert_called_once()
    app.section.stop.assert_called_once()
    app.lighting_client.stop.assert_called_once()
    assert app.is_running is False


def test_one_client_failing_to_close_does_not_strand_the_rest(monkeypatch):
    _threads_started(monkeypatch)
    app = _app(enable_ui=False, event_buffer=None)
    app.audio_client.close.side_effect = OSError('device already gone')

    _run(app)

    app.lighting_client.stop.assert_called_once()


def _built_app(monkeypatch, **options):
    from autolux.lib import section_chain

    for module, name in (
        ('autolux.lib.clients.pyaudio_client', 'PyAudioClient'),
        ('autolux.lib.analyser.music_analyser', 'MusicAnalyser'),
        ('autolux.lib.engine.light_engine', 'LightEngine'),
        ('autolux.lib.engine.effect_controller', 'EffectController'),
    ):
        monkeypatch.setattr(f'{module}.{name}', MagicMock())
    monkeypatch.setattr(section_chain, 'artifacts_present', lambda *a, **k: False)
    monkeypatch.setattr(asyncio, 'get_event_loop', MagicMock())

    return AutoluxController(lighting_client=MagicMock(), **options)


def test_a_session_report_keeps_the_whole_session(monkeypatch):
    from autolux.lib.clock import VirtualClock
    from autolux.lib.engine.event_buffer import EventBuffer

    app = _built_app(monkeypatch, enable_ui=False, report_path='out.json')
    assert app.event_buffer._window_sec == float('inf')

    def half_an_hour(window):
        clock = VirtualClock()
        buffer = EventBuffer(window_sec=window, clock=clock)
        buffer.start()
        for index in range(60):
            clock.advance(30.0)
            buffer.set_intent(f'intent-{index % 2}', song_sec=float(index))
        return buffer.to_report()['metrics']['intent_changes_count']

    assert half_an_hour(60.0) < 10, 'the window used not to prune here'
    assert half_an_hour(float('inf')) == 60


def test_the_live_view_keeps_its_rolling_window(monkeypatch):
    buffer = _built_app(monkeypatch, enable_ui=True).event_buffer
    assert buffer._window_sec == 60.0


def test_the_snapshot_carries_the_rig_revision_the_session_loaded(monkeypatch):
    app = _built_app(monkeypatch, enable_ui=True, fixture_setup={'rig_revision': 'loaded'})
    assert app.event_buffer.snapshot()['rig_revision'] == 'loaded'


async def test_restart_show_from_the_config_tab_exits_the_show_cleanly(
    monkeypatch, tmp_path, caplog
):
    from autolux.lib.ui_bridge import parse_control_command

    app = _built_app(
        monkeypatch,
        enable_ui=True,
        fixture_setup={'rig_revision': 'loaded'},
        show_config_path=tmp_path / 'show.json',
    )
    app.is_running = True
    command = parse_control_command({'action': 'restart_show', 'ignored': 1})
    assert command == {'action': 'restart_show'}
    app.control.commands.put_nowait(command)
    with caplog.at_level('INFO'):
        await app.control.apply_pending(app.light_engine)
    # The same exit `autolux stop` takes: the loop ends and run() shuts down without a fault.
    assert (app._stop_requested, app.is_running) == (True, False)
    assert 'restart requested from the Config tab (rig changed)' in caplog.text


async def test_source_silence_keeps_the_monitor_audio_tail():
    from autolux.lib.delayed_monitor import DelayedMonitor
    from autolux.lib.main import PLAYBACK_DELAY_SEC
    from autolux.tests.test_light_engine import engine, hold_silence

    light, queue, clock, _ = engine()
    played = []
    monitor = DelayedMonitor(PLAYBACK_DELAY_SEC, played.append, clock=clock)
    for _ in range(20):
        clock.advance(1.0)
        monitor.feed(b'music')
    assert monitor.started and played
    buffered = monitor.buffered
    light.on_sound_stop()
    await hold_silence(light, clock)
    await queue.drain()
    assert monitor.buffered == buffered
    monitor.feed(b'silence')
    assert played[-1] == b'music'
