import os
from pathlib import Path
import subprocess
import sys

import pytest

from cli.production import check_conflicts


def test_conflicting_process_is_refused_without_killing_it():
    process = {'Name': 'starfire.exe', 'ProcessId': 123, 'ExecutablePath': 'another-checkout'}
    wanted = Path('starfire.exe')
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
