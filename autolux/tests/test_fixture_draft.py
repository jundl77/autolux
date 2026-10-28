import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from autolux.lighting import mvr, mvr_schema
from autolux.lighting.gdtf import fixture_library
from autolux.simulate.fixture_panel import FIXTURE_COLORS, MAP_COLUMNS, browser_library

ROOT = Path(__file__).resolve().parents[2]
PARITY_ROTATIONS = [(0, 0, 0), (180, 0, 0), (-90, 0, 0), (270, 0, 0), (30, 20, -40), (60, 90, 0)]


def test_fixture_draft_integrity(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for the browser-side draft integrity checks')
    room = mvr.read((ROOT / 'autolux/tests/data/room.mvr').read_bytes())
    data = {
        'draft': mvr.draft(room),
        'settings': {
            # The sample rig's dimmer is a type the room does not use yet.
            'library': browser_library(
                fixture_library(ROOT / 'rig/library')
                | fixture_library(ROOT / 'autolux/tests/data/generic_rig')
            ),
            'schema': mvr_schema.describe(),
            'channelCount': mvr.DMX_UNIVERSE_SIZE,
            'maxUniverse': 32767,
            'maxQuantity': 64,
            'columns': MAP_COLUMNS,
            'colors': FIXTURE_COLORS,
            'accentRole': mvr_schema.ACCENT_ROLE,
            'roles': mvr_schema.ROLES,
        },
        'parity': [
            {
                'rotation': list(rotation),
                'matrix': mvr.format_matrix(mvr.compose_matrix([10, -20, 30], rotation)),
                'decomposed': list(mvr.rotation_degrees(mvr.compose_matrix([0, 0, 0], rotation))),
            }
            for rotation in PARITY_ROTATIONS
        ],
        'patched': [
            [
                fixture['name'],
                fixture['universe'],
                fixture['address'],
                [
                    cell['geometry']
                    for cell in mvr.gdtf_modes(mvr.gdtf_description(room, fixture['gdtf_spec']))[
                        fixture['gdtf_mode']
                    ]['cells']
                ],
            ]
            for fixture in mvr.patched_fixtures(room)
        ],
    }
    source = tmp_path / 'draft.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    result = subprocess.run(
        [node, '--test', str(Path(__file__).with_name('fixture_draft.test.cjs'))],
        capture_output=True,
        check=False,
        text=True,
        encoding='utf-8',
        timeout=60,
        env={**os.environ, 'FIXTURE_DRAFT_DATA': str(source)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
