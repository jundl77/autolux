"""Validated, atomic fixture patch storage for the dashboard."""

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path


class FixtureStore:
    def __init__(self, patch_path: Path, profiles_path: Path, validate: Callable[[Path], None]):
        self.patch_path = Path(patch_path)
        self.profiles_path = Path(profiles_path)
        self._validate = validate
        self._lock = threading.Lock()

    def profiles(self) -> dict:
        return json.loads(self.profiles_path.read_text(encoding='utf-8'))['profiles']

    def load(self) -> tuple[dict, str]:
        with self._lock:
            content = self.patch_path.read_bytes()
            return json.loads(content), hashlib.sha256(content).hexdigest()

    def save(self, patch: dict, expected_revision: str) -> str:
        content = (json.dumps(patch, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode(
            'utf-8'
        )
        with self._lock:
            current = hashlib.sha256(self.patch_path.read_bytes()).hexdigest()
            if current != expected_revision:
                raise ValueError('Fixture patch changed. Reload it before saving your edits.')
            staged_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='wb',
                    prefix='.rig-',
                    suffix='.json',
                    dir=self.patch_path.parent,
                    delete=False,
                ) as staged:
                    staged_path = Path(staged.name)
                    staged.write(content)
                    staged.flush()
                    os.fsync(staged.fileno())
                self._validate(staged_path)
                # Detect external edits made while the native validator was running.
                if hashlib.sha256(self.patch_path.read_bytes()).hexdigest() != current:
                    raise ValueError('Fixture patch changed during validation. Reload and retry.')
                os.replace(staged_path, self.patch_path)
            finally:
                if staged_path is not None:
                    staged_path.unlink(missing_ok=True)
        return hashlib.sha256(content).hexdigest()
