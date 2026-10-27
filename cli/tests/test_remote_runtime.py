import base64
import subprocess
from pathlib import Path

import pytest

from cli.remote_runtime import ReceiverSpec, RemoteRuntime, encoded_command, ps_literal


def test_remote_paths_cannot_inject_powershell():
    value = "C:/my user's folder/$(whoami)"
    assert ps_literal(value) == "'C:/my user''s folder/$(whoami)'"
    script = f'Write-Output {ps_literal(value)}'
    assert base64.b64decode(encoded_command(script)[-1]).decode('utf-16le') == script


@pytest.mark.parametrize('host,user', [('host; command', 'User'), ('127.0.0.1', 'User; command')])
def test_remote_target_rejects_shell_syntax(tmp_path, host, user):
    with pytest.raises(ValueError):
        RemoteRuntime(
            ReceiverSpec(Path('rx.exe'), {}),
            host,
            user,
            tmp_path / 'key',
            tmp_path / 'known',
            tmp_path / 'run',
        )


def test_ssh_uses_pinned_host_key_and_noninteractive_auth(tmp_path, monkeypatch):
    key, known = tmp_path / 'key', tmp_path / 'known'
    key.touch()
    known.touch()
    runtime = RemoteRuntime(
        ReceiverSpec(Path('rx.exe'), {}),
        '127.0.0.1',
        'User',
        key,
        known,
        tmp_path / 'run',
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, 'radius-ssh-ready\n', '')

    monkeypatch.setattr(subprocess, 'run', run)
    runtime.check_connection()
    assert 'StrictHostKeyChecking=yes' in calls[0]
    assert 'BatchMode=yes' in calls[0]
    assert f'UserKnownHostsFile={known}' in calls[0]


def test_missing_host_key_store_fails_before_network_access(tmp_path):
    key = tmp_path / 'key'
    key.touch()
    runtime = RemoteRuntime(
        ReceiverSpec(Path('rx.exe'), {}),
        '127.0.0.1',
        'User',
        key,
        tmp_path / 'missing',
        tmp_path / 'run',
    )
    with pytest.raises(RuntimeError, match='Missing SSH setup'):
        runtime.options()


def test_prepared_receiver_uses_rust_readiness_and_stops_owned_session(tmp_path, monkeypatch):
    from unittest.mock import Mock

    key, known = tmp_path / 'key', tmp_path / 'known'
    key.touch()
    known.touch()
    runtime = RemoteRuntime(
        ReceiverSpec(Path('radius_rx.exe'), {}),
        '127.0.0.1',
        'User',
        key,
        known,
        tmp_path / 'run',
    )
    runtime.run_dir.mkdir()
    runtime.remote = {'bundle': 'C:/Autolux/radius/test', 'run': 'C:/Autolux/runs/test'}
    ssh = Mock(return_value='ready')
    runtime.ssh = ssh
    process = Mock(poll=lambda: None, wait=lambda **_: 0)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=process))
    with runtime:
        assert runtime.process is process
        assert ssh.call_count == 1
    assert 'stop' in ssh.call_args.args[0]
    process.kill.assert_not_called()
    assert runtime.process is None
