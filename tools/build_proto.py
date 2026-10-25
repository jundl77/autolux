"""Generate standard Python Protobuf bindings for source runs and distributions."""

import argparse
from pathlib import Path

from grpc_tools import protoc
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parents[1]
GENERATED_ROOT = ROOT / 'build/generated'


def generate(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    result = protoc.main(
        [
            'protoc',
            f'--proto_path={ROOT}',
            f'--python_out={output}',
            f'--pyi_out={output}',
            *(str(path) for path in sorted((ROOT / 'proto/starfire').glob('*.proto'))),
        ]
    )
    if result:
        raise RuntimeError(f'Starfire Protobuf generation failed (exit {result})')


class BuildPy(build_py):
    def run(self):
        super().run()
        generate(Path(self.build_lib))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=GENERATED_ROOT)
    generate(parser.parse_args().output)
