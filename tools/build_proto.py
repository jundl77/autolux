"""Compile shared protocol descriptors for source runs and Python distributions."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parents[1]


def find_protoc() -> Path:
    executable = 'protoc.exe' if os.name == 'nt' else 'protoc'
    candidates = []
    if os.environ.get('PROTOC'):
        candidates.append(Path(os.environ['PROTOC']))
    if found := shutil.which('protoc'):
        candidates.append(Path(found))
    candidates.append(ROOT / 'radius/cmake-build-release/_deps/protobuf-build' / executable)
    cargo = Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))
    candidates.extend(
        sorted((cargo / 'registry/src').glob(f'*/protoc-bin-vendored-*/bin/{executable}'))
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError('protoc is missing. Build Radius or Starfire, or set PROTOC to its path.')


def generate(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(find_protoc()),
            f'--proto_path={ROOT / "proto/starfire"}',
            f'--descriptor_set_out={output}',
            '--include_imports',
            str(ROOT / 'proto/starfire/starfire.proto'),
        ],
        check=True,
        timeout=30,
    )


class BuildPy(build_py):
    def run(self):
        super().run()
        generate(Path(self.build_lib) / 'proto/starfire/starfire.pb')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/proto/starfire.pb')
    generate(parser.parse_args().output)
