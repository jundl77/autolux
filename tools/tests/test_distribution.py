"""Exercise generated protocol dependencies through the published Python artifact."""

from pathlib import Path
import subprocess
import sys


def test_wheel_built_from_sdist_contains_both_generated_protocols(tmp_path):
    root = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ['uv', 'build', '--out-dir', str(tmp_path)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=120,
    )
    assert built.returncode == 0, built.stderr[-6000:]
    wheel = next(tmp_path.glob('*.whl'))
    probe = subprocess.run(
        [
            sys.executable,
            '-I',
            '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'from proto.starfire import starfire_pb2 as starfire; '
            'from proto.radius import radius_pb2 as radius; '
            'renderer = starfire.Metrics(run_id=2**64-1); '
            'transport = radius.Metrics(run_id=2**64-1); '
            'assert starfire.Metrics.FromString(renderer.SerializeToString()).run_id == 2**64-1; '
            'assert radius.Metrics.FromString(transport.SerializeToString()).run_id == 2**64-1',
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 0, probe.stderr
