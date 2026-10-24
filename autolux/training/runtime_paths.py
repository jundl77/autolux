"""Resolve the current source tree for standalone campaign tools."""
from pathlib import Path


def code_component(path):
    root = Path(path).resolve()
    component = root / 'autolux' if (root / 'autolux/lib').is_dir() else root
    if component.name != 'autolux' or not (component / '__init__.py').is_file() or not (component / 'lib').is_dir():
        raise ValueError(f'{root} is not an Autolux source tree')
    return component
