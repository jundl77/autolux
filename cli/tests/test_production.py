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
        (None, False),
        (None, True),
    ],
)
async def test_partial_start_and_show_failure_clean_up_owned_endpoints(
    tmp_path, monkeypatch, failure, checking
):
    from cli import main, remote_runtime, windows_runtime, starfire_runtime, health_monitor
    from autolux.lib import section_chain
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lib.engine.effect_definitions import AUTOLOOP_BY_LIGHT_INTENT
    from types import SimpleNamespace
    import json

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
    monkeypatch.setattr(production, 'running_processes', lambda: [])
    monkeypatch.setattr(production, 'resolve_devices', lambda *_: 1)
    monkeypatch.setattr(section_chain, 'artifacts_present', lambda: True)

    def validate_command(command, **kwargs):
        assert command[1] == 'check'
        if failure == 'script_failure':
            return SimpleNamespace(
                returncode=1,
                stdout='',
                stderr='autoloop atmospheric/blue_tide failed: unknown group strip',
            )
        universes = [1] if failure == 'universe_mismatch' else [0]
        checked_autoloops = [
            {'bank': bank, 'autoloop': autoloop}
            for bank, autoloop in AUTOLOOP_BY_LIGHT_INTENT.values()
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
            pass

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

        def send_command(self, command):
            assert self.process is not None, 'command sent before renderer readiness'
            events.append(command['kind'])

        def check_health(self):
            pass

        def snapshot(self):
            return None

    class App:
        def __init__(self, *_, **kwargs):
            assert not any(name in events for name in ('rx_start', 'starfire_start'))
            if failure == 'controller_init':
                raise RuntimeError('model initialization failed')
            self.lighting_client = kwargs['lighting_client']
            self.event_buffer = EventBuffer()
            events.append('app_initialized')

        def _shut_down(self):
            events.append('app_stop')
            self.lighting_client.stop()

        async def run(self):
            assert all(name in events for name in ('rx_start', 'starfire_start'))
            events.append('app_run')
            try:
                self.health_check()
                if failure == 'controller_run':
                    raise RuntimeError('show failed')
            finally:
                self._shut_down()

    monkeypatch.setattr(windows_runtime, 'RoleLock', Lock)
    monkeypatch.setattr(remote_runtime, 'RemoteRuntime', Remote)
    monkeypatch.setattr(main, 'AutoluxController', App)
    monkeypatch.setattr(production, 'StarfireRuntime', Renderer)
    if failure == 'monitor_setup':
        from unittest.mock import Mock

        monkeypatch.setattr(
            health_monitor, 'HealthMonitor', Mock(side_effect=RuntimeError('monitor setup failed'))
        )
    if failure:
        with pytest.raises(
            RuntimeError, match='fixture universes' if failure == 'universe_mismatch' else None
        ):
            await production.run(args)
    elif checking:
        await production._run_session(args, checking=True)
    else:
        await production.run(args)
    preflight_failures = ('universe_mismatch', 'script_failure', 'missing_autoloop')
    construction_failures = (*preflight_failures, 'controller_init')
    assert events.count('app_stop') == (0 if checking or failure in construction_failures else 1)
    assert events[-1] == 'unlock'
    if failure in preflight_failures:
        assert events == ['unlock']
    elif checking:
        assert events == ['ssh_check', 'receiver_check', 'unlock']
    elif failure == 'controller_init':
        assert events == ['ssh_check', 'unlock']
    if 'starfire_stop' in events:
        assert events.index('starfire_stop') < events.index('rx_stop')
    if 'app_run' in events:
        assert events.index('app_stop') < events.index('starfire_stop')
        assert events.count('stop') == 1
    else:
        assert 'stop' not in events
    assert main.global_app is None
