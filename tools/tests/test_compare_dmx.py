import gzip
import json
import struct

import numpy as np
import pytest

from tools.check_sequence_reference import evaluate_metrics
from tools.compare_dmx import Capture, compare, fit_shared_channel_offset_beats, read_capture


def capture(beats, channels):
    dmx = np.zeros((len(beats), 512), dtype=np.uint8)
    for address, values in channels.items():
        dmx[:, address - 1] = values
    return Capture(np.array(beats, dtype=float), dmx)


def test_coarse_fine_wrap_is_one_native_unit_not_a_large_channel_error():
    reference = capture([0, 1], {1: [0, 0], 2: [255, 255]})
    candidate = capture([0, 1], {1: [1, 1], 2: [0, 0]})
    report = compare(
        reference, candidate, {'pan': {'channels': [[1, 2]]}}, start_beat=0, end_beat=1
    )
    assert report['groups']['pan']['mae_units'] == 1
    assert report['groups']['pan']['maximum_value'] == 65535


def test_colour_slot_equivalence_keeps_raw_byte_error_and_detects_wrong_colour():
    reference = capture([0, 1], {6: [111, 111]})
    same_colour = capture([0, 1], {6: [96, 96]})
    wrong_colour = capture([0, 1], {6: [160, 160]})
    groups = {'colour': {'channels': [[6]], 'equivalent_ranges': [[96, 127], [160, 191]]}}
    same = compare(reference, same_colour, groups, start_beat=0, end_beat=1)['groups']['colour']
    assert same['mae_units'] == 15
    assert same['exact_percent'] == 0
    assert same['semantic_exact_percent'] == 100
    wrong = compare(reference, wrong_colour, groups, start_beat=0, end_beat=1)['groups']['colour']
    assert wrong['semantic_exact_percent'] == 0


def test_uncovered_strip_is_reported_instead_of_diluted_by_inactive_channels():
    reference = capture([0, 1], {1: [128, 128], 97: [255, 255]})
    candidate = capture([0, 1], {1: [128, 128]})
    report = compare(
        reference, candidate, {'dimmer': {'channels': [[1]]}}, start_beat=0, end_beat=1
    )
    assert report['active_uncovered_channels'] == [97]
    assert report['groups']['dimmer']['exact_percent'] == 100


def test_discrete_channels_are_held_without_inventing_intermediate_slots():
    reference = capture([0, 0.5, 1], {6: [96, 160, 160]})
    candidate = capture([0, 0.75, 1], {6: [96, 160, 160]})
    group = {'colour': {'channels': [[6]]}}
    result = compare(reference, candidate, group, start_beat=0, end_beat=1, step_beats=0.01)[
        'groups'
    ]['colour']
    assert result['maximum_error_units'] == 64
    assert result['different_duration_beats'] == pytest.approx(0.25)
    assert result['mae_units'] == pytest.approx(16)


def test_alignment_is_shared_and_fixed_for_holdout():
    beats = np.arange(0, 10, 0.01)
    reference = capture(beats, {1: ((beats % 2) > 1) * 255, 2: ((beats % 2) > 1) * 255})
    # Two properties demand opposing offsets. One shared shift cannot fix both.
    candidate = capture(
        beats, {1: (((beats - 0.2) % 2) > 1) * 255, 2: (((beats + 0.2) % 2) > 1) * 255}
    )
    groups = {'left': {'channels': [[1]]}, 'right': {'channels': [[2]]}}
    fit = fit_shared_channel_offset_beats(
        reference, candidate, groups, start_beat=1, end_beat=4, radius_beats=0.3, step_beats=0.01
    )
    heldout = compare(
        reference, candidate, groups, start_beat=5, end_beat=8, offset_beats=fit['offset_beats']
    )
    assert sum(group['mae_units'] for group in heldout['groups'].values()) > 60
    assert heldout['candidate_offset_beats'] == fit['offset_beats']


def test_alignment_recovers_one_known_shift_without_changing_holdout():
    beats = np.arange(0, 10, 0.01)
    reference = capture(beats, {1: ((beats % 2) > 1) * 255})
    candidate = Capture(reference.beats + 0.1, reference.channels)
    groups = {'dimmer': {'channels': [[1]]}}
    fit = fit_shared_channel_offset_beats(
        reference, candidate, groups, start_beat=1, end_beat=4, radius_beats=0.2, step_beats=0.01
    )
    assert fit['offset_beats'] == pytest.approx(0.1)
    heldout = compare(
        reference, candidate, groups, start_beat=5, end_beat=8, offset_beats=fit['offset_beats']
    )
    assert heldout['groups']['dimmer']['mae_units'] < 1


def test_comparison_refuses_extrapolation_and_duplicate_channel_definitions():
    source = capture([0, 1], {1: [1, 1]})
    with pytest.raises(ValueError, match='outside'):
        compare(
            source,
            source,
            {'dimmer': {'channels': [[1]]}},
            start_beat=0,
            end_beat=1,
            offset_beats=-0.1,
        )
    with pytest.raises(ValueError, match='more than once'):
        compare(
            source,
            source,
            {'pan': {'channels': [[1, 2]]}, 'dimmer': {'channels': [[2]]}},
            start_beat=0,
            end_beat=1,
        )


def test_capture_parsers_agree_and_compressed_reference_preserves_every_channel(tmp_path):
    raw = bytes(range(256)) * 2
    records = [
        {'at_seconds': seconds, 'universe': 0, 'loop': 'drop', 'dmx': raw.hex()}
        for seconds in [1, 2]
    ]
    reference = tmp_path / 'frames.json.gz'
    with gzip.open(reference, 'wt', encoding='utf-8') as stream:
        json.dump(records, stream)
    native = tmp_path / 'render.dmx'
    encoded = struct.pack('<8sIQ', b'STARFIRE', 1, 2)
    for seconds in [1, 2]:
        encoded += struct.pack('<ddHH', seconds, seconds * 2, 1, 0) + raw
    native.write_bytes(encoded)
    a = read_capture(reference, bpm=120, origin_seconds=1, loop='drop')
    b = read_capture(native, bpm=120, origin_seconds=1)
    np.testing.assert_array_equal(a.beats, [0, 2])
    np.testing.assert_array_equal(a.channels, b.channels)
    np.testing.assert_array_equal(a.beats, b.beats)
    native.write_bytes(encoded[:-1])
    with pytest.raises(ValueError, match='Truncated'):
        read_capture(native, bpm=120)


def test_capture_parser_rejects_short_dmx_and_nonmonotonic_timestamps(tmp_path):
    source = tmp_path / 'frames.json'
    records = [{'at_seconds': seconds, 'universe': 0, 'dmx': '00' * 511} for seconds in [1, 2]]
    source.write_text(json.dumps(records))
    with pytest.raises(ValueError, match='exactly 512'):
        read_capture(source, bpm=120)
    for record in records:
        record['dmx'] = '00' * 512
        record['at_seconds'] = 1
    source.write_text(json.dumps(records))
    with pytest.raises(ValueError, match='strictly increasing'):
        read_capture(source, bpm=120)


def test_independent_cut_samples_can_exceed_self_maximum_without_a_wrong_waveform():
    def waveform(beats):
        return np.rint(np.where(beats < 2, 100, np.minimum(255, 50 * (beats - 2))))

    reference_beats = np.arange(0.04, 8.25, 0.1)
    candidate_beats = np.arange(0, 8.25, 0.1)
    reference = capture(reference_beats, {1: waveform(reference_beats)})
    candidate = capture(candidate_beats, {1: waveform(candidate_beats)})
    groups = {'fixture.dimmer': {'channels': [[1]]}}
    window = {'start_beat': 0.2, 'end_beat': 8}
    before = compare(reference, reference, groups, offset_beats=-0.1, **window)
    after = compare(reference, reference, groups, offset_beats=0.1, **window)
    metrics = ['mae_units', 'p95_error_units', 'p99_error_units', 'maximum_error_units']
    limits = {
        'fixture.dimmer': {
            key + '_limit': max(
                before['groups']['fixture.dimmer'][key], after['groups']['fixture.dimmer'][key]
            )
            + 1
            for key in metrics
        }
    }
    same_waveform = compare(reference, candidate, groups, **window)
    failures, diagnostics = evaluate_metrics(same_waveform, limits, ['dimmer'])
    assert not failures
    assert len(diagnostics) == 1 and 'maximum_error_units' in diagnostics[0]

    wrong_curve = capture(candidate_beats, {1: np.full(len(candidate_beats), 100)})
    wrong_timing = capture(candidate_beats, {1: waveform(candidate_beats - 0.4)})
    for wrong in [wrong_curve, wrong_timing]:
        failures, _ = evaluate_metrics(
            compare(reference, wrong, groups, **window), limits, ['dimmer']
        )
        assert any('mae_units' in failure for failure in failures)


def test_safe_control_maximum_cannot_be_downgraded_to_a_cut_diagnostic():
    reference = capture([0, 1], {7: [7, 7]})
    candidate = capture([0, 0.999, 1], {7: [7, 128, 128]})
    comparison = compare(
        reference,
        candidate,
        {'shutter': {'channels': [[7]]}},
        start_beat=0,
        end_beat=1,
        step_beats=0.0005,
    )
    failures, diagnostics = evaluate_metrics(
        comparison, {'shutter': {'maximum_error_units_limit': 0}}, ['dimmer', 'rgb', 'pan', 'tilt']
    )
    assert failures and not diagnostics
