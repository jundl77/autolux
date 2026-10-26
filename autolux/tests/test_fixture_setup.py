import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autolux.lighting import mvr
from autolux.lighting.fixture_setup import FixtureStore, channel_occupancy

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / 'rig/library'
MODES = {'rgb.gdtf': {'3ch': {'channels': 3}}, 'mover.gdtf': {'12ch': {'channels': 12}}}


def fixture(name='left', address=1, universe=0, spec='rgb.gdtf', mode='3ch', breaks=(0,)):
    return {
        'name': name,
        'gdtf_spec': spec,
        'gdtf_mode': mode,
        'universe': universe,
        'address': address,
        'addresses': [(index, universe, address) for index in breaks],
        'matrix': list(mvr.IDENTITY_MATRIX),
    }


def test_patch_occupancy_retains_both_owners_of_conflicting_channels():
    occupied, errors = channel_occupancy([fixture(), fixture('right', 3)], MODES, 0)
    assert len(occupied) == 512
    assert occupied[:5] == [[0], [0], [0, 1], [1], [1]]
    assert errors == ['right overlaps left in universe 1.']


def test_last_channel_can_be_allocated_without_clipping_or_wrapping():
    modes = {**MODES, 'large.gdtf': {'big': {'channels': 509}}}
    rows = [fixture(spec='large.gdtf', mode='big'), fixture('last', 510)]
    assert channel_occupancy(rows, modes, 0)[0][-1] == [1]
    rows[-1] = fixture('last', 511)
    occupied, errors = channel_occupancy(rows, modes, 0)
    assert 'do not fit' in errors[0]
    assert occupied[509:] == [[], [], []]


@pytest.mark.parametrize(
    'changes,message',
    [
        ({'gdtf_mode': '4ch'}, "GDTF mode '4ch' is not in rgb.gdtf"),
        ({'name': ''}, 'unique, short fixture name'),
        ({'addresses': [(1, 0, 1)]}, 'one Address on break 0'),
        ({'addresses': [(0, 0, 1), (1, 0, 9)]}, 'one Address on break 0'),
        ({'matrix': mvr.compose_matrix([0, 0, 0], [0, 61, 0])}, 'exceeds +/-60 degrees'),
    ],
)
def test_patches_starfire_cannot_render_are_rejected(changes, message):
    errors = channel_occupancy([{**fixture(), **changes}], MODES, 0)[1]
    assert any(message in error for error in errors), errors


def test_identical_addresses_in_distinct_universes_are_independent():
    assert not channel_occupancy([fixture(), fixture('other', universe=1)], MODES, 0)[1]


@pytest.fixture
def rig_file(tmp_path):
    path = tmp_path / 'rig.mvr'
    path.write_bytes((ROOT / 'rig/room.mvr').read_bytes())
    return path


def store_for(path, validate):
    return FixtureStore(path, LIBRARY, validate)


def fixture_node(draft, name):
    return next(
        node
        for node in mvr._nodes(draft['scene'])
        if node['tag'] == 'Fixture' and node['attributes'].get('name') == name
    )


def address_node(draft, name):
    fixture_entry = fixture_node(draft, name)
    addresses = next(child for child in fixture_entry['children'] if child['tag'] == 'Addresses')
    return addresses['children'][0]


def test_unchanged_draft_saves_byte_identical_and_reloads_identical(rig_file):
    before = rig_file.read_bytes()
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    saved, saved_revision = store.save(draft, revision)
    assert rig_file.read_bytes() == before
    assert saved_revision == revision
    assert mvr._without_keys(saved) == mvr._without_keys(draft)


def test_save_validates_staged_content_before_atomic_replace(rig_file):
    before = rig_file.read_bytes()
    seen = []

    def validate(staged):
        assert staged != rig_file
        assert staged.parent == rig_file.parent
        assert rig_file.read_bytes() == before
        seen.append(mvr.patched_fixtures(mvr.read(staged.read_bytes())))

    store = store_for(rig_file, validate)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    reloaded, saved_revision = store.save(draft, revision)
    assert [f['address'] for f in seen[0] if f['name'] == 'wash_right'] == [200]
    assert store.load() == (reloaded, saved_revision)
    assert saved_revision == hashlib.sha256(rig_file.read_bytes()).hexdigest()
    assert saved_revision != revision
    assert address_node(reloaded, 'wash_right')['text'] == '200'
    assert list(rig_file.parent.glob('.rig-*.mvr')) == []


@pytest.mark.parametrize('error_type', [ValueError, TimeoutError, OSError])
def test_rejected_or_interrupted_validation_preserves_saved_rig(rig_file, error_type):
    before = rig_file.read_bytes()

    def reject(_staged):
        raise error_type('validation failed')

    store = store_for(rig_file, reject)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    with pytest.raises(error_type, match='validation failed'):
        store.save(draft, revision)
    assert rig_file.read_bytes() == before
    assert list(rig_file.parent.glob('.rig-*.mvr')) == []


@pytest.mark.parametrize(
    'edit,message',
    [
        (lambda draft: address_node(draft, 'spot_left').update(text='0'), 'absolute address'),
        (lambda draft: address_node(draft, 'spot_left').update(text='14'), 'overlaps'),
        (
            lambda draft: fixture_node(draft, 'spot_left')['attributes'].update(uuid='nope'),
            'non-nil UUID',
        ),
        (
            lambda draft: fixture_node(draft, 'spot_left')['children'].append(
                {'tag': 'Sparkle', 'attributes': {}, 'text': '', 'children': []}
            ),
            'Sparkle is not allowed',
        ),
        (lambda draft: draft['scene'].update(tag='Scene'), 'GeneralSceneDescription'),
    ],
)
def test_nonconforming_drafts_never_reach_validator_or_disk(rig_file, edit, message):
    before = rig_file.read_bytes()
    calls = []
    store = store_for(rig_file, calls.append)
    draft, revision = store.load()
    edit(draft)
    with pytest.raises(ValueError, match=message):
        store.save(draft, revision)
    assert not calls
    assert rig_file.read_bytes() == before


def test_stale_browser_revision_is_rejected_before_validation(rig_file):
    calls = []
    store = store_for(rig_file, calls.append)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    store.save(copy.deepcopy(draft), revision)
    saved = rig_file.read_bytes()
    address_node(draft, 'wash_right')['text'] = '300'
    with pytest.raises(ValueError, match='changed'):
        store.save(draft, revision)
    assert len(calls) == 1
    assert rig_file.read_bytes() == saved


def test_external_edit_during_validation_is_not_overwritten(rig_file):
    external = b'externally changed'

    def validate(_staged):
        rig_file.write_bytes(external)

    store = store_for(rig_file, validate)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    with pytest.raises(ValueError, match='during validation'):
        store.save(draft, revision)
    assert rig_file.read_bytes() == external
    assert list(rig_file.parent.glob('.rig-*.mvr')) == []


def test_failed_replace_preserves_saved_data_and_cleans_temporary_file(rig_file, monkeypatch):
    before = rig_file.read_bytes()
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'

    def denied(_source, _destination):
        raise PermissionError('file locked')

    monkeypatch.setattr('autolux.lighting.fixture_setup.os.replace', denied)
    with pytest.raises(PermissionError, match='file locked'):
        store.save(draft, revision)
    assert rig_file.read_bytes() == before
    assert list(rig_file.parent.glob('.rig-*.mvr')) == []


def test_concurrent_saves_from_one_revision_allow_only_one_winner(rig_file):
    validations = []
    store = store_for(rig_file, validations.append)
    draft, revision = store.load()

    def save(address):
        candidate = copy.deepcopy(draft)
        address_node(candidate, 'wash_right')['text'] = str(address)
        try:
            store.save(candidate, revision)
            return address
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [200, 300]))
    winners = [address for address in results if address is not None]
    assert len(winners) == 1
    assert len(validations) == 1
    assert address_node(store.load()[0], 'wash_right')['text'] == str(winners[0])


def test_class_order_follows_document_order_after_moving_a_fixture(rig_file):
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    children = next(node for node in mvr._nodes(draft['scene']) if node['tag'] == 'ChildList')
    spots = [node for node in children['children'] if node['attributes']['name'].startswith('spot')]
    first, second = (children['children'].index(spot) for spot in spots)
    children['children'][first], children['children'][second] = spots[1], spots[0]
    store.save(draft, revision)
    fixtures = mvr.patched_fixtures(mvr.read(rig_file.read_bytes()))
    assert mvr.groups(fixtures)['movers'] == ['spot_right', 'spot_left']


def test_config_names_the_library_folder_it_lists():
    from autolux.simulate.fixture_panel import library_folder

    assert library_folder(LIBRARY) == 'rig/library'
    listed = FixtureStore(ROOT / 'rig/room.mvr', LIBRARY, lambda _: None).library()
    assert set(listed) == {path.name for path in LIBRARY.glob('*.gdtf')}
