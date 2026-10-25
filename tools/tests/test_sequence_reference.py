import pytest

from tools.check_sequence_reference import check_sequence, find_binary


@pytest.fixture(scope='module')
def binary():
    try:
        return find_binary()
    except FileNotFoundError:
        pytest.skip(
            'Build Starfire to compare actual rendered DMX against captured reference output'
        )


@pytest.mark.parametrize('bank', ['atmospheric', 'breakdown', 'buildup', 'drop'])
def test_rendered_sequence_matches_calibration_and_holdout_tempos(bank, binary, tmp_path):
    result = check_sequence(bank, binary, tmp_path)
    failures = [
        f'{bpm} BPM: {message}'
        for bpm, capture in result['captures'].items()
        for message in capture['failures']
    ]
    assert not failures, '\n'.join(failures)
