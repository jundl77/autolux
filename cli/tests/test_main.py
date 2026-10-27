import argparse
import asyncio
import io
import threading

import pytest

from autolux.simulate.evaluator import print_evaluation
from cli import main


def test_report_printing_supports_redirected_windows_output(monkeypatch):
    output = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
    errors = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')

    async def print_report(_args):
        print_evaluation(
            {
                'passed': True,
                'score': 1.0,
                'criteria': {
                    'timing': {'value': 8.0, 'passed': True, 'spec': {'max': 15.0}},
                },
            }
        )

    monkeypatch.setattr(main, '_parse_command', lambda: argparse.Namespace(func=print_report))
    monkeypatch.setattr(main.sys, 'stdout', output)
    monkeypatch.setattr(main.sys, 'stderr', errors)
    main.run_sync()
    output.flush()
    printed = output.buffer.getvalue().decode('utf-8')
    assert 'PASS' in printed and '≤ 15.0' in printed and '✓' in printed
    assert errors.encoding == 'utf-8'


def test_blocking_command_owns_main_thread_without_an_asyncio_loop(monkeypatch):
    called = []

    def blocking_server(_args):
        assert threading.current_thread() is threading.main_thread()
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        called.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, '_parse_command', lambda: argparse.Namespace(func=blocking_server))
    main.run_sync()
    assert called == [True]
