import base64
import subprocess
from pathlib import Path

import pytest

from cli import remote_runtime
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
    ssh = Mock(side_effect=lambda script, **_: 'free' if 'Mutex' in script else 'ready')
    runtime.ssh = ssh
    process = Mock(poll=lambda: None, wait=lambda **_: 0)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=process))
    with runtime:
        assert runtime.process is process
        assert ssh.call_count == 1
    scripts = [call.args[0] for call in ssh.call_args_list]
    # The stop request, then the check that the receiver let go of the hardware.
    assert 'stop' in scripts[1] and scripts[2] == remote_runtime.RECEIVER_MUTEX_PROBE
    process.kill.assert_not_called()
    assert runtime.process is None


def _remote(tmp_path, answers):
    runtime = RemoteRuntime(
        ReceiverSpec(Path('rx.exe'), {}), '127.0.0.1', 'User', tmp_path, tmp_path, tmp_path
    )
    runtime.ssh = lambda script, **_: answers.pop(0)
    return runtime


def test_the_next_session_waits_for_the_old_receiver_to_free_the_hardware(tmp_path):
    clock = [0.0]
    runtime = _remote(tmp_path, ['busy', 'busy', 'free'])
    runtime.wait_receiver_gone(
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)
    )
    assert clock[0] == 1.0


def test_a_receiver_that_never_lets_go_fails_the_session(tmp_path):
    clock = [0.0]
    runtime = _remote(tmp_path, ['busy'] * 100)
    with pytest.raises(RuntimeError, match='still holds the hardware'):
        runtime.wait_receiver_gone(
            clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)
        )
    assert clock[0] == remote_runtime.RECEIVER_EXIT_TIMEOUT_SECONDS


def test_an_ssh_session_that_survives_its_kill_is_logged_not_raised(tmp_path, caplog):
    from unittest.mock import Mock

    runtime = _remote(tmp_path, [''])
    runtime.remote = {'run': 'C:/Autolux/runs/test'}
    runtime.process = Mock(wait=Mock(side_effect=subprocess.TimeoutExpired('ssh', 0)))
    runtime.close()
    assert runtime.process is None
    assert 'receiver SSH survived its kill' in caplog.text
