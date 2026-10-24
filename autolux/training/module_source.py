"""Require offline tools to import packages from their declared source root."""

from __future__ import annotations

import importlib
from pathlib import Path


def require_imported_package_under_root(package: str, root: Path) -> None:
    imported_package_source_path = Path(importlib.import_module(package).__file__).resolve()
    if not imported_package_source_path.is_relative_to(Path(root).resolve()):
        raise RuntimeError(
            f"{package} resolved to {imported_package_source_path}, which is not under {root}. "
            f"Something on sys.path -- or an earlier import -- decided that, "
            f"not this script; fix the path setup rather than the symptom."
        )
