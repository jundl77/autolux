"""Compile the GDTF test descriptions and corpus into the profiles Starfire's tests load."""

import argparse
import zipfile
from pathlib import Path

from autolux.lighting import mvr
from autolux.lighting.gdtf_compile import compile_mode
from proto.starfire import rig_pb2 as rig_wire

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'autolux/tests/data/gdtf'
CORPUS = ROOT / 'tests/data/corpus/gdtf'
OUTPUT = ROOT / 'starfire/tests/data/compiled_profiles.pb'


def corpus_descriptions() -> dict[str, str]:
    return {
        path.stem: zipfile.ZipFile(path).read(mvr.GDTF_DESCRIPTION).decode('utf-8')
        for path in sorted(CORPUS.glob('*.gdtf'))
    }


def compiled_profiles() -> bytes:
    """Every synthetic mode as `<file stem>#<mode>`, every corpus mode that compiles as
    `corpus/<file stem>#<mode>`; autolux/tests/test_gdtf_compile.py pins the refusals."""
    rig = rig_wire.RigDefinition()
    for path in sorted(SOURCES.glob('*.xml')):
        description = path.read_text(encoding='utf-8')
        for mode in mvr.gdtf_modes(description, path.name):
            rig.profiles.add(
                profile_id=f'{path.stem}#{mode}',
                dmx_mode=mode,
                mode=compile_mode(description, mode),
            )
    for stem, description in corpus_descriptions().items():
        try:
            modes = mvr.gdtf_modes(description, stem)
        except ValueError:
            continue
        for mode in modes:
            try:
                compiled = compile_mode(description, mode)
            except ValueError:
                continue
            rig.profiles.add(profile_id=f'corpus/{stem}#{mode}', dmx_mode=mode, mode=compiled)
    return rig.SerializeToString()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    output = parser.parse_args().output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(compiled_profiles())
