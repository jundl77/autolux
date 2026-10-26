import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from cli import starfire


async def test_owned_native_process_success_and_failure_are_reported():
    await starfire._run_child([sys.executable, '-c', 'raise SystemExit(0)'])
    with pytest.raises(RuntimeError, match='code 3'):
        await starfire._run_child([sys.executable, '-c', 'raise SystemExit(3)'])


class Child:
    def __init__(self, stop_file, *, exits_on_stop):
        self.returncode = None
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stop_file = stop_file
        self.killed = False
        self.exits_on_stop = exits_on_stop

    async def wait(self):
        self.started.set()
        if self.exits_on_stop and self.stop_file.exists():
            self.returncode = 0
            self.finished.set()
        await self.finished.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.finished.set()


@pytest.mark.parametrize('exits_on_stop', [True, False])
async def test_cancellation_requests_native_blackout_before_bounded_forcekill(
    monkeypatch, tmp_path, exits_on_stop
):
    stop_file = tmp_path / 'stop'
    child = Child(stop_file, exits_on_stop=exits_on_stop)

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(starfire.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(starfire, 'CHILD_STOP_TIMEOUT_SEC', 0.01)
    task = asyncio.create_task(starfire._run_child(['owned-renderer'], stop_file=stop_file))
    await child.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stop_file.exists()
    assert child.killed is not exits_on_stop


async def test_missing_binary_fails_before_launch(monkeypatch, tmp_path):
    async def unexpected_launch(*args, **kwargs):
        pytest.fail('missing renderer must not launch anything or invoke a compiler')

    monkeypatch.setattr(starfire.asyncio, 'create_subprocess_exec', unexpected_launch)
    with pytest.raises(RuntimeError, match='binary is missing'):
        await starfire.run_native(
            argparse.Namespace(binary=tmp_path / 'missing', starfire_action='list')
        )


async def test_report_conversion_writes_native_input_without_modifying_report(tmp_path):
    source = tmp_path / 'session.json'
    output = tmp_path / 'replay.json'
    report = {
        'duration_sec': 20.0,
        'metrics': {'look_ahead_sec': 14.0},
        'beats': [{'t': 1.0, 'bpm': 120.0}],
        'intents': [{'t': 15.0, 'song_t': 1.0, 'intent': 'drop'}],
    }
    source.write_text(json.dumps(report), encoding='utf-8')
    args = argparse.Namespace(input=source, output=output, frame_rate_hz=40)
    await starfire.convert_report(args)
    replay = json.loads(output.read_text())
    assert replay['events'][0]['at_seconds'] == replay['events'][1]['at_seconds'] == 1.0
    assert json.loads(source.read_text()) == report
    args.output = source
    with pytest.raises(ValueError, match='must differ'):
        await starfire.convert_report(args)
    assert json.loads(source.read_text()) == report


async def test_next_replay_loads_saved_fixture_patch_unless_operator_selects_another(
    monkeypatch, tmp_path
):
    binary = tmp_path / 'renderer'
    binary.touch()
    saved = tmp_path / 'saved-rig.mvr'
    initial = tmp_path / 'initial-rig.mvr'
    explicit = tmp_path / 'explicit-rig.mvr'
    monkeypatch.setattr(starfire, 'DEFAULT_FIXTURE_STORE', saved)
    monkeypatch.setattr(starfire, 'DEFAULT_RIG', initial)
    launched = []

    async def launch(command):
        launched.append(command)

    monkeypatch.setattr(starfire, '_run_child', launch)
    monkeypatch.setattr(starfire, 'native_rig', contextlib.nullcontext)
    args = argparse.Namespace(
        binary=binary,
        starfire_action='replay',
        input=tmp_path / 'input.json',
        rig=None,
    )
    await starfire.run_native(args)
    saved.write_text('{}')
    await starfire.run_native(args)
    args.rig = explicit
    await starfire.run_native(args)
    assert [command[command.index('--rig') + 1] for command in launched] == [
        str(initial),
        str(saved),
        str(explicit),
    ]


@pytest.mark.parametrize('shutdown', ['owned_process', 'keyboard_interrupt'])
def test_dashboard_http_readiness_and_bounded_shutdown(tmp_path, shutdown):
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lib.ui_bridge import SnapshotServer, kill_tree

    if not starfire.DEFAULT_BINARY.is_file():
        pytest.skip('Build Starfire to validate the dashboard entry process')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    source = SnapshotServer(EventBuffer(), 0)
    source.start()
    process = None
    log_path = tmp_path / 'dashboard.log'
    interrupt_file = tmp_path / 'interrupt'
    entry = ['-m', 'autolux']
    if shutdown == 'keyboard_interrupt':
        entry = [
            '-c',
            'import _thread, sys, threading, time\n'
            'from pathlib import Path\n'
            'from cli.main import run_sync\n'
            'interrupt_file = Path(sys.argv.pop(1))\n'
            'def interrupt():\n'
            '    while not interrupt_file.exists():\n'
            '        time.sleep(0.05)\n'
            '    _thread.interrupt_main()\n'
            'threading.Thread(target=interrupt, daemon=True).start()\n'
            'run_sync()\n',
            str(interrupt_file),
        ]
    try:
        with log_path.open('wb') as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    *entry,
                    'starfire',
                    'ui',
                    '--port',
                    str(port),
                    '--snapshot-port',
                    str(source.port),
                    '--rig-store',
                    str(tmp_path / 'rig.mvr'),
                ],
                cwd=starfire.ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **(
                    {
                        'creationflags': subprocess.CREATE_NO_WINDOW
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                    }
                    if os.name == 'nt'
                    else {'start_new_session': True}
                ),
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, log_path.read_text(errors='replace')
            try:
                with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/metrics', timeout=0.5
                ) as response:
                    assert response.status == 200
                    assert b'autolux_up' in response.read()
                break
            except urllib.error.URLError:
                time.sleep(0.05)
        else:
            pytest.fail(f'Dashboard readiness timed out:\n{log_path.read_text(errors="replace")}')
        assert 'EventLoopBlocked' not in log_path.read_text(errors='replace')
        if shutdown == 'keyboard_interrupt':
            interrupt_file.touch()
            assert process.wait(timeout=5) == 0, log_path.read_text(errors='replace')
    finally:
        if process is not None:
            kill_tree(process)
        source.stop()
    assert process.poll() is not None
