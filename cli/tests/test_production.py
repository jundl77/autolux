import argparse
from pathlib import Path

import pytest
from cli import production
from cli.native_runtime import NativeSpec


def test_live_commands_offer_verbose_diagnostics():
    from cli.main import build_parser

    parser = build_parser()
    assert parser.parse_args(['prod', '--verbose']).verbose
    assert parser.parse_args(['run', '1', '--verbose']).verbose
    assert not parser.parse_args(['prod']).verbose


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
    monkeypatch.setattr(production, 'native_specs', readiness)
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    with pytest.raises(
        RuntimeError, match='validation failed' if exit_code else 'readiness reached'
    ):
        await production.run(parser.parse_args(['--check']))
    assert calls == [[production.sys.executable, '-X', 'utf8', str(script)]]


@pytest.mark.parametrize('duration', ['0', '-1', 'nan', 'inf'])
async def test_invalid_duration_is_rejected_before_startup(duration):
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    with pytest.raises(ValueError, match='finite and positive'):
        await production.run(parser.parse_args(['--duration', duration]))


def test_specs_share_one_session_and_delay_only_transmitted_audio():
    parser = argparse.ArgumentParser()
    production.add_arguments(parser)
    args = parser.parse_args(['--receiver-host', '127.0.0.1'])
    tx, rx = production.native_specs(args, 14)
    assert tx.config['session'] == rx.config['session'] != 0
    assert tx.config['playback_delay_seconds'] == 14
    assert 'playback_delay_seconds' not in rx.config
    assert tx.config['artnet_address'] == '127.0.0.1'
    assert tx.config['dmx_universe'] == 0
    assert rx.config['peer_address'] == '127.0.0.1'
    assert (tx.name, rx.name) == ('radius_tx', 'radius_rx')


@pytest.mark.parametrize('failure', ['receiver_start', 'controller_run', None])
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
        {'peer_address': '127.0.0.1', 'session': 17, 'overlay_port': 19001, 'dmx_universe': 0},
    )
    rx = NativeSpec('radius_rx', Path('rx.exe'), {})
    monkeypatch.setattr(production, 'ROOT', tmp_path)
    (tmp_path / '.live').mkdir()
    monkeypatch.setattr(production, 'native_specs', lambda *_: [tx, rx])
    monkeypatch.setattr(production, 'validate_native', lambda *_: None)
    monkeypatch.setattr(production, 'running_processes', lambda: [{'Name': 'loopMIDI.exe'}])
    monkeypatch.setattr(production, 'resolve_devices', lambda *_: (1, 2))
    monkeypatch.setattr(section_chain, 'artifacts_present', lambda: True)

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

    class App:
        def __init__(self, *_, **kwargs):
            assert kwargs['overlay_host'] == '127.0.0.1'
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
    if failure:
        with pytest.raises(RuntimeError):
            await production.run(args)
    else:
        await production.run(args)
    assert events.count('app_stop') == 1
    assert events[-1] == 'unlock'
    if failure != 'receiver_start':
        assert events.index('tx_stop') < events.index('rx_stop')
    assert main.global_app is None
