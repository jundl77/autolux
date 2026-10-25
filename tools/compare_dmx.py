"""Compare offline DMX captures in musical time; never send lighting output.

Reference JSON contains at_seconds, universe and 512-byte hex dmx fields. Native
Starfire .dmx files and gzip-compressed JSON are also accepted. Channel groups use
one-based addresses; a two-address component is a coarse/fine 16-bit value.
"""

import argparse
import gzip
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DMX_CHANNEL_COUNT = 512
BYTE_MAX = 255
WORD_MAX = 65535
CAPTURE_MAGIC = b'STARFIRE'
CAPTURE_VERSION = 1
DEFAULT_SAMPLE_STEP_BEATS = 0.005
MAX_ALIGNMENT_STEPS = 2001
MAX_COMPARISON_SAMPLES = 1_000_000


@dataclass(frozen=True)
class Capture:
    beats: np.ndarray
    channels: np.ndarray

    def __post_init__(self):
        if len(self.beats) < 2 or self.channels.shape != (len(self.beats), DMX_CHANNEL_COUNT):
            raise ValueError('Capture needs at least two complete 512-channel frames')
        if not np.isfinite(self.beats).all() or (np.diff(self.beats) <= 0).any():
            raise ValueError('Capture times must be finite and strictly increasing')
        if self.channels.dtype != np.uint8:
            raise ValueError('Capture channels must be uint8')

    def held_at(self, beats):
        if beats[0] < self.beats[0] or beats[-1] > self.beats[-1]:
            raise ValueError('Comparison window extends outside a capture')
        indices = np.searchsorted(self.beats, beats, side='right') - 1
        return self.channels[indices]


def _read_json(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def _native_frames(path, universe):
    data = path.read_bytes()
    header = struct.Struct('<8sIQ')
    frame_header = struct.Struct('<ddH')
    if len(data) < header.size:
        raise ValueError('Truncated native capture header')
    magic, version, count = header.unpack_from(data)
    if magic != CAPTURE_MAGIC or version != CAPTURE_VERSION:
        raise ValueError('Unsupported native capture format')
    offset = header.size
    frames = []
    for _ in range(count):
        if len(data) - offset < frame_header.size:
            raise ValueError('Truncated native frame header')
        seconds, beat, universe_count = frame_header.unpack_from(data, offset)
        offset += frame_header.size
        if not math.isfinite(beat) or not math.isfinite(seconds):
            raise ValueError('Native capture has a nonfinite clock')
        seen = set()
        for _ in range(universe_count):
            if len(data) - offset < 2 + DMX_CHANNEL_COUNT:
                raise ValueError('Truncated native DMX frame')
            number = struct.unpack_from('<H', data, offset)[0]
            offset += 2
            if number in seen:
                raise ValueError('Duplicate universe in native frame')
            seen.add(number)
            if number == universe:
                frames.append((seconds, data[offset : offset + DMX_CHANNEL_COUNT]))
            offset += DMX_CHANNEL_COUNT
    if offset != len(data):
        raise ValueError('Trailing bytes in native capture')
    return frames


def read_capture(path, *, bpm, origin_seconds=0.0, loop=None, universe=0):
    """Convert observed seconds to beats at an explicitly supplied constant BPM."""
    if not math.isfinite(bpm) or bpm <= 0 or not math.isfinite(origin_seconds):
        raise ValueError('BPM must be positive and origin must be finite')
    path = Path(path)
    if path.suffix == '.dmx':
        if loop is not None:
            raise ValueError('Native captures have no named selection filter')
        frames = _native_frames(path, universe)
    else:
        frames = [
            (row['at_seconds'], bytes.fromhex(row['dmx']))
            for row in _read_json(path)
            if row['universe'] == universe and (loop is None or row.get('loop') == loop)
        ]
    if any(len(channels) != DMX_CHANNEL_COUNT for _, channels in frames):
        raise ValueError('Every DMX frame must contain exactly 512 channels')
    beats = (np.array([seconds for seconds, _ in frames]) - origin_seconds) * bpm / 60
    channels = np.array([list(channels) for _, channels in frames], dtype=np.uint8)
    return Capture(beats, channels)


def validate_groups(groups):
    if not groups:
        raise ValueError('At least one semantic channel group is required')
    used = set()
    for name, group in groups.items():
        components = group['channels']
        widths = {len(component) for component in components}
        if not components or not widths <= {1, 2} or len(widths) != 1:
            raise ValueError(f'{name}: components must have the same one- or two-byte width')
        for component in components:
            for address in component:
                if type(address) is not int or not 1 <= address <= DMX_CHANNEL_COUNT:
                    raise ValueError(f'{name}: channel address outside 1..512')
                if address in used:
                    raise ValueError(f'{name}: channel {address} appears more than once')
                used.add(address)
        ranges = group.get('equivalent_ranges', [])
        previous_end = -1
        for lower, upper in ranges:
            if (
                widths != {1}
                or type(lower) is not int
                or type(upper) is not int
                or not 0 <= lower <= upper <= BYTE_MAX
                or lower <= previous_end
            ):
                raise ValueError(f'{name}: equivalent byte ranges must be ordered and disjoint')
            previous_end = upper
    return used


def _values(channels, group):
    components = group['channels']
    values = channels[:, [component[0] - 1 for component in components]].astype(np.int32)
    if len(components[0]) == 2:
        values = values * (BYTE_MAX + 1)
        values += channels[:, [component[1] - 1 for component in components]]
    return values


def _semantic_values(values, ranges):
    mapped = values.copy()
    for lower, upper in ranges:
        mapped[(values >= lower) & (values <= upper)] = lower
    return mapped


def _samples(reference, candidate, start_beat, end_beat, offset_beats, step_beats):
    if not all(math.isfinite(v) for v in [start_beat, end_beat, offset_beats, step_beats]):
        raise ValueError('Window, offset and sample step must be finite')
    if end_beat <= start_beat or step_beats <= 0:
        raise ValueError('Comparison needs a positive window and sample step')
    count = math.ceil((end_beat - start_beat) / step_beats)
    if count > MAX_COMPARISON_SAMPLES:
        raise ValueError('Comparison grid exceeds one million samples')
    beats = np.linspace(start_beat, end_beat, count, endpoint=False)
    return reference.held_at(beats), candidate.held_at(beats + offset_beats)


def compare(
    reference,
    candidate,
    groups,
    *,
    start_beat,
    end_beat,
    offset_beats=0.0,
    step_beats=DEFAULT_SAMPLE_STEP_BEATS,
):
    """Positive offset samples the candidate later; all properties share this offset."""
    covered = validate_groups(groups)
    left, right = _samples(reference, candidate, start_beat, end_beat, offset_beats, step_beats)
    result = {}
    duration = end_beat - start_beat
    for name, group in groups.items():
        reference_values, candidate_values = _values(left, group), _values(right, group)
        errors = np.abs(reference_values - candidate_values)
        raw_equal = (errors == 0).all(axis=1)
        limits = WORD_MAX if len(group['channels'][0]) == 2 else BYTE_MAX
        item = {
            'maximum_value': limits,
            'mae_units': float(errors.mean()),
            'p50_error_units': float(np.quantile(errors, 0.5)),
            'p95_error_units': float(np.quantile(errors, 0.95)),
            'p99_error_units': float(np.quantile(errors, 0.99)),
            'maximum_error_units': int(errors.max()),
            'exact_percent': float(raw_equal.mean() * 100),
            'different_duration_beats': float((~raw_equal).mean() * duration),
            'more_than_one_unit_duration_beats': float((errors > 1).any(axis=1).mean() * duration),
            'reference_range': [int(reference_values.min()), int(reference_values.max())],
            'candidate_range': [int(candidate_values.min()), int(candidate_values.max())],
        }
        ranges = group.get('equivalent_ranges')
        if ranges:
            semantic_equal = (
                _semantic_values(reference_values, ranges)
                == _semantic_values(candidate_values, ranges)
            ).all(axis=1)
            item['semantic_exact_percent'] = float(semantic_equal.mean() * 100)
            item['equivalent_ranges'] = ranges
        result[name] = item
    active = np.flatnonzero(np.any(left != 0, axis=0) | np.any(right != 0, axis=0)) + 1
    uncovered = [int(address) for address in active if address not in covered]
    return {
        'start_beat': start_beat,
        'end_beat': end_beat,
        'candidate_offset_beats': offset_beats,
        'sample_count': len(left),
        'sample_step_beats': duration / len(left),
        'sampling': 'uniform musical time, zero-order hold; no interpolation',
        'active_uncovered_channels': uncovered,
        'groups': result,
    }


def fit_shared_channel_offset_beats(
    reference,
    candidate,
    groups,
    *,
    start_beat,
    end_beat,
    center_beats=0.0,
    radius_beats=0.1,
    step_beats=0.0025,
):
    """Fit one bounded offset; score groups equally in their normalized native units.

    Fit only on calibration data. Call compare on a separate held-out window with
    this unchanged offset; fitting each property or holdout hides model errors.
    """
    validate_groups(groups)
    if not all(math.isfinite(v) for v in [center_beats, radius_beats, step_beats]):
        raise ValueError('Alignment parameters must be finite')
    if radius_beats < 0 or step_beats <= 0:
        raise ValueError('Invalid or excessive alignment search')
    step_count = math.ceil(2 * radius_beats / step_beats) + 1
    if step_count > MAX_ALIGNMENT_STEPS:
        raise ValueError('Invalid or excessive alignment search')
    offsets = np.unique(np.append(np.linspace(-radius_beats, radius_beats, step_count), 0.0))
    offsets += center_beats
    scores = []
    for offset in offsets:
        left, right = _samples(
            reference, candidate, start_beat, end_beat, offset, DEFAULT_SAMPLE_STEP_BEATS
        )
        errors = []
        for group in groups.values():
            raw_left, raw_right = _values(left, group), _values(right, group)
            ranges = group.get('equivalent_ranges', [])
            if ranges:
                error = (
                    _semantic_values(raw_left, ranges) != _semantic_values(raw_right, ranges)
                ).mean()
            else:
                limit = WORD_MAX if len(group['channels'][0]) == 2 else BYTE_MAX
                error = np.abs(raw_left - raw_right).mean() / limit
            errors.append(error)
        scores.append(float(np.mean(errors)))
    best = min(range(len(offsets)), key=lambda i: (scores[i], abs(offsets[i] - center_beats)))
    return {
        'offset_beats': float(offsets[best]),
        'normalized_group_mean_error': scores[best],
        'search_center_beats': center_beats,
        'search_radius_beats': radius_beats,
        'search_step_beats': step_beats,
        'at_search_boundary': best in {0, len(offsets) - 1} and len(offsets) > 1,
        'calibration_window_beats': [start_beat, end_beat],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--groups', type=Path, required=True)
    parser.add_argument('--reference-bpm', type=float, required=True)
    parser.add_argument('--candidate-bpm', type=float, required=True)
    parser.add_argument('--reference-origin-seconds', type=float, default=0.0)
    parser.add_argument('--candidate-origin-seconds', type=float, default=0.0)
    parser.add_argument('--reference-loop')
    parser.add_argument('--candidate-loop')
    parser.add_argument('--universe', type=int, default=0)
    parser.add_argument('--start-beat', type=float, required=True)
    parser.add_argument('--end-beat', type=float, required=True)
    parser.add_argument('--candidate-offset-beats', type=float, default=0.0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = read_capture(
        args.reference,
        bpm=args.reference_bpm,
        origin_seconds=args.reference_origin_seconds,
        loop=args.reference_loop,
        universe=args.universe,
    )
    candidate = read_capture(
        args.candidate,
        bpm=args.candidate_bpm,
        origin_seconds=args.candidate_origin_seconds,
        loop=args.candidate_loop,
        universe=args.universe,
    )
    report = compare(
        reference,
        candidate,
        _read_json(args.groups),
        start_beat=args.start_beat,
        end_beat=args.end_beat,
        offset_beats=args.candidate_offset_beats,
    )
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
