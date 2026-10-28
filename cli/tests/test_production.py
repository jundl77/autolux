import argparse
from pathlib import Path

import pytest

from cli import production
from cli.remote_runtime import ReceiverSpec


def test_receiver_playback_settings_are_sent_in_transmitter_login(tmp_path, monkeypatch):
    import json
    import secrets

    random_values = iter([0, 2**64 - 1])
    requested_bits = []

    def random_run_id(bits):
        requested_bits.append(bits)
        return next(random_values)

    monkeypatch.setattr(secrets, 'randbits', random_run_id)

    for name in ('transmitter', 'receiver'):
        config = {
            'peer_address': '127.0.0.1',
            'bus_port': 19002,
            'sample_rate': 44100,
            'jitter_buffer_ms': 500,
            'audio_enabled': True,
            'dmx_enabled': True,
            'audio_device_id': 'capture',
            'dmx_universe': 0,
        }
        (tmp_path / f'{name}.json').write_text(json.dumps(config))
    binary = tmp_path / 'radius_rx.exe'
    binary.touch()
    args = argparse.Namespace(
        tx_config=tmp_path / 'transmitter.json',
        rx_config=tmp_path / 'receiver.json',
        receiver_binary=binary,
        receiver_host=None,
    )
    transmitter, receiver = production.transport_settings(args, 14)
    assert transmitter['run_id'] == receiver.config['run_id'] == 1
    output = production.radius_output(transmitter)
    assert output.receiver_address == '127.0.0.1:19002'
    assert output.playback_delay_seconds == 14
    assert output.receiver_buffer_ms == 500
    assert output.receiver_audio_enabled and output.receiver_lighting_enabled
    restarted_tx, restarted_rx = production.transport_settings(args, 14)
    assert restarted_tx['run_id'] == restarted_rx.config['run_id'] == 2**64 - 1
    assert requested_bits == [64, 64]


@pytest.mark.parametrize('exit_code', [0, 7])
async def test_full_check_delegates_to_tools_before_device_readiness(
    tmp_path, monkeypatch, exit_code
):
    from types import SimpleNamespace

    script = tmp_path / 'tools/validate.py'
    script.parent.mkdir()
    script.write_text('# validation fixture\n')
    monkeypatch.setattr(production, 'ROOT', tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=exit_code)

    def readiness(*args):
        raise RuntimeError('readiness reached')

    monkeypatch.setattr(production.subprocess, 'run', run)
    monkeypatch.setattr(production, 'run_readiness_check', readiness)
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    with pytest.raises(
        RuntimeError, match='validation failed' if exit_code else 'readiness reached'
    ):
        await production.run(parser.parse_args(['--check-full']))
    assert calls == [[production.sys.executable, '-X', 'utf8', str(script)]]


async def test_quick_check_only_runs_readiness_without_starting_outputs(monkeypatch):
    from unittest.mock import Mock

    readiness = Mock()
    monkeypatch.setattr(production, 'run_readiness_check', readiness)
    monkeypatch.setattr(
        production.subprocess, 'run', Mock(side_effect=AssertionError('full suite'))
    )
    monkeypatch.setattr(production, '_run_session', Mock(side_effect=AssertionError('live show')))
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    args = parser.parse_args(['--check'])
    await production.run(args)
    readiness.assert_called_once_with(args)


def _blocked_readiness(*_):
    import time

    time.sleep(60)


def test_stalled_readiness_worker_is_killed_within_its_deadline(monkeypatch):
    import multiprocessing
    import time

    monkeypatch.setattr(production, '_readiness_worker', _blocked_readiness)
    monkeypatch.setattr(production, 'READINESS_TIMEOUT_SECONDS', 0.1)
    existing_children = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='timed out'):
        production.run_readiness_check(argparse.Namespace())
    assert time.monotonic() - started < 5
    assert {child.pid for child in multiprocessing.active_children()} == existing_children


def test_controller_preparation_needs_no_audio_stream_or_ready_renderer(monkeypatch):
    from unittest.mock import Mock

    from autolux.lib import section_chain
    from autolux.lib.analyser import music_analyser
    from autolux.lib.clients import pyaudio_client
    from autolux.lib.clients.starfire_client import StarfireClient
    from autolux.lib.main import AutoluxController

    audio = Mock()
    send = Mock()
    monkeypatch.setattr(pyaudio_client, 'PyAudioClient', Mock(return_value=audio))
    monkeypatch.setattr(music_analyser, 'MadmomRhythm', Mock())
    monkeypatch.setattr(section_chain, 'artifacts_present', lambda: False)

    app = AutoluxController(lighting_client=StarfireClient(send))
    try:
        audio.start_streams.assert_not_called()
        send.assert_not_called()
    finally:
        app._shut_down()
    audio.close.assert_called_once()
    send.assert_called_once_with({'kind': 'stop'})


@pytest.mark.parametrize(
    'failure,checking',
    [
        ('universe_mismatch', False),
        ('script_failure', False),
        ('missing_autoloop', False),
        ('controller_init', False),
        ('receiver_start', False),
        ('renderer_start', False),
        ('monitor_setup', False),
        ('controller_run', False),
        ('audio_open', False),
        ('first_health', False),
        ('dashboard_lost', False),
        ('teardown', False),
        ('teardown_on_stop', False),
        ('restart_request', False),
        (None, False),
        (None, True),
    ],
)
async def test_partial_start_and_show_failure_clean_up_owned_endpoints(
    tmp_path, monkeypatch, failure, checking
):
    import json
    from types import SimpleNamespace

    from autolux.lib import section_chain
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lighting.autoloop_catalogue import required_autoloops
    from cli import health_monitor, main, remote_runtime, starfire_runtime, windows_runtime

    events = []
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    args = parser.parse_args([])
    args.starfire_binary = tmp_path / 'starfire.exe'
    args.starfire_binary.touch()
    tx = {
        'peer_address': '127.0.0.1',
        'bus_port': 19002,
        'run_id': 17,
        'dmx_universe': 0,
        'audio_device_id': 'capture',
        'playback_delay_seconds': 14,
        'receiver': {'audio_enabled': True, 'lighting_enabled': True, 'jitter_buffer_ms': 500},
    }
    rx = ReceiverSpec(Path('rx.exe'), {'run_id': 17})
    monkeypatch.setattr(production, 'ROOT', tmp_path)
    (tmp_path / '.live').mkdir()
    monkeypatch.setattr(production, 'transport_settings', lambda *_: [tx, rx])
    monkeypatch.setattr(production, 'running_processes', list)
    monkeypatch.setattr(production, 'resolve_devices', lambda *_: 1)
    monkeypatch.setattr(section_chain, 'artifacts_present', lambda: True)

    def validate_command(command, **kwargs):
        assert command[1] == 'check'
        if failure == 'script_failure':
            return SimpleNamespace(
                returncode=1,
                stdout='',
                stderr='autoloop atmospheric/cold_breathe failed: unknown group strip',
            )
        universes = [1] if failure == 'universe_mismatch' else [0]
        checked_autoloops = [
            {'bank': bank, 'autoloop': autoloop}
            for bank, autoloop in sorted(required_autoloops(args.autoloops))
        ]
        if failure == 'missing_autoloop':
            checked_autoloops.pop()
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({'universes': universes, 'checked_autoloops': checked_autoloops}),
            stderr='',
        )

    monkeypatch.setattr(starfire_runtime.subprocess, 'run', validate_command)

    class Lock:
        def __init__(self, *_):
            pass

        def close(self):
            events.append('unlock')

    class Remote:
        def __init__(self, *_):
            pass

        def check_connection(self):
            events.append('ssh_check')

        def prepare(self):
            events.append('receiver_check')

        def __enter__(self):
            assert 'app_initialized' in events
            if failure == 'receiver_start':
                raise RuntimeError('start failed')
            events.append('rx_start')
            return self

        def __exit__(self, *_):
            events.append('rx_stop')

        def check_health(self):
            if failure == 'first_health':
                raise RuntimeError('receiver link lost')

    class Renderer:
        def __init__(self, binary, rig, autoloops, artnet, directory, **kwargs):
            assert artnet is None
            assert kwargs['run_id'] == rx.config['run_id'] == 17
            assert kwargs['radius'].receiver_buffer_ms == 500
            self.process = None

        def __enter__(self):
            assert 'app_initialized' in events
            if failure == 'renderer_start':
                raise RuntimeError('renderer start failed')
            self.process = object()
            events.append('starfire_start')
            return self

        def __exit__(self, *_):
            events.append('starfire_stop')
            self.process = None
            if failure in ('teardown', 'teardown_on_stop'):
                raise RuntimeError('Starfire shutdown failed')

        def send_command(self, command):
            assert self.process is not None, 'command sent before renderer readiness'
            events.append(command['kind'])

        def check_health(self):
            pass

        def snapshot(self):
            return None

    class LightEngine:
        def note_capture_discontinuities(self, total):
            events.append('capture_count')

    class App:
        _stop_requested = restart_requested = False
        _ui = SimpleNamespace(dashboard_lost=lambda: failure == 'dashboard_lost')

        def __init__(self, *_, **kwargs):
            assert not any(name in events for name in ('rx_start', 'starfire_start'))
            if failure == 'controller_init':
                raise RuntimeError('model initialization failed')
            self.lighting_client = kwargs['lighting_client']
            self.event_buffer = EventBuffer()
            # A constructed AutoluxController always owns its engine; the health check relies on it.
            self.light_engine = LightEngine()
            events.append('app_initialized')

        def _shut_down(self):
            events.append('app_stop')
            self.lighting_client.stop()

        async def run(self):
            assert all(name in events for name in ('rx_start', 'starfire_start'))
            events.append('app_run')
            try:
                if failure == 'audio_open':
                    raise OSError('capture device unavailable')
                self.health_check()
                if failure == 'controller_run':
                    raise KeyError('show failed')
                self.restart_requested = failure == 'restart_request'
                self._stop_requested = failure == 'teardown_on_stop'
            finally:
                self._shut_down()

    monkeypatch.setattr(windows_runtime, 'RoleLock', Lock)
    monkeypatch.setattr(remote_runtime, 'RemoteRuntime', Remote)
    monkeypatch.setattr(main, 'AutoluxController', App)
    monkeypatch.setattr(main, 'write_report', lambda app, path: events.append(('report', path)))
    monkeypatch.setattr(production, 'StarfireRuntime', Renderer)
    args.report = 'show.json'
    if failure == 'monitor_setup':
        from unittest.mock import Mock

        monkeypatch.setattr(
            health_monitor, 'HealthMonitor', Mock(side_effect=RuntimeError('monitor setup failed'))
        )
    if failure == 'controller_run':
        # A show that ran and then failed, on any error, is a fault the session loop restarts.
        with pytest.raises(production.ShowFault, match='show failed'):
            await production._run_session(args)
    elif failure == 'dashboard_lost':
        # A relaunched viewer that never got its port fails the session; the loop backs off.
        with pytest.raises(RuntimeError, match='dashboard did not come back') as raised:
            await production._run_session(args, number=2)
        assert not isinstance(raised.value, production.ShowFault)
    elif failure == 'restart_request':
        assert await production._run_session(args) == 'restart'
    elif failure == 'teardown':
        # Once the show played, a failed teardown restarts it like any fault.
        with pytest.raises(production.ShowFault, match='Starfire shutdown failed'):
            await production._run_session(args)
    elif failure == 'teardown_on_stop':
        # ...but never brings back a show the operator stopped.
        assert await production._run_session(args) == 'stop'
    elif failure:
        # A first show that fails before its first passed health check exits for the operator.
        with pytest.raises(
            (RuntimeError, OSError),
            match='fixture universes' if failure == 'universe_mismatch' else None,
        ) as raised:
            await production.run(args)
        assert not isinstance(raised.value, production.ShowFault)
    elif checking:
        await production._run_session(args, checking=True)
    else:
        await production.run(args)
        [(_, report)] = [event for event in events if event[0] == 'report']
        assert Path(report).name == 'show.json' and Path(report).parent.parent == tmp_path / '.live'
    preflight_failures = ('universe_mismatch', 'script_failure', 'missing_autoloop')
    construction_failures = (*preflight_failures, 'controller_init')
    assert events.count('app_stop') == (0 if checking or failure in construction_failures else 1)
    # The controller lock belongs to run(), held across sessions; a session alone takes none.
    direct = ('controller_run', 'dashboard_lost', 'restart_request', 'teardown', 'teardown_on_stop')
    locked = not checking and failure not in direct
    assert events.count('unlock') == locked and (not locked or events[-1] == 'unlock')
    if failure in preflight_failures:
        assert events == ['unlock']
    elif checking:
        assert events == ['ssh_check', 'receiver_check']
    elif failure == 'controller_init':
        assert events == ['ssh_check', 'unlock']
    if 'starfire_stop' in events:
        assert events.index('starfire_stop') < events.index('rx_stop')
    if 'app_run' in events:
        unhealthy = ('audio_open', 'first_health', 'dashboard_lost')
        assert ('capture_count' in events) == (failure not in unhealthy)
        assert events.index('app_stop') < events.index('starfire_stop')
        assert events.count('stop') == 1
    else:
        assert 'stop' not in events
    assert main.global_app is None


class _Shows:
    """Scripted session outcomes on a simulated clock: each entry is (run seconds, outcome)."""

    def __init__(self, *script, stop_at=None):
        self.now = 0
        self.script = list(script)
        self.starts = []
        self.stop_at = stop_at

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds

    def stop_requested(self):
        return self.stop_at is not None and self.now >= self.stop_at

    async def session(self):
        self.starts.append(self.now)
        seconds, outcome = self.script.pop(0)
        self.now += seconds
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def run(self):
        await production.run_shows(
            self.session, self.stop_requested, clock=self.clock, sleep=self.sleep
        )


async def test_a_config_tab_restart_starts_the_next_show_at_once(caplog):
    shows = _Shows((30, 'restart'), (30, 'restart'), (30, 'stop'))
    with caplog.at_level('INFO'):
        await shows.run()
    assert shows.starts == [0, 30, 60]
    assert caplog.text.count('restarting on request from the Config tab') == 2


async def test_a_stop_ends_the_loop_without_a_relaunch():
    shows = _Shows((30, 'stop'))
    await shows.run()
    assert shows.starts == [0]


async def test_faults_back_off_and_a_healthy_run_resets_the_wait(caplog):
    fault = production.ShowFault('Starfire heartbeat response timed out')
    shows = _Shows((1, fault), (1, fault), (1, fault), (600, fault), (1, 'stop'))
    with caplog.at_level('INFO'):
        await shows.run()
    # 5, 15, 60 s after each short run; the 10-minute run starts over at 5 s.
    assert shows.starts == [0, 6, 22, 83, 688]
    assert 'show fault 3 in a row: Starfire heartbeat response timed out; restarting in 60 s' in (
        caplog.text
    )
    assert 'healthy run of 600 s; the fault backoff starts over' in caplog.text
    assert 'show fault 1 in a row: Starfire heartbeat response timed out; restarting in 5 s' in (
        caplog.text
    )
    assert caplog.text.endswith('show stopped; exiting\n')


async def test_a_fault_storm_including_failed_relaunches_waits_60_s_each():
    fault = production.ShowFault('capture resyncs')
    # Any failed relaunch retries, not only the expected classes.
    setup = KeyError('receiver unreachable')
    shows = _Shows((0, fault), (0, setup), (0, setup), (0, fault), (0, setup), (0, 'stop'))
    await shows.run()
    gaps = [later - earlier for earlier, later in zip(shows.starts, shows.starts[1:])]
    assert gaps == [5, 15, 60, 60, 60]


async def test_a_stop_during_the_backoff_exits_before_the_relaunch():
    shows = _Shows((0, production.ShowFault('renderer exited')), (0, 'stop'), stop_at=3)
    await shows.run()
    assert (shows.starts, shows.now) == ([0], 3)


async def test_a_stop_with_a_pending_restart_request_exits():
    shows = _Shows((0, 'restart'), (0, 'stop'), stop_at=0)
    await shows.run()
    assert shows.starts == [0]


async def test_a_first_show_that_cannot_start_exits_for_the_operator():
    shows = _Shows((0, RuntimeError('An Autolux controller is already running.')))
    with pytest.raises(RuntimeError, match='already running'):
        await shows.run()
    assert shows.starts == [0]


def test_the_loop_reads_the_stop_flag_of_the_current_session(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(production, 'ROOT', tmp_path)
    run_dir = tmp_path / '.live' / 'run'
    run_dir.mkdir(parents=True)
    (tmp_path / '.live/current.json').write_text(json.dumps({'run_dir': str(run_dir)}))
    assert not production.stop_requested()
    (run_dir / 'stop').touch()
    assert production.stop_requested()
    # A current.json caught mid-write is no stop, not a crash of the waiting loop.
    (tmp_path / '.live/current.json').write_text('{"run_d')
    assert not production.stop_requested()


def _prod_args(*argv):
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    return parser.parse_args(list(argv))


async def test_run_holds_the_controller_lock_through_the_backoff_wait(monkeypatch):
    import functools

    from cli import windows_runtime

    held = []
    waits = []

    class Lock:
        def __init__(self, role):
            held.append(role)

        def close(self):
            held.append('released')

    outcomes = [production.ShowFault('renderer exited'), 'stop']

    async def session(args, **_):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(seconds):
        waits.append(list(held))
        now[0] += seconds

    now = [0]
    monkeypatch.setattr(windows_runtime, 'RoleLock', Lock)
    monkeypatch.setattr(production, '_run_session', session)
    monkeypatch.setattr(production, 'stop_requested', lambda: False)
    monkeypatch.setattr(
        production,
        'run_shows',
        functools.partial(production.run_shows, clock=lambda: now[0], sleep=sleep),
    )
    await production.run(_prod_args())
    assert waits and all(state == ['controller'] for state in waits)
    assert held == ['controller', 'released']


async def test_one_duration_deadline_spans_every_session(monkeypatch):
    from cli import windows_runtime

    seen = {}

    async def loop(session, stop):
        seen['stop'] = [stop(), stop()]
        await session()
        await session()

    async def fake_session(args, *, number, deadline):
        seen.setdefault('sessions', []).append((number, deadline))

    times = iter([100, 129, 130])
    monkeypatch.setattr(windows_runtime, 'RoleLock', lambda role: argparse.Namespace(close=list))
    monkeypatch.setattr(production, 'run_shows', loop)
    monkeypatch.setattr(production, '_run_session', fake_session)
    monkeypatch.setattr(production, 'stop_requested', lambda: False)
    await production.run(_prod_args('--duration', '30'), clock=lambda: next(times))
    # Every session ends at the same deadline, and no relaunch starts after it.
    assert seen['sessions'] == [(1, 130), (2, 130)]
    assert seen['stop'] == [False, True]


async def test_a_stop_that_lands_on_the_old_session_during_a_relaunch_still_stops(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    monkeypatch.setattr(production, 'ROOT', tmp_path)
    monkeypatch.setattr(production, 'transport_settings', lambda *_: ({}, None))
    # The old session's stop file appears between the read and the current.json write.
    monkeypatch.setattr(
        production, '_stop_file', lambda: Mock(exists=Mock(side_effect=[False, True]))
    )
    monkeypatch.setattr(production, 'running_processes', Mock(side_effect=AssertionError))
    assert await production._run_session(_prod_args(), number=2) == 'stop'
