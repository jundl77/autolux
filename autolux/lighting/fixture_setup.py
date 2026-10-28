import hashlib
import logging
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
REPO_RIG_NAME = 'room.mvr'
RIG_DEFINITION_SUFFIX = '.rig.pb'
LOGGER = logging.getLogger(__name__)


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
    errors += mvr.group_roles(fixtures)[1]
    if errors:
        raise ValueError(errors[0])
    return fixtures


def load_rig(path: Path) -> tuple[mvr.Scene, list[dict]]:
    scene = mvr.read(read_bounded(Path(path), mvr.MAX_MVR_BYTES))
    return scene, require_renderable(scene)


def _fixture_type_id(scene: mvr.Scene, spec: str) -> str:
    fixture_type = mvr.gdtf_root(mvr.gdtf_description(scene, spec), spec).find('FixtureType')
    return (fixture_type.get('FixtureTypeID') or '').upper()


def refresh_profiles(rig_path: Path, library_path: Path) -> list[str]:
    """Bring the store's embedded GDTF up to the library's; the store keeps only the patch.

    A stored profile is the library's fixture type when their FixtureTypeIDs match; it takes
    the library archive and name, and the patch's GDTFSpec references follow the name.
    Returns the refreshed specs; the store is rewritten only when one changed.
    """
    rig_path = Path(rig_path)
    scene = mvr.read(read_bounded(rig_path, mvr.MAX_MVR_BYTES))
    library = {
        entry['fixture_type_id'].upper(): (spec, entry['archive'], entry['modes'])
        for spec, entry in fixture_library(library_path).items()
    }
    patched = mvr.patched_fixtures(scene)
    renames = {}
    for spec in sorted(mvr.referenced_specs(scene.root) & set(scene.entries)):
        guid = _fixture_type_id(scene, spec)
        if guid not in library:
            LOGGER.warning(
                '[rig] %s (FixtureTypeID %s) is not in the library; keeping the stored profile',
                spec,
                guid or 'missing',
            )
            continue
        current, archive, modes = library[guid]
        if current == spec and scene.entries[spec][1] == archive:
            continue
        used = {fixture['gdtf_mode'] for fixture in patched if fixture['gdtf_spec'] == spec}
        if missing := sorted(used - set(modes)):
            LOGGER.warning(
                '[rig] %s lacks the patched mode(s) %s; keeping the stored %s',
                current,
                ', '.join(missing),
                spec,
            )
            continue
        renames[spec] = current, archive
    if not renames:
        return []
    for node in scene.root.iter('GDTFSpec'):
        node.text = renames[node.text][0] if node.text in renames else node.text
    refreshed = mvr.Scene(
        scene.root, {name: entry for name, entry in scene.entries.items() if name not in renames}
    )
    for spec, archive in renames.values():
        mvr.embed_archive(refreshed, spec, archive)
    require_renderable(refreshed)
    _replace(rig_path, mvr.write(refreshed))
    for old, (new, _) in renames.items():
        LOGGER.info(
            '[rig] refreshed %s from the library%s', old, '' if new == old else f' as {new}'
        )
    return sorted(renames)


def _stage(path: Path, content: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        mode='wb', prefix='.rig-', suffix=path.suffix, dir=path.parent, delete=False
    ) as staged:
        staged.write(content)
        staged.flush()
        os.fsync(staged.fileno())
    return Path(staged.name)


def _replace(path: Path, content: bytes) -> None:
    staged = _stage(path, content)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def rig_revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FixtureStore:
    def __init__(self, rig_path: Path, library_path: Path, validate: Callable[[Path], bytes]):
        """`validate` returns the RigDefinition Starfire accepted for the staged MVR."""
        self.rig_path = Path(rig_path)
        self.library_path = Path(library_path)
        # The tracked rig the show seeds from sits beside the library its GDTF comes from.
        self.repo_rig = self.library_path.parent / REPO_RIG_NAME
        self._validate = validate
        self._lock = threading.Lock()

    def library(self) -> dict:
        return fixture_library(self.library_path)

    def load(self) -> tuple[dict, str]:
        with self._lock:
            content = read_bounded(self.rig_path, mvr.MAX_MVR_BYTES)
            return mvr.draft(mvr.read(content)), rig_revision(content)

    def save(self, edited: dict, expected_revision: str) -> tuple[dict, str]:
        library = self.library()
        with self._lock:
            before = read_bounded(self.rig_path, mvr.MAX_MVR_BYTES)
            current = rig_revision(before)
            if current != expected_revision:
                raise ValueError('Fixture patch changed. Reload it before saving your edits.')
            scene = mvr.apply_draft(mvr.read(before), edited, library)
            require_renderable(scene)
            content = mvr.write(scene)
            staged = {}
            try:
                staged[self.rig_path] = _stage(self.rig_path, content)
                definition = self._validate(staged[self.rig_path])
                if self.repo_rig != self.rig_path:
                    staged[self.repo_rig] = _stage(self.repo_rig, content)
                exported = self.repo_rig.with_suffix(RIG_DEFINITION_SUFFIX)
                staged[exported] = _stage(exported, definition)
                # Another process may edit the rig while the native validator runs.
                if rig_revision(self.rig_path.read_bytes()) != current:
                    raise ValueError('Fixture patch changed during validation. Reload and retry.')
                for target, source in staged.items():
                    os.replace(source, target)
            finally:
                for source in staged.values():
                    source.unlink(missing_ok=True)
        return mvr.draft(mvr.read(content)), rig_revision(content)
