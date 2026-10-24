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
def test_native_formatting_skips_deleted_tracked_files(tmp_path):
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / 'build-radius.ps1', tmp_path / 'build-radius.ps1'
    )
    native = tmp_path / 'radius'
    native.mkdir()
    deleted = native / 'old.proto'
    deleted.write_text('syntax = "proto3";\n')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'add', 'radius/old.proto'], cwd=tmp_path, check=True)
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
    assert 'clang-format -i --style=file radius/radius.proto' in invocations
    assert 'old.proto' not in invocations
