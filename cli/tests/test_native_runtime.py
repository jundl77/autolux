import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cli.native_runtime import NativeRuntime, NativeSpec
from cli.production import check_conflicts


@pytest.fixture
def children(tmp_path, monkeypatch):
    helper = tmp_path / 'child.py'
    helper.write_text(
        '''import json, os, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
directory = Path(os.environ['AUTOLUX_RUNTIME_DIR'])
if config.get('exit'):
    raise SystemExit(7)
if not config.get('hang'):
    (directory / 'ready').write_text('autolux-native-v1')
while not (directory / 'stop').exists():
    time.sleep(0.01)
(directory / 'stopped').touch()
''',
        encoding='utf-8',
    )
    original = subprocess.Popen
    launched = []

    def spawn(command, **kwargs):
        process = original([sys.executable, str(helper), command[1]], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    yield launched
    for process in launched:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def spec(name, **config):
    return NativeSpec(name, Path(sys.executable), config)


def test_owned_children_become_ready_and_stop_gracefully(tmp_path, children):
    directory = tmp_path / 'session'
    with NativeRuntime([spec('audio'), spec('dmx')], directory) as runtime:
        assert len(runtime.children) == 2
        runtime.check_health()
    assert all(p.poll() == 0 for p in children)
    assert (directory / 'audio/stopped').exists()
    assert (directory / 'dmx/stopped').exists()


def test_failed_second_child_cleans_up_first_child(tmp_path, children):
    with pytest.raises(RuntimeError, match='dmx exited with code 7'):
        with NativeRuntime([spec('audio'), spec('dmx', exit=True)], tmp_path / 'session'):
            pytest.fail('partial startup must not succeed')
    assert all(p.poll() is not None for p in children)


def test_startup_without_readiness_times_out_and_is_reaped(tmp_path, children):
    with pytest.raises(RuntimeError, match='did not become ready'):
        with NativeRuntime([spec('audio', hang=True)], tmp_path / 'session', startup_timeout=0.3):
            pytest.fail('unready child must not succeed')
    assert children[0].poll() is not None


def test_death_after_startup_is_not_silent(tmp_path, children):
    with NativeRuntime([spec('audio')], tmp_path / 'session') as runtime:
        children[0].kill()
        children[0].wait(timeout=5)
        with pytest.raises(RuntimeError, match='audio exited'):
            runtime.check_health()


def test_conflicting_process_is_refused_without_killing_it():
    process = {'Name': 'radius_tx.exe', 'ProcessId': 123, 'ExecutablePath': 'another-checkout'}
    wanted = NativeSpec('radius_tx', Path('radius_tx.exe'), {})
    with pytest.raises(RuntimeError, match='PID 123'):
        check_conflicts([wanted], [process])
    check_conflicts([wanted], [{'Name': 'radius_rx.exe'}])


@pytest.mark.skipif(os.name != 'nt', reason='Windows process ownership')
def test_role_lock_spans_instances_and_releases():
    from cli.windows_runtime import RoleLock

    first = RoleLock('test-' + str(os.getpid()))
    try:
        with pytest.raises(RuntimeError, match='already running'):
            RoleLock('test-' + str(os.getpid()))
    finally:
        first.close()
    second = RoleLock('test-' + str(os.getpid()))
    second.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows process ownership')
def test_job_close_kills_owned_children_without_touching_others():
    from cli.windows_runtime import Job

    children = [
        subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)'],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(2)
    ]
    job = Job()
    try:
        job.add(children[0].pid)
        job.close()
        children[0].wait(timeout=5)
        assert children[1].poll() is None
    finally:
        job.close()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
