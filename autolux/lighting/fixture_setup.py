"""Validated, atomic fixture patch storage for the dashboard."""

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from autolux.lighting.gdtf import MAX_CATALOGUE_BYTES, profile_metadata, read_bounded

DMX_UNIVERSE_SIZE = 512
MAX_ARTNET_UNIVERSE = 32767


def channel_occupancy(
    rows: list[dict], profiles: dict, universe: int
) -> tuple[list[list[int]], list[str]]:
    """Map each channel to its fixture rows, retaining overlaps for the patch view."""
    occupied = [[] for _ in range(DMX_UNIVERSE_SIZE)]
    errors = []
    names = set()
    addresses = {}
    for index, row in enumerate(rows):
        name = row.get('id')
        label = name or f'Fixture {index + 1}'
        if not isinstance(name, str) or not name.strip() or name in names:
            errors.append(f'{label}: fixture names must be nonempty and unique.')
        else:
            names.add(name)
        target, start = row.get('universe'), row.get('address')
        profile = profiles.get(row.get('profile'), {})
        count = profile.get('channels')
        if type(target) is not int or not 0 <= target <= MAX_ARTNET_UNIVERSE:
            errors.append(f'{label}: select a valid universe.')
            continue
        if type(start) is not int or not 1 <= start <= DMX_UNIVERSE_SIZE:
            errors.append(f'{label}: DMX address must be an integer from 1 to 512.')
            continue
        if type(count) is not int or not 1 <= count <= DMX_UNIVERSE_SIZE:
            errors.append(f'{label}: select a valid fixture profile.')
            continue
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
            errors.append(f'{label} overlaps {rows[other]["id"]} in universe {target + 1}.')
    return occupied, errors


def next_free_address(rows: list[dict], profiles: dict, universe: int, channel_count: int) -> int:
    """Find the first contiguous DMX address range in a valid patch."""
    if type(channel_count) is not int or not 1 <= channel_count <= DMX_UNIVERSE_SIZE:
        raise ValueError('Fixture channel count must be within 1..512.')
    if type(universe) is not int or not 0 <= universe <= MAX_ARTNET_UNIVERSE:
        raise ValueError('Select a valid universe.')
    occupied, errors = channel_occupancy(rows, profiles, universe)
    if errors:
        raise ValueError(errors[0])
    free = 0
    for index, owners in enumerate(occupied):
        free = 0 if owners else free + 1
        if free == channel_count:
            return index - channel_count + 2
    raise ValueError(
        f'No room for {channel_count} consecutive channels in universe {universe + 1}.'
    )


def repatch_dmx_fixture(
    rows: list[dict], profiles: dict, fixture_id: str, artnet_universe: int, dmx_start_address: int
) -> list[dict]:
    """Return a valid draft with one fixture moved; universes are zero-based, addresses one-based."""
    matches = [index for index, row in enumerate(rows) if row['id'] == fixture_id]
    if len(matches) != 1:
        raise ValueError('The fixture changed. Select it again before moving it.')
    moved = [dict(row) for row in rows]
    moved[matches[0]].update(universe=artnet_universe, address=dmx_start_address)
    _, errors = channel_occupancy(moved, profiles, artnet_universe)
    if errors:
        raise ValueError(errors[0])
    return moved


class FixtureStore:
    def __init__(self, patch_path: Path, profiles_path: Path, validate: Callable[[Path], None]):
        self.patch_path = Path(patch_path)
        self.profiles_path = Path(profiles_path)
        self._validate = validate
        self._lock = threading.Lock()

    def profiles(self) -> dict:
        profiles = json.loads(read_bounded(self.profiles_path, MAX_CATALOGUE_BYTES))['profiles']
        return {
            name: profile_metadata(profile['gdtf_xml'], profile['dmx_mode'])
            for name, profile in profiles.items()
        }

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


def fixture_rows(patch):
    groups = patch.get('groups', {})
    rows = []
    for fixture in patch['fixtures']:
        row = {key: fixture[key] for key in ('id', 'profile', 'universe', 'address')}
        row['_original_id'] = fixture['id']
        row['groups'] = ', '.join(
            name for name, members in groups.items() if fixture['id'] in members
        )
        for axis in ('pan', 'tilt'):
            limits = fixture.get(axis, {})
            row.update(
                {
                    f'{axis}_min': limits.get('min', 0.0),
                    f'{axis}_max': limits.get('max', 1.0),
                    f'{axis}_inverted': limits.get('inverted', False),
                }
            )
        rows.append(row)
    return rows


def fixture_patch(rows, previous):
    fixtures, groups = [], {}
    renamed = {}
    for row in rows:
        fixture = {key: row.get(key) for key in ('id', 'profile', 'universe', 'address')}
        original = row.get('_original_id')
        if original:
            if original in renamed:
                raise ValueError('A fixture row was duplicated. Add a new fixture instead.')
            renamed[original] = fixture['id']
        for key in ('universe', 'address'):
            value = fixture[key]
            if isinstance(value, float) and value.is_integer():
                fixture[key] = int(value)
        for axis in ('pan', 'tilt'):
            fixture[axis] = {
                'min': row.get(f'{axis}_min'),
                'max': row.get(f'{axis}_max'),
                'inverted': row.get(f'{axis}_inverted'),
            }
        fixtures.append(fixture)
        membership = row.get('groups') or ''
        if not isinstance(membership, str):
            raise TypeError('Group membership must be comma-separated names.')
        for name in membership.split(','):
            if name := name.strip():
                groups.setdefault(name, []).append(fixture['id'])
    for name, members in groups.items():
        prior = [
            renamed[member]
            for member in previous.get('groups', {}).get(name, [])
            if member in renamed
        ]
        groups[name] = [member for member in prior if member in members] + [
            member for member in members if member not in prior
        ]
    return {'schema_version': previous['schema_version'], 'fixtures': fixtures, 'groups': groups}
