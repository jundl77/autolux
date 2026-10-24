"""Run the complete repository validation serially, without starting a show."""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CHECK_TIMEOUT_SECONDS = 3600
TIMEOUT_EXIT_CODE = 124


def validation_steps(output_dir):
    audio_files = sorted((ROOT / 'autolux/training/eval_audio').glob('*.mp3'))
    if not audio_files:
        raise RuntimeError(
            'Full validation requires the source checkout and committed evaluation audio.'
        )
    python = [sys.executable, '-X', 'utf8']
    return [
        (
            'python-format',
            python + ['-m', 'ruff', 'format', '--check', 'autolux', 'cli', 'radius', 'tools'],
        ),
        ('cpp-format', python + ['-m', 'pre_commit', 'run', 'cpp-format', '--all-files']),
        (
            'build',
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(ROOT / 'build.ps1')],
        ),
        ('tests', python + ['-m', 'pytest', '-q', '-ra']),
        ('backtest', python + ['autolux/training/run_eval_set.py']),
        (
            'simulation',
            python
            + [
                '-m',
                'autolux',
                'simulate',
                'file',
                str(audio_files[0]),
                '--report',
                str(output_dir / 'simulation.json'),
            ],
        ),
    ]


def run_checks(steps, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    results = []
    for name, command in steps:
        log_path = output_dir / f'{name}.log'
        print(f'[validate] {name} — {log_path}', flush=True)
        started = time.monotonic()
        with log_path.open('w', encoding='utf8') as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=CHECK_TIMEOUT_SECONDS,
                )
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                log.write(f'Check exceeded {CHECK_TIMEOUT_SECONDS} seconds.\n')
                exit_code = TIMEOUT_EXIT_CODE
            except OSError as error:
                log.write(f'{error}\n')
                exit_code = 1
        results.append(
            {
                'name': name,
                'exit_code': exit_code,
                'seconds': round(time.monotonic() - started, 2),
                'log': str(log_path),
            }
        )
        print(f'[validate] {name}: {"PASS" if exit_code == 0 else "FAIL"}', flush=True)
        if exit_code:
            print(
                '\n'.join(log_path.read_text(encoding='utf8', errors='replace').splitlines()[-20:])
            )
    return results


def main():
    output_dir = ROOT / '.live/validation' / datetime.now().strftime('%Y%m%d-%H%M%S')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    results = run_checks(validation_steps(output_dir), output_dir)
    report = output_dir / 'results.json'
    report.write_text(
        json.dumps({'revision': revision, 'checks': results}, indent=2), encoding='utf8'
    )
    print(f'[validate] Results for {revision[:12]}: {report}', flush=True)
    return int(any(result['exit_code'] for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
