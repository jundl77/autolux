"""Exercise browser-independent draft integrity using Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_fixture_draft_integrity():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for the browser-side draft integrity checks')
    result = subprocess.run(
        [node, '--test', str(Path(__file__).with_name('fixture_draft.test.cjs'))],
        capture_output=True,
        check=False,
        text=True,
        encoding='utf-8',
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
