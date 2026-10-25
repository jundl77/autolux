import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell build workflow')


def build_fixture(tmp_path, failed_step=None):
    (tmp_path / 'tools').mkdir()
    for name in ('build.ps1', 'build-radius.ps1', 'build-starfire.ps1', 'tools/build-native.ps1'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'proto').mkdir()
    (tmp_path / 'proto/test.proto').write_text('syntax = "proto3";\n')
    cargo = tmp_path / 'cargo.cmd'
    cargo.write_text(
        '@echo off\n'
        'echo %*>>"%~dp0cargo-invocations.txt"\n'
        'echo compiler progress 1>&2\n'
        f'if "%1" == "{failed_step}" exit /b 7\n'
        'exit /b 0\n'
    )
    (tmp_path / 'uv.cmd').write_text(
        '@echo off\n'
        'echo %*>>"%~dp0uv-invocations.txt"\n'
        'echo package progress 1>&2\n'
        f'if "%5" == "ruff" exit /b {7 if failed_step == "python" else 0}\n'
        f'if "%5" == "clang-format" exit /b {7 if failed_step == "proto" else 0}\n'
        f'if "%2" == "--no-project" exit /b {7 if failed_step == "protocol" else 0}\n'
        f'if "%1" == "build" exit /b {7 if failed_step == "package" else 0}\n'
        'exit /b 0\n'
    )
    return cargo


def run_build(tmp_path, script, *arguments):
    return subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-File',
            str(tmp_path / script),
            *arguments,
        ],
        check=False,
        capture_output=True,
        timeout=30,
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']},
    )


@pytest.mark.parametrize(
    'failed_step', [None, 'python', 'proto', 'protocol', 'fmt', 'clippy', 'test', 'build']
)
def test_native_build_stops_on_failure_and_accepts_progress_on_stderr(tmp_path, failed_step):
    cargo = build_fixture(tmp_path, failed_step)
    result = run_build(tmp_path, 'build-starfire.ps1', '-Cargo', str(cargo))
    assert (result.returncode == 0) == (failed_step is None)
    uv_calls = (tmp_path / 'uv-invocations.txt').read_text().splitlines()
    assert len(uv_calls) == {'python': 1, 'proto': 2}.get(failed_step, 3)
    cargo_log = tmp_path / 'cargo-invocations.txt'
    if failed_step in ('python', 'proto', 'protocol'):
        assert not cargo_log.exists()
    else:
        calls = cargo_log.read_text().splitlines()
        expected = ['fmt', 'clippy', 'test', 'build']
        if failed_step in expected:
            expected = expected[: expected.index(failed_step) + 1]
        assert [call.split()[0] for call in calls] == expected
        assert all('--package starfire' in call for call in calls[1:])
        assert b'compiler progress' in result.stderr


def test_proto_formatting_skips_deleted_tracked_files(tmp_path):
    cargo = build_fixture(tmp_path)
    old = tmp_path / 'proto/test.proto'
    subprocess.run(['git', 'add', 'proto/test.proto'], cwd=tmp_path, check=True)
    old.rename(tmp_path / 'proto/renamed.proto')
    result = run_build(tmp_path, 'build-radius.ps1', '-Cargo', str(cargo))
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / 'uv-invocations.txt').read_text()
    assert 'clang-format -i --style=file proto/renamed.proto' in invocations
    assert 'test.proto' not in invocations
    calls = (tmp_path / 'cargo-invocations.txt').read_text().splitlines()
    assert all('--package radius' in call for call in calls[1:])


@pytest.mark.parametrize('failed_step', [None, 'package'])
def test_workspace_build_runs_once_and_propagates_package_failure(tmp_path, failed_step):
    build_fixture(tmp_path, failed_step)
    result = run_build(tmp_path, 'build.ps1')
    assert (result.returncode == 0) == (failed_step is None)
    calls = (tmp_path / 'cargo-invocations.txt').read_text().splitlines()
    assert [call.split()[0] for call in calls] == ['fmt', 'clippy', 'test', 'build']
    assert all('--workspace' in call for call in calls[1:])
    assert (tmp_path / 'uv-invocations.txt').read_text().splitlines()[-1] == 'build'
    assert b'package progress' in result.stderr
