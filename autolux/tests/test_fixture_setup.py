"""Fixture patch persistence must never publish unvalidated or stale edits."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from autolux.lighting.fixture_setup import (
    FixtureStore,
    channel_occupancy,
)

PROFILES = {'rgb': {'channels': 3}, 'moving_head': {'channels': 12}}


def fixture_row(name='left', address=1, universe=0, profile='rgb'):
    return {'id': name, 'profile': profile, 'universe': universe, 'address': address}


def test_patch_occupancy_retains_both_owners_of_conflicting_channels():
    rows = [fixture_row(address=1), fixture_row('right', address=3)]
    occupied, errors = channel_occupancy(rows, PROFILES, 0)
    assert len(occupied) == 512
    assert occupied[:5] == [[0], [0], [0, 1], [1], [1]]
    assert errors == ['right overlaps left in universe 1.']


def test_last_channel_can_be_allocated_without_clipping_or_wrapping():
    profiles = {'large': {'channels': 509}, **PROFILES}
    rows = [fixture_row(profile='large')]
    rows.append(fixture_row('last', 510))
    assert channel_occupancy(rows, profiles, 0)[0][-1] == [1]
    rows[-1]['address'] = 511
    occupied, errors = channel_occupancy(rows, profiles, 0)
    assert 'do not fit' in errors[0]
    assert occupied[509:] == [[], [], []]


@pytest.mark.parametrize(
    'field,value',
    [
        ('address', 0),
        ('address', 513),
        ('address', True),
        ('address', 1.5),
        ('universe', -1),
        ('universe', 32768),
        ('universe', False),
        ('profile', 'missing'),
        ('id', ''),
    ],
)
def test_invalid_patch_is_rejected_before_persistence(field, value):
    row = fixture_row()
    row[field] = value
    assert channel_occupancy([row], PROFILES, 0)[1]


def test_identical_addresses_in_distinct_universes_are_independent():
    rows = [fixture_row(), fixture_row('other', universe=1)]
    assert not channel_occupancy(rows, PROFILES, 0)[1]


@pytest.fixture
def patch_file(tmp_path):
    path = tmp_path / 'rig.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'fixtures': [
                    {'id': 'left', 'profile': 'generic_rgb', 'universe': 0, 'address': 1},
                    {'id': 'right', 'profile': 'generic_rgb', 'universe': 0, 'address': 4},
                ],
                'groups': {'wash': ['right', 'left']},
            }
        ),
        encoding='utf-8',
    )
    return path


def store_for(path, validate):
    return FixtureStore(path, path.parent / 'profiles.json', validate)


def test_save_validates_staged_content_before_atomic_replace(patch_file):
    before = patch_file.read_bytes()
    seen = []

    def validate(staged):
        assert staged != patch_file
        assert staged.parent == patch_file.parent
        assert patch_file.read_bytes() == before
        seen.append(json.loads(staged.read_bytes()))

    store = store_for(patch_file, validate)
    patch, revision = store.load()
    patch['fixtures'][0]['address'] = 20
    saved_revision = store.save(patch, revision)

    assert seen == [patch]
    assert store.load() == (patch, saved_revision)
    assert saved_revision == hashlib.sha256(patch_file.read_bytes()).hexdigest()
    assert saved_revision != revision
    assert json.loads(patch_file.read_bytes())['groups']['wash'] == ['right', 'left']
    assert list(patch_file.parent.glob('.rig-*.json')) == []


@pytest.mark.parametrize('error_type', [ValueError, TimeoutError, OSError])
def test_rejected_or_interrupted_validation_preserves_saved_patch(patch_file, error_type):
    before = patch_file.read_bytes()

    def reject(_staged):
        raise error_type('validation failed')

    store = store_for(patch_file, reject)
    patch, revision = store.load()
    patch['fixtures'][0]['address'] = 0
    with pytest.raises(error_type, match='validation failed'):
        store.save(patch, revision)
    assert patch_file.read_bytes() == before
    assert list(patch_file.parent.glob('.rig-*.json')) == []


def test_stale_browser_revision_is_rejected_before_validation(patch_file):
    calls = []
    store = store_for(patch_file, calls.append)
    patch, revision = store.load()
    patch['fixtures'][0]['address'] = 10
    store.save(patch, revision)
    saved = patch_file.read_bytes()
    patch['fixtures'][0]['address'] = 20
    with pytest.raises(ValueError, match='changed'):
        store.save(patch, revision)
    assert len(calls) == 1
    assert patch_file.read_bytes() == saved


def test_external_edit_during_validation_is_not_overwritten(patch_file):
    external = b'{"externally_changed": true}\n'

    def validate(_staged):
        patch_file.write_bytes(external)

    store = store_for(patch_file, validate)
    patch, revision = store.load()
    with pytest.raises(ValueError, match='during validation'):
        store.save(patch, revision)
    assert patch_file.read_bytes() == external
    assert list(patch_file.parent.glob('.rig-*.json')) == []


def test_failed_replace_preserves_saved_data_and_cleans_temporary_file(patch_file, monkeypatch):
    before = patch_file.read_bytes()
    store = store_for(patch_file, lambda _staged: None)
    patch, revision = store.load()

    def denied(_source, _destination):
        raise PermissionError('file locked')

    monkeypatch.setattr('autolux.lighting.fixture_setup.os.replace', denied)
    with pytest.raises(PermissionError, match='file locked'):
        store.save(patch, revision)
    assert patch_file.read_bytes() == before
    assert list(patch_file.parent.glob('.rig-*.json')) == []


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_values_never_reach_validator_or_disk(patch_file, value):
    before = patch_file.read_bytes()
    calls = []
    store = store_for(patch_file, calls.append)
    patch, revision = store.load()
    patch['fixtures'][0]['pan'] = {'min': value, 'max': 1}
    with pytest.raises(ValueError):
        store.save(patch, revision)
    assert not calls
    assert patch_file.read_bytes() == before


def test_concurrent_saves_from_one_revision_allow_only_one_winner(patch_file):
    validations = []
    store = store_for(patch_file, validations.append)
    patch, revision = store.load()

    def save(address):
        candidate = json.loads(json.dumps(patch))
        candidate['fixtures'][0]['address'] = address
        try:
            store.save(candidate, revision)
            return address
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [10, 20]))
    winners = [address for address in results if address is not None]
    assert len(winners) == 1
    assert len(validations) == 1
    assert store.load()[0]['fixtures'][0]['address'] == winners[0]


def test_renaming_fixture_preserves_group_order_used_by_autoloop_spread(patch_file):
    from autolux.lighting.fixture_setup import fixture_patch, fixture_rows

    store = store_for(patch_file, lambda _staged: None)
    original, revision = store.load()
    rows = fixture_rows(original)
    rows[1]['id'] = 'rear'
    renamed = fixture_patch(rows, original)
    store.save(renamed, revision)
    assert store.load()[0]['groups']['wash'] == ['rear', 'left']
    assert [fixture['id'] for fixture in renamed['fixtures']] == ['left', 'rear']
    assert all('_original_id' not in fixture for fixture in renamed['fixtures'])


def test_swapping_fixture_names_preserves_physical_group_order(patch_file):
    from autolux.lighting.fixture_setup import fixture_patch, fixture_rows

    store = store_for(patch_file, lambda _staged: None)
    original, _revision = store.load()
    rows = fixture_rows(original)
    rows[0]['id'], rows[1]['id'] = rows[1]['id'], rows[0]['id']
    assert fixture_patch(rows, original)['groups']['wash'] == ['left', 'right']


def test_copied_row_identity_cannot_silently_reorder_a_group(patch_file):
    from autolux.lighting.fixture_setup import fixture_patch, fixture_rows

    original, _revision = store_for(patch_file, lambda _staged: None).load()
    rows = fixture_rows(original)
    rows.append({**rows[1], 'id': 'clone'})
    with pytest.raises(ValueError, match='duplicated'):
        fixture_patch(rows, original)


def test_reusing_removed_fixture_name_does_not_duplicate_group_members(patch_file):
    from autolux.lighting.fixture_setup import fixture_patch, fixture_rows

    original, _revision = store_for(patch_file, lambda _staged: None).load()
    rows = fixture_rows(original)[:1]
    rows[0]['id'] = 'right'
    saved = fixture_patch(rows, original)
    assert saved['groups']['wash'] == ['right']
    assert len(saved['fixtures']) == 1
