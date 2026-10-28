import base64
import json
import shutil
from pathlib import Path

import pytest

from autolux.lighting.fixture_setup import FixtureStore
from autolux.lighting.starfire_replay import (
    PREVIEW_KIND,
    bar_starts,
    is_preview,
    load_preview,
    preview_overview,
    report_to_replay,
)

GENERIC_RIG = Path(__file__).parent / 'data' / 'generic_rig'
RATE_HZ = 10


def preview_text(frames=None) -> str:
    """Two cells in one group at 120 bpm, 4/4: dark, then red, then the second cell strobing."""
    header = {
        'kind': PREVIEW_KIND,
        'frame_rate_hz': RATE_HZ,
        'duration_seconds': 2.0,
        'frames': 21,
        'beats_per_bar': 4,
        'cells': [
            {'id': 'a', 'fixture': 'a', 'x': -1.0, 'y': 0.0, 'z': 3.0},
            {'id': 'b', 'fixture': 'b', 'x': 1.0, 'y': 0.0, 'z': 3.0},
        ],
        'groups': {'wash': [0, 1], 'empty': []},
    }
    if frames is None:
        frames = []
        for index in range(21):
            if index < 5:
                bank, cells = None, [[0.0, 1.0, 1.0, 1.0, 0.0]] * 2
            elif index < 15:
                bank, cells = 'drop', [[1.0, 1.0, 0.0, 0.0, 0.0], [0.5, 1.0, 0.0, 0.0, 0.0]]
            else:
                bank, cells = (
                    'drop',
                    [[1.0, 1.0, 0.0, 0.0, 0.0], [1.0, 1.0, 0.0, 0.0, 8.0, [0, 0, -1]]],
                )
            loop = 'chase' if bank else None
            frames.append(
                {
                    't': index / RATE_HZ,
                    'beat': index / 5,
                    'bank': bank,
                    'autoloop': loop,
                    'c': cells,
                }
            )
    return '\n'.join(json.dumps(line) for line in [header, *frames]) + '\n'


def test_the_loader_reads_the_header_and_every_frame():
    preview = load_preview(preview_text())
    assert preview.rate_hz == RATE_HZ
    assert [cell['fixture'] for cell in preview.header['cells']] == ['a', 'b']
    assert len(preview.frames) == 21
    assert preview.frames[20]['c'][1] == [1.0, 1.0, 0.0, 0.0, 8.0, [0, 0, -1]]
    assert is_preview(preview_text())
    assert not is_preview(json.dumps({'duration_sec': 1.0, 'beats': []}, indent=2))


@pytest.mark.parametrize(
    ('frames', 'reason'),
    [
        ([], 'has 0 frames, its header says 21'),
        ([{'t': 0, 'beat': 0, 'c': [[0, 0, 0, 0, 0]]}] * 21, 'one entry per header cell'),
        ([{'t': 0, 'beat': 0, 'c': [[0, 0, 0, 0]] * 2}] * 21, 'intensity, RGB and strobe'),
    ],
)
def test_the_loader_refuses_frames_that_do_not_match_the_header(frames, reason):
    with pytest.raises(ValueError, match=reason):
        load_preview(preview_text(frames))


def test_the_loader_refuses_group_members_outside_the_header_cells():
    with pytest.raises(ValueError, match='header cell indices'):
        load_preview(preview_text().replace('"empty": []', '"empty": [2]'))


def test_bars_start_where_the_renderers_grid_enters_them():
    # Five frames a beat, four beats a bar: a bar every 20 frames.
    assert bar_starts(load_preview(preview_text())) == [(1, 0), (2, 20)]


def test_the_overview_shows_level_strobe_and_section_colour_over_the_track():
    overview = preview_overview(load_preview(preview_text()), columns=3)
    # 21 frames in slices of 7: five dark then two at 0.75; all 0.75; one at 0.75 then six at 1.
    assert overview['columns'] == 3
    assert overview['groups'] == {'wash': [0.214, 0.75, 0.964]}
    assert overview['strobe'] == [0.0, 0.0, 0.857]
    assert overview['strobe_seconds'] == 0.6
    assert overview['group_strobe_seconds'] == {'wash': 0.6}
    assert overview['sections'] == [
        {'base': None, 'start': 0, 'end': 5, 'rgb': None},
        {'base': 'drop/chase', 'start': 5, 'end': 21, 'rgb': [1.0, 0.0, 0.0]},
    ]


def test_report_conversion_is_deterministic():
    report = {
        'duration_sec': 20.0,
        'beats': [{'t': 1.0 + index / 2, 'bpm': 120.0, 'rms': 0.1} for index in range(16)],
        'intents': [
            {'t': 15.0, 'song_t': 1.0, 'intent': 'buildup'},
            {'t': 18.0, 'song_t': 5.0, 'intent': 'drop'},
        ],
        'metrics': {'look_ahead_sec': 14.0},
    }
    assert json.dumps(report_to_replay(report)) == json.dumps(report_to_replay(report))


def test_loading_a_preview_file_sets_up_the_scrubber_and_serves_its_frames(tmp_path):
    from autolux.simulate.visualizer_app import build_app
    from autolux.tests.test_show_control import _Recorder

    store_path = tmp_path / 'store.mvr'
    shutil.copy(GENERIC_RIG / 'generic.mvr', store_path)
    app = build_app(_Recorder(), fixture_store=FixtureStore(store_path, GENERIC_RIG, bytes))
    output = next(key for key in app.callback_map if '.preview-source.data.' in key)
    text = preview_text()
    contents = 'data:application/octet-stream;base64,' + base64.b64encode(text.encode()).decode()
    callback = app.callback_map[output]
    client = app.server.test_client()
    reply = client.post(
        '/_dash-update-component',
        json={
            'output': output,
            'outputs': [
                {'id': part.split('.')[0], 'property': part.split('.')[1]}
                for part in output.strip('.').split('...')
            ],
            'inputs': [{**callback['inputs'][0], 'value': contents}],
            'state': [{**callback['state'][0], 'value': 'set.jsonl'}],
            'changedPropIds': ['preview-upload.contents'],
        },
    ).get_json()['response']
    source = reply['preview-source']['data']
    assert reply['preview-frame'] == {'max': 20, 'marks': {'0': '1', '20': '2'}, 'disabled': False}
    assert reply['preview-play'] == {'disabled': False}
    assert reply['preview-clock'] == {'interval': 100}
    assert reply['preview-status']['children'] == 'set.jsonl: 2 s, 2 bars, 21 frames at 10 Hz'
    assert source['overview']['strobe_seconds'] == 0.6
    served = client.get(source['url'])
    assert served.mimetype == 'application/x-ndjson'
    assert served.get_data(as_text=True) == text
