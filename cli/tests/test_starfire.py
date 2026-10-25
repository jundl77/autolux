import argparse
import asyncio
import json
import sys

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
    saved = tmp_path / 'saved-rig.json'
    initial = tmp_path / 'initial-rig.json'
    explicit = tmp_path / 'explicit-rig.json'
    monkeypatch.setattr(starfire, 'DEFAULT_FIXTURE_STORE', saved)
    monkeypatch.setattr(starfire, 'DEFAULT_RIG', initial)
    launched = []

    async def launch(command):
        launched.append(command)

    monkeypatch.setattr(starfire, '_run_child', launch)
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
