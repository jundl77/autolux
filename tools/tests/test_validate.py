import subprocess
import sys

from tools import validate


def test_checks_continue_after_failure_and_preserve_unicode_output(tmp_path):
    steps = [
        ('fail', [sys.executable, '-c', 'print("failed"); raise SystemExit(7)']),
        ('unicode', [sys.executable, '-c', 'print("音声 → lighting")']),
    ]
    results = validate.run_checks(steps, tmp_path)
    assert [result['exit_code'] for result in results] == [7, 0]
    assert '音声 → lighting' in (tmp_path / 'unicode.log').read_text(encoding='utf8')


def test_timeouts_are_failures_and_do_not_skip_remaining_checks(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(validate.subprocess, 'run', run)
    results = validate.run_checks([('slow', ['slow']), ('next', ['next'])], tmp_path)
    assert [result['exit_code'] for result in results] == [validate.TIMEOUT_EXIT_CODE, 0]
    assert 'exceeded' in (tmp_path / 'slow.log').read_text()
