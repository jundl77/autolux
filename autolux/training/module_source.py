"""Require offline tools to import packages from their declared source root."""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def experiment_checkout(environment_variable: str) -> Path:
    """Resolve an explicitly selected experiment tree; never substitute current code."""
    value = os.environ.get(environment_variable)
    if not value:
        raise RuntimeError(f"Set {environment_variable} to the required experiment checkout")
    root = Path(value).expanduser().resolve()
    if not (root / "training" / "nn").is_dir():
        raise RuntimeError(f"{environment_variable} must contain training/nn: {root}")
    return root


def experiment_python() -> Path:
    variable = "AUTOLUX_EXPERIMENT_PYTHON"
    value = os.environ.get(variable)
    if not value:
        raise RuntimeError(f"Set {variable} to the experiment's Python executable")
    executable = Path(value).expanduser().resolve()
    if not executable.is_file():
        raise RuntimeError(f"{variable} is not a file: {executable}")
    return executable


def require_imported_package_under_root(package: str, root: Path) -> None:
    imported_package_source_path = Path(importlib.import_module(package).__file__).resolve()
    if not imported_package_source_path.is_relative_to(Path(root).resolve()):
        raise RuntimeError(
            f"{package} resolved to {imported_package_source_path}, which is not under {root}. "
            f"Something on sys.path -- or an earlier import -- decided that, "
            f"not this script; fix the path setup rather than the symptom."
        )
