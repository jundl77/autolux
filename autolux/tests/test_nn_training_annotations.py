"""Training consumes manual corrections without changing corpus admission or splits."""

import json

import numpy as np
import pytest
from nn import downbeat_train, train
from nn.dataset import WindowDataset, make_splits
from nn.downbeat_dataset import DownbeatWindowDataset
from raveform_fetch_annotations import beat_csv_path, load_all_tracks

from autolux.lib.label_space import LABEL_INDEX
from autolux.tests.test_nn_dataset import fake_corpus
from autolux.tests.test_nn_downbeat_dataset import grid_rows, write_grid


@pytest.fixture
def annotated_corpus(tmp_path):
    tracks = [
        ('eval', 'eval', 'Heldout Artist - Benchmark'),
        ('related', 'related', 'Heldout Artist - Other'),
        ('corrected', 'corrected', 'Training Artist - Corrected'),
        ('hand-new', 'hand-new', 'Manual Artist - New'),
        ('validation', 'validation', 'Validation Artist - Track'),
        ('test', 'test', 'Test Artist - Track'),
    ]
    corpus, eval_set = fake_corpus(tmp_path, tracks)
    published = corpus / 'annotations/segments.json'
    published.write_text(
        json.dumps(
            [track for track in json.loads(published.read_text()) if track['id'] != 'hand-new']
        ),
        encoding='utf-8',
    )
    corrections = {'corrected': 'breakdown', 'hand-new': 'buildup', 'eval': 'drop'}
    for identifier, label in {**corrections, 'unadmitted': 'drop'}.items():
        title = next((title for _, key, title in tracks if key == identifier), 'Unknown - Draft')
        (corpus / 'annotations' / f'{identifier}.hand.json').write_text(
            json.dumps(
                {
                    'id': identifier,
                    'title': title,
                    'duration': 150.0,
                    'sections': [{'start': 0.0, 'end': 150.0, 'name': label}],
                }
            ),
            encoding='utf-8',
        )
    for track in load_all_tracks(corpus):
        write_grid(beat_csv_path(corpus, track), grid_rows())
    frozen = {'train': ['corrected', 'hand-new'], 'val': ['validation'], 'test': ['test']}
    (corpus / 'splits.json').write_text(json.dumps(frozen), encoding='utf-8')
    return corpus, eval_set, frozen


def assert_corrected_targets(dataset):
    expected = {'corrected': 'breakdown', 'hand-new': 'buildup'}
    assert set(dataset.track_ids()) == set(expected)
    for index in range(len(dataset)):
        # The shared section targets also validate annotations used by the downbeat dataset.
        _, labels, mask, _, _ = WindowDataset.window(dataset, index, 0)
        assert mask.all()
        assert np.all(labels == LABEL_INDEX[expected[dataset.track_id_of(index)]])


@pytest.mark.parametrize('dataset_type', [WindowDataset, DownbeatWindowDataset])
def test_dataset_defaults_accept_admitted_manual_tracks(annotated_corpus, dataset_type):
    corpus, _, frozen = annotated_corpus
    assert_corrected_targets(dataset_type(corpus, frozen['train']))


def test_training_class_weights_use_only_corrected_admitted_targets(annotated_corpus):
    corpus, _, frozen = annotated_corpus
    stats = train.load_target_stats(corpus, frozen['train'])
    supervised = np.flatnonzero(stats.class_counts)
    assert set(supervised) == {LABEL_INDEX['breakdown'], LABEL_INDEX['buildup']}
    assert (
        stats.class_counts[LABEL_INDEX['breakdown']] == stats.class_counts[LABEL_INDEX['buildup']]
    )


@pytest.mark.parametrize(
    'trainer,dataset_name', [(train, 'WindowDataset'), (downbeat_train, 'DownbeatWindowDataset')]
)
def test_training_prepares_corrected_labels_without_admitting_holdouts(
    annotated_corpus, monkeypatch, trainer, dataset_name
):
    corpus, eval_set, frozen = annotated_corpus
    before = (corpus / 'splits.json').read_bytes()
    datasets = []
    dataset_type = getattr(trainer, dataset_name)

    class Prepared(Exception):
        """End the test after real dataset construction, before model/GPU work."""

    def prepare_dataset(*args, **kwargs):
        dataset = dataset_type(*args, **kwargs)
        datasets.append(dataset)
        if len(datasets) == 2:
            raise Prepared
        return dataset

    monkeypatch.setattr(trainer, dataset_name, prepare_dataset)
    monkeypatch.setattr(trainer, 'seed_everything', lambda _: None)
    monkeypatch.setattr(
        trainer,
        'make_splits',
        lambda root, **kwargs: make_splits(root, eval_set_path=eval_set, **kwargs),
    )
    with pytest.raises(Prepared):
        trainer.train(
            {
                'data_dir': corpus,
                'run_name': 'annotation-check',
                'seed': 1337,
                'device': 'cpu',
                'smoke_tracks': 0,
                'lr': 0.001,
                'batch_size': 1,
            }
        )
    assert_corrected_targets(datasets[0])
    assert datasets[1].track_ids() == frozen['val']
    _, labels, mask, _, _ = WindowDataset.window(datasets[1], 0, 0)
    assert np.all(labels[mask] == LABEL_INDEX['intro'])
    assert (corpus / 'splits.json').read_bytes() == before
    splits = make_splits(corpus, eval_set_path=eval_set, write=False)
    assert {name: splits[name] for name in frozen} == frozen
    assert splits['excluded_eval_set'] == ['eval']
    assert splits['excluded_artist'] == ['related']
