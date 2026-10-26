import hashlib
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from autolux.lighting import mvr
from autolux.lighting.gdtf import fixture_library, read_bounded
from autolux.lighting.mvr_validation import require_valid

DMX_UNIVERSE_SIZE = mvr.DMX_UNIVERSE_SIZE
MAX_ARTNET_UNIVERSE = 32767
MAX_FIXTURES = 512
MAX_NAME_BYTES = 128
ONE_ADDRESS_RULE = 'Starfire renders one Address on break 0 per fixture'


def rig_modes(scene: mvr.Scene) -> dict[str, dict]:
    return {
        spec: mvr.gdtf_modes(mvr.gdtf_description(scene, spec), spec)
        for spec in sorted(mvr.referenced_specs(scene.root))
        if spec in scene.entries
    }


def channel_occupancy(
    fixtures: list[dict], modes: dict, universe: int
) -> tuple[list[list[int]], list[str]]:
    occupied = [[] for _ in range(DMX_UNIVERSE_SIZE)]
    errors = []
    if not 1 <= len(fixtures) <= MAX_FIXTURES:
        errors.append(f'A rig needs 1 to {MAX_FIXTURES} patched fixtures.')
    names = set()
    addresses = {}
    for index, fixture in enumerate(fixtures):
        name = fixture.get('name')
        label = name or f'Fixture {index + 1}'
        if (
            not name
            or not name.strip()
            or len(name.encode('utf-8')) > MAX_NAME_BYTES
            or name in names
        ):
            errors.append(f'{label}: Starfire needs a nonempty, unique, short fixture name.')
        else:
            names.add(name)
        if [entry[0] for entry in fixture['addresses']] != [0]:
            errors.append(f'{label}: {ONE_ADDRESS_RULE}.')
            continue
        try:
            mvr.home_orientation(fixture['matrix'])
        except (KeyError, ValueError) as error:
            errors.append(f'{label}: {error}.')
            continue
        target, start = fixture['universe'], fixture['address']
        if not 0 <= target <= MAX_ARTNET_UNIVERSE:
            errors.append(f'{label}: universe {target + 1} exceeds Art-Net.')
            continue
        mode = modes.get(fixture['gdtf_spec'], {}).get(fixture['gdtf_mode'])
        if mode is None:
            errors.append(
                f'{label}: GDTF mode {fixture["gdtf_mode"]!r} is not in {fixture["gdtf_spec"]}.'
            )
            continue
        count = mode['channels']
        if start + count - 1 > DMX_UNIVERSE_SIZE:
            errors.append(f'{label}: {count} channels do not fit at address {start}.')
            continue
        conflicts = set()
        for channel in range(start, start + count):
            key = (target, channel)
            if key in addresses:
                conflicts.add(addresses[key])
            addresses[key] = index
            if target == universe:
                occupied[channel - 1].append(index)
        for other in sorted(conflicts):
            errors.append(f'{label} overlaps {fixtures[other]["name"]} in universe {target + 1}.')
    return occupied, errors


def require_renderable(scene: mvr.Scene) -> list[dict]:
    require_valid(scene)
    fixtures = mvr.patched_fixtures(scene)
    _, errors = channel_occupancy(fixtures, rig_modes(scene), 0)
    if errors:
        raise ValueError(errors[0])
    return fixtures


def load_rig(path: Path) -> tuple[mvr.Scene, list[dict]]:
    scene = mvr.read(read_bounded(Path(path), mvr.MAX_MVR_BYTES))
    return scene, require_renderable(scene)


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FixtureStore:
    def __init__(self, rig_path: Path, profiles_path: Path, validate: Callable[[Path], None]):
        self.rig_path = Path(rig_path)
        self.profiles_path = Path(profiles_path)
        self._validate = validate
        self._lock = threading.Lock()

    def library(self) -> dict:
        return fixture_library(self.profiles_path)

    def load(self) -> tuple[dict, str]:
        with self._lock:
            content = read_bounded(self.rig_path, mvr.MAX_MVR_BYTES)
            return mvr.draft(mvr.read(content)), _revision(content)

    def save(self, edited: dict, expected_revision: str) -> tuple[dict, str]:
        library = self.library()
        with self._lock:
            before = read_bounded(self.rig_path, mvr.MAX_MVR_BYTES)
            current = _revision(before)
            if current != expected_revision:
                raise ValueError('Fixture patch changed. Reload it before saving your edits.')
            scene = mvr.apply_draft(mvr.read(before), edited, library)
            require_renderable(scene)
            content = mvr.write(scene)
            staged_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='wb',
                    prefix='.rig-',
                    suffix='.mvr',
                    dir=self.rig_path.parent,
                    delete=False,
                ) as staged:
                    staged_path = Path(staged.name)
                    staged.write(content)
                    staged.flush()
                    os.fsync(staged.fileno())
                self._validate(staged_path)
                if _revision(self.rig_path.read_bytes()) != current:
                    raise ValueError('Fixture patch changed during validation. Reload and retry.')
                os.replace(staged_path, self.rig_path)
            finally:
                if staged_path is not None:
                    staged_path.unlink(missing_ok=True)
        return mvr.draft(mvr.read(content)), _revision(content)
