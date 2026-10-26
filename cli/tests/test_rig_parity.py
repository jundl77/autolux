"""Autolux's Login rig and Starfire's own MVR reader must build the same RigDefinition."""

import os
import subprocess
from pathlib import Path

import pytest

from autolux.lighting import mvr
from autolux.tests.test_mvr import sample_archive, sample_text
from cli.starfire_assets import rig_definition
from proto.starfire import rig_pb2 as rig_wire

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / 'target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
DIM = 'uuid="44444444-0000-4000-8000-000000000001" name="Dim 1"'


def starfire_rig(path: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BINARY), 'rig', str(path), '--definition', str(tmp_path / 'definition.pb')],
        capture_output=True,
        check=False,
        text=True,
        encoding='utf-8',
        timeout=30,
    )


def edited_sample(old: str, new: str) -> bytes:
    text = sample_text()
    assert old in text
    return sample_archive(text.replace(old, new, 1))


@pytest.fixture(autouse=True)
def binary():
    if not BINARY.is_file():
        pytest.skip('Build Starfire to compare the native MVR reader')


@pytest.mark.parametrize(
    'name,content',
    [
        ('room', lambda: (ROOT / 'rigs/room.mvr').read_bytes()),
        ('generic', lambda: (ROOT / 'rigs/generic.mvr').read_bytes()),
        ('sample', sample_archive),
        # Explicit default limits, inversion only and a standing head keep one canonical form.
        (
            'movement',
            lambda: edited_sample(
                '<DMXInvertPan>false</DMXInvertPan>', '<DMXInvertPan>true</DMXInvertPan>'
            ),
        ),
        (
            'standing',
            lambda: edited_sample(
                '{1,0,0}{0,1,0}{0,0,1}{500,0,0}', '{1,0,0}{0,-1,0}{0,0,-1}{500,0,0}'
            ),
        ),
    ],
)
def test_python_login_rig_equals_the_native_reader(tmp_path, name, content):
    path = tmp_path / f'{name}.mvr'
    path.write_bytes(content())
    scene = mvr.read(path.read_bytes())
    if name == 'movement':
        for fixture in mvr.patched_fixtures(scene):
            mvr.set_movement_limits(scene, fixture['uuid'], 'tilt', 0.0, 1.0)
        mvr.set_movement_limits(scene, mvr.patched_fixtures(scene)[0]['uuid'], 'pan', 0.25, 0.75)
        path.write_bytes(mvr.write(scene))
    result = starfire_rig(path, tmp_path)
    assert result.returncode == 0, result.stderr
    native = rig_wire.RigDefinition.FromString((tmp_path / 'definition.pb').read_bytes())
    assert rig_definition(path) == native


def test_render_rules_reject_in_both_readers_with_one_wording(tmp_path):
    path = tmp_path / 'steep.mvr'
    # Rx(75): the tilt-home approximation is refused on both sides.
    path.write_bytes(
        edited_sample(
            '{1,0,0}{0,1,0}{0,0,1}{500,0,0}',
            '{1,0,0}{0,0.258819,0.965926}{0,-0.965926,0.258819}{500,0,0}',
        )
    )
    rule = 'fixture pitch 75.0 exceeds +/-60 degrees'
    with pytest.raises(ValueError) as error:
        rig_definition(path)
    assert rule in str(error.value)
    result = starfire_rig(path, tmp_path)
    assert result.returncode != 0
    assert rule in result.stderr
