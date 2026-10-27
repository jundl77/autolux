"""Compile the synthetic GDTF test descriptions into the profiles Starfire's tests load."""

import argparse
from pathlib import Path

from autolux.lighting import mvr
from autolux.lighting.gdtf_compile import compile_mode
from proto.starfire import rig_pb2 as rig_wire

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'autolux/tests/data/gdtf'
OUTPUT = ROOT / 'starfire/tests/data/compiled_profiles.pb'


def compiled_profiles() -> bytes:
    """Every mode of every test description, as `<file stem>#<mode>` profiles."""
    rig = rig_wire.RigDefinition()
    for path in sorted(SOURCES.glob('*.xml')):
        description = path.read_text(encoding='utf-8')
        for mode in mvr.gdtf_modes(description, path.name):
            rig.profiles.add(
                profile_id=f'{path.stem}#{mode}',
                dmx_mode=mode,
                mode=compile_mode(description, mode),
            )
    return rig.SerializeToString()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    output = parser.parse_args().output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(compiled_profiles())
