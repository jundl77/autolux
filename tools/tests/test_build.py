import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell build workflow')
@pytest.mark.parametrize('exit_code', [0, 7])
def test_build_accepts_normal_stderr_but_propagates_package_failure(tmp_path, exit_code):
    shutil.copyfile(Path(__file__).resolve().parents[2] / 'build.ps1', tmp_path / 'build.ps1')
    (tmp_path / 'build-radius.ps1').write_text("Write-Output 'native build fixture'\n")
    (tmp_path / 'build-starfire.ps1').write_text("Write-Output 'Rust build fixture'\n")
    (tmp_path / 'uv.cmd').write_text(
        f'@echo off\necho package progress 1>&2\nexit /b {exit_code}\n'
    )
    (tmp_path / 'harness.ps1').write_text(
        'param([string]$Build, [string]$Log)\n'
        '$env:PATH = "$PSScriptRoot;$env:PATH"\n'
        '& $Build *> $Log\n'
        'exit $LASTEXITCODE\n'
    )
    log = tmp_path / 'build.log'
    result = subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-File',
            str(tmp_path / 'harness.ps1'),
            '-Build',
            str(tmp_path / 'build.ps1'),
            '-Log',
            str(log),
        ],
        capture_output=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (exit_code == 0)
    assert 'package progress' in log.read_text(encoding='utf-16')


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell build workflow')
@pytest.mark.parametrize('source_directory', ['radius', 'proto/radius'])
def test_native_formatting_skips_deleted_tracked_files(tmp_path, source_directory):
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / 'build-radius.ps1', tmp_path / 'build-radius.ps1'
    )
    native = tmp_path / source_directory
    native.mkdir(parents=True)
    deleted = native / 'old.proto'
    deleted.write_text('syntax = "proto3";\n')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'add', f'{source_directory}/old.proto'], cwd=tmp_path, check=True)
    deleted.rename(native / 'radius.proto')
    (tmp_path / 'uv.cmd').write_text('@echo off\necho %*>>"%~dp0uv-invocations.txt"\nexit /b 0\n')
    environment = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    result = subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-File',
            str(tmp_path / 'build-radius.ps1'),
            '-CMake',
            str(tmp_path / 'missing-cmake.exe'),
        ],
        env=environment,
        capture_output=True,
        timeout=30,
    )
    # This fixture stops after formatting, before any compiler can run.
    assert result.returncode != 0
    invocations = (tmp_path / 'uv-invocations.txt').read_text()
    assert f'clang-format -i --style=file {source_directory}/radius.proto' in invocations
    assert 'old.proto' not in invocations


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell build workflow')
@pytest.mark.parametrize('failed_step', [None, 'fmt', 'test', 'protocol'])
def test_starfire_build_stops_on_failure_and_accepts_progress_on_stderr(tmp_path, failed_step):
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / 'build-starfire.ps1',
        tmp_path / 'build-starfire.ps1',
    )
    cargo = tmp_path / 'cargo.cmd'
    cargo.write_text(
        '@echo off\n'
        'echo %1>>"%~dp0cargo-invocations.txt"\n'
        'echo compiler progress 1>&2\n'
        f'if "%1" == "{failed_step}" exit /b 7\n'
        'exit /b 0\n'
    )
    (tmp_path / 'uv.cmd').write_text(
        '@echo off\n'
        'echo %*>>"%~dp0protocol-invocations.txt"\n'
        'echo protocol compiler progress 1>&2\n'
        f'exit /b {7 if failed_step == "protocol" else 0}\n'
    )
    result = subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-File',
            str(tmp_path / 'build-starfire.ps1'),
            '-Cargo',
            str(cargo),
        ],
        capture_output=True,
        timeout=30,
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']},
    )
    assert (result.returncode == 0) == (failed_step is None)
    invocations = (tmp_path / 'cargo-invocations.txt').read_text().splitlines()
    expected = ['fmt', 'clippy', 'test', 'build']
    if failed_step in expected:
        expected = expected[: expected.index(failed_step) + 1]
    assert invocations == expected
    protocol_invocations = tmp_path / 'protocol-invocations.txt'
    if failed_step in (None, 'protocol'):
        assert protocol_invocations.read_text().splitlines() == [
            'run --locked python tools/build_proto.py'
        ]
        assert b'protocol compiler progress' in result.stderr
    else:
        assert not protocol_invocations.exists()
    if failed_step == 'protocol':
        assert b'Starfire protocol generation failed' in result.stderr
    assert b'compiler progress' in result.stderr
