import copy
import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autolux.lighting import mvr
from autolux.lighting.fixture_setup import FixtureStore, channel_occupancy
from cli.starfire_assets import rig_bytes, rig_definition

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / 'rig/library'
ROOM = ROOT / 'autolux/tests/data/room.mvr'
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
    path.write_bytes(ROOM.read_bytes())
    return path


def store_for(path, validate):
    """A store whose repo rig is a scratch copy of the room; `validate` sees each staged rig."""
    library = path.parent / 'rig/library'
    shutil.copytree(LIBRARY, library)
    shutil.copy(ROOM, library.parent / 'room.mvr')
    shutil.copy(ROOM.with_suffix('.rig.pb'), library.parent / 'room.rig.pb')

    def export(staged):
        validate(staged)
        return rig_bytes(staged)

    return FixtureStore(path, library, export, library.parent / 'room.mvr')


def written(store) -> list[bytes]:
    return [
        path.read_bytes()
        for path in (store.rig_path, store.repo_rig, store.repo_rig.with_suffix('.rig.pb'))
    ]


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
    assert list(rig_file.parent.rglob('.rig-*')) == []


def test_save_writes_the_store_the_repo_rig_and_its_rig_definition_together(rig_file):

    def validate(_staged):
        # Nothing is replaced until Starfire has accepted the staged rig.
        assert written(store) == before

    store = store_for(rig_file, validate)
    before = written(store)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    _, saved_revision = store.save(draft, revision)
    stored, repo, exported = written(store)
    assert stored == repo != before[0]
    assert saved_revision == hashlib.sha256(stored).hexdigest()
    assert exported == rig_definition(store.repo_rig).SerializeToString() != before[2]
    assert list(rig_file.parent.rglob('.rig-*')) == []


def test_save_warns_when_the_tracked_rig_differs_from_the_store(rig_file):
    store = store_for(rig_file, lambda _staged: None)
    assert not store.repo_rig_diverged()
    store.repo_rig.write_bytes(store.repo_rig.read_bytes() + b' ')
    assert store.repo_rig_diverged()
    assert not FixtureStore(rig_file, store.library_path, rig_bytes).repo_rig_diverged()


def test_a_store_without_a_repo_rig_saves_only_itself(rig_file):
    store = store_for(rig_file, lambda _staged: None)
    repo = written(store)[1:]
    alone = FixtureStore(rig_file, store.library_path, rig_bytes)
    draft, revision = alone.load()
    address_node(draft, 'wash_right')['text'] = '200'
    alone.save(draft, revision)
    assert written(store)[1:] == repo
    assert rig_file.read_bytes() != ROOM.read_bytes()


@pytest.mark.parametrize('failing', [2, 3])
def test_a_failed_repo_replace_leaves_the_store_the_show_reads_unchanged(
    rig_file, monkeypatch, failing
):
    import os

    before = rig_file.read_bytes()
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    calls = []

    def replace(source, target):
        calls.append(target)
        if len(calls) == failing:
            raise PermissionError('file locked')
        os.replace(source, target)

    monkeypatch.setattr('autolux.lighting.fixture_setup.os.replace', replace)
    with pytest.raises(PermissionError, match='file locked'):
        store.save(draft, revision)
    # The store is the last replace, so it never moved before the failing one.
    assert store.rig_path not in calls[:-1]
    assert rig_file.read_bytes() == before
    assert list(rig_file.parent.rglob('.rig-*')) == []


def test_a_save_on_a_stale_revision_writes_none_of_the_three_files(rig_file):
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    address_node(draft, 'wash_right')['text'] = '200'
    store.save(copy.deepcopy(draft), revision)
    saved = written(store)
    address_node(draft, 'wash_right')['text'] = '300'
    with pytest.raises(ValueError, match='Reload'):
        store.save(draft, revision)
    assert written(store) == saved
    assert list(rig_file.parent.rglob('.rig-*')) == []


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
    assert list(rig_file.parent.rglob('.rig-*')) == []


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
    assert list(rig_file.parent.rglob('.rig-*')) == []


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
    assert list(rig_file.parent.rglob('.rig-*')) == []


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


def test_class_order_follows_positions_not_document_order_after_moving_a_fixture(rig_file):
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    children = next(node for node in mvr._nodes(draft['scene']) if node['tag'] == 'ChildList')
    spots = [node for node in children['children'] if node['attributes']['name'].startswith('spot')]
    first, second = (children['children'].index(spot) for spot in spots)
    children['children'][first], children['children'][second] = spots[1], spots[0]
    store.save(draft, revision)
    fixtures = mvr.patched_fixtures(mvr.read(rig_file.read_bytes()))
    assert mvr.groups(fixtures)[0]['movers'] == ['spot_left', 'spot_right']


def test_a_browser_plan_drag_moves_the_stage_area_and_group_order_on_save(rig_file):
    import json

    from proto.starfire import rig_pb2 as rig_wire

    # The Matrix fixture_draft.test.cjs checks the browser writes for this drag.
    drag = json.loads((ROOT / 'autolux/tests/data/plan_drag.json').read_text(encoding='utf-8'))
    store = store_for(rig_file, lambda _staged: None)
    draft, revision = store.load()
    spot = fixture_node(draft, drag['fixture'])
    next(node for node in spot['children'] if node['tag'] == 'Matrix')['text'] = drag['matrix']
    store.save(draft, revision)
    areas = {
        fixture.fixture_id: rig_wire.StageArea.Name(fixture.area)
        for fixture in rig_definition(rig_file).fixtures
    }
    # From (-300, 0), on a cut and so up centre, to x 600: the right third, still upstage.
    assert areas['spot_left'] == 'STAGE_AREA_UP_RIGHT'
    fixtures = mvr.patched_fixtures(mvr.read(rig_file.read_bytes()))
    assert mvr.groups(fixtures)[0]['movers'] == ['spot_right', 'spot_left']


def test_config_names_the_library_folder_it_lists():
    from autolux.simulate.fixture_panel import library_folder

    assert library_folder(LIBRARY) == 'rig/library'
    listed = FixtureStore(ROOM, LIBRARY, lambda _: None).library()
    assert set(listed) == {path.name for path in LIBRARY.glob('*.gdtf')}


KINTA = 'Chauvet DJ@Mini Kinta IRC@1.0.gdtf'
STALE_KINTA = 'CHAUVET DJ@chauvet_mini_kinta_irc_4ch.gdtf'


def _stale_store(path: Path) -> bytes:
    """The room as an old store holds it: the Kinta under its old name, with a stale profile."""
    scene = mvr.read(ROOM.read_bytes())
    stale = mvr.gdtf_description(scene, KINTA).replace(
        'Name="Mini Kinta IRC"', 'Name="chauvet_mini_kinta_irc_4ch"', 1
    )
    stale = stale.replace('<Revisions>', '<!-- before the wheel colours -->\n    <Revisions>', 1)
    for node in scene.root.iter('GDTFSpec'):
        if node.text == KINTA:
            node.text = STALE_KINTA
    del scene.entries[KINTA]
    mvr.embed_gdtf(scene, STALE_KINTA, stale)
    content = mvr.write(scene)
    path.write_bytes(content)
    return content


def _patch(content: bytes) -> list[dict]:
    return [
        {key: value for key, value in fixture.items() if key != 'gdtf_spec'}
        for fixture in mvr.patched_fixtures(mvr.read(content))
    ]


def test_a_start_refreshes_stale_store_profiles_from_the_library_and_keeps_the_patch(
    tmp_path, caplog
):
    from autolux.lighting.fixture_setup import refresh_profiles

    store = tmp_path / 'rig.mvr'
    before = _stale_store(store)
    with caplog.at_level('INFO'):
        assert refresh_profiles(store, LIBRARY) == [STALE_KINTA]
    after = store.read_bytes()
    scene = mvr.read(after)
    assert scene.entries[KINTA][1] == (LIBRARY / KINTA).read_bytes()
    assert STALE_KINTA not in scene.entries
    assert _patch(after) == _patch(before)
    kinta = next(f for f in mvr.patched_fixtures(scene) if f['name'] == 'kinta')
    assert kinta['gdtf_spec'] == KINTA
    assert f'refreshed {STALE_KINTA} from the library as {KINTA}' in caplog.text
    # The other profiles already match the library, so nothing else changes and a rerun is a no-op.
    assert after == ROOM.read_bytes()
    assert refresh_profiles(store, LIBRARY) == []
    assert store.read_bytes() == after


def test_a_store_profile_missing_from_the_library_is_kept_with_a_warning(tmp_path, caplog):
    from autolux.lighting.fixture_setup import refresh_profiles

    library = tmp_path / 'library'
    library.mkdir()
    for archive in LIBRARY.glob('*.gdtf'):
        if archive.name != KINTA:
            (library / archive.name).write_bytes(archive.read_bytes())
    store = tmp_path / 'rig.mvr'
    before = _stale_store(store)
    with caplog.at_level('WARNING'):
        assert refresh_profiles(store, library) == []
    assert store.read_bytes() == before
    assert f'{STALE_KINTA} (FixtureTypeID 25DB1C80-' in caplog.text
    assert 'keeping the stored profile' in caplog.text
