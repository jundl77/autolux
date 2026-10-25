import argparse
from pathlib import Path

import pytest
from cli import production
from cli.native_runtime import NativeSpec


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


@pytest.mark.parametrize('failure', ['receiver_start', 'renderer_start', 'controller_run', None])
async def test_partial_start_and_show_failure_clean_up_owned_endpoints(
    tmp_path, monkeypatch, failure
):
    from cli import main, remote_runtime, windows_runtime
    from autolux.lib import section_chain
    from autolux.lib.engine.event_buffer import EventBuffer

    events = []
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    args = parser.parse_args([])
    tx = NativeSpec(
        'radius_tx',
        Path('tx.exe'),
        {
            'peer_address': '127.0.0.1',
            'session': 17,
            'artnet_address': '127.0.0.1',
            'dmx_universe': 0,
        },
    )
    rx = NativeSpec('radius_rx', Path('rx.exe'), {})
    monkeypatch.setattr(production, 'ROOT', tmp_path)
    (tmp_path / '.live').mkdir()
    monkeypatch.setattr(production, 'native_specs', lambda *_: [tx, rx])
    monkeypatch.setattr(production, 'validate_native', lambda *_: None)
    monkeypatch.setattr(production, 'running_processes', lambda: [])
    monkeypatch.setattr(production, 'resolve_devices', lambda *_: 1)
    monkeypatch.setattr(section_chain, 'artifacts_present', lambda: True)
    monkeypatch.setattr(production, 'validate_starfire', lambda *_: None)

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

        def __enter__(self):
            if failure == 'receiver_start':
                raise RuntimeError('start failed')
            events.append('rx_start')
            return self

        def __exit__(self, *_):
            events.append('rx_stop')

        def check_health(self):
            pass

    class Native:
        def __init__(self, specs, path):
            Path(path).mkdir(parents=True)

        def __enter__(self):
            events.append('tx_start')
            return self

        def __exit__(self, *_):
            events.append('tx_stop')

        def check_health(self):
            pass

    class Renderer:
        def __init__(self, binary, rig, sequences, artnet, directory):
            assert artnet == '127.0.0.1:6454'

        def __enter__(self):
            if failure == 'renderer_start':
                raise RuntimeError('renderer start failed')
            events.append('starfire_start')
            return self

        def __exit__(self, *_):
            events.append('starfire_stop')

        def send_command(self, command):
            events.append(command['kind'])

        def check_health(self):
            pass

        def snapshot(self):
            return None

    class App:
        def __init__(self, *_, **kwargs):
            assert kwargs['lighting_client'] is not None
            self.event_buffer = EventBuffer()

        def _shut_down(self):
            events.append('app_stop')

        async def run(self):
            try:
                self.health_check()
                if failure == 'controller_run':
                    raise RuntimeError('show failed')
            finally:
                self._shut_down()

    monkeypatch.setattr(windows_runtime, 'RoleLock', Lock)
    monkeypatch.setattr(remote_runtime, 'RemoteRuntime', Remote)
    monkeypatch.setattr(production, 'NativeRuntime', Native)
    monkeypatch.setattr(main, 'AutoluxController', App)
    monkeypatch.setattr(production, 'StarfireRuntime', Renderer)
    if failure:
        with pytest.raises(RuntimeError):
            await production.run(args)
    else:
        await production.run(args)
    assert events.count('app_stop') == (0 if failure in ('receiver_start', 'renderer_start') else 1)
    assert events[-1] == 'unlock'
    if failure != 'receiver_start':
        assert events.index('tx_stop') < events.index('rx_stop')
    if failure not in ('receiver_start', 'renderer_start'):
        assert events.index('starfire_stop') < events.index('tx_stop')
    assert main.global_app is None
