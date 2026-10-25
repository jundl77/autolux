"""Render the four Lua autoloops offline and compare their actual DMX to captured references."""

import argparse
import gzip
import hashlib
import json
import subprocess
from pathlib import Path

from tools.compare_dmx import compare, fit_shared_channel_offset_beats, read_capture

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'starfire/tests/reference/soundswitch'
FRAME_RATE_HZ = 40
RENDER_TIMEOUT_SECONDS = 30
METRIC_EPSILON = 1e-9


def read_json(path):
    data = path.read_bytes()
    if path.suffix == '.gz':
        data = gzip.decompress(data)
    return json.loads(data)


def find_binary():
    for profile in ['release', 'debug']:
        for name in ['starfire.exe', 'starfire']:
            candidate = ROOT / 'starfire/target' / profile / name
            if candidate.is_file():
                return candidate
    raise FileNotFoundError('Build Starfire before checking real autoloop output')


def evaluate_metrics(comparison, limits, continuous_properties):
    """Gate sustained error; retain continuous-property maxima as cut diagnostics.

    Independently sampled cuts can miss an endpoint, so a self-shift maximum is
    not a valid upper bound. Safe controls still require exact values everywhere.
    """
    failures, diagnostics = [], []
    if comparison['active_uncovered_channels']:
        failures.append(f'Active unmapped channels: {comparison["active_uncovered_channels"]}')
    for name, metrics in limits.items():
        for key, limit in metrics.items():
            metric = key.removesuffix('_limit')
            actual = comparison['groups'][name][metric]
            if actual <= limit + METRIC_EPSILON:
                continue
            message = f'{name} {metric}: {actual:.6f} exceeds {limit:.6f}'
            if metric == 'maximum_error_units' and name.rsplit('.', 1)[-1] in continuous_properties:
                diagnostics.append(message)
            else:
                failures.append(message)
    return failures, diagnostics


def check_autoloop(bank, binary, output_dir):
    manifest = read_json(REFERENCE / 'manifest.json')
    groups = read_json(REFERENCE / 'channel-groups.json')
    alignment = manifest['alignment']
    details = manifest['banks'][bank]
    output_dir.mkdir(parents=True, exist_ok=True)
    script = ROOT / 'autoloop' / bank / f'{details["autoloop"]}.lua'
    result = {'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(), 'captures': {}}
    offset = None
    for reference in manifest['captures']:
        bpm = reference['bpm']
        budgets = read_json(
            REFERENCE / reference.get('cadence_budget_file', 'cadence-budgets.json.gz')
        )
        stem = output_dir / f'{bank}-{bpm}'
        replay = {
            'schema_version': 1,
            'duration_seconds': manifest['beats_per_selection'] * 60 / bpm,
            'frame_rate_hz': FRAME_RATE_HZ,
            'events': [
                {'at_seconds': 0, 'command': {'kind': 'beat', 'position_beats': 0, 'bpm': bpm}},
                {
                    'at_seconds': 0,
                    'command': {
                        'kind': 'select',
                        'bank': bank,
                        'autoloop': details['autoloop'],
                        'transition': {'kind': 'cut'},
                    },
                },
            ],
        }
        stem.with_suffix('.replay.json').write_text(json.dumps(replay), encoding='utf-8')
        subprocess.run(
            [
                str(binary),
                'replay',
                str(stem.with_suffix('.replay.json')),
                '--rig',
                str(ROOT / 'rigs/room.json'),
                '--autoloops',
                str(ROOT / 'autoloop'),
                '--output',
                str(stem.with_suffix('.dmx')),
                '--summary',
                str(stem.with_suffix('.summary.json')),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        captured = read_capture(
            REFERENCE / reference['files']['frames']['path'],
            bpm=bpm,
            origin_seconds=reference['selected_at_seconds'][bank],
            loop=bank,
        )
        rendered = read_capture(stem.with_suffix('.dmx'), bpm=bpm)
        if offset is None:
            if bpm != alignment['calibration_bpm']:
                raise ValueError('Calibration capture must be first')
            first, last = alignment['calibration_window_beats']
            fitted = fit_shared_channel_offset_beats(
                captured,
                rendered,
                groups,
                start_beat=first,
                end_beat=last,
                radius_beats=alignment['maximum_shared_offset_beats'],
            )
            result['alignment'] = fitted
            offset = fitted['offset_beats']
        first, last = alignment['evaluation_window_beats']
        comparison = compare(
            captured, rendered, groups, start_beat=first, end_beat=last, offset_beats=offset
        )
        limits = budgets['captures'][str(bpm)][bank]['groups']
        failures, diagnostics = evaluate_metrics(
            comparison, limits, budgets['continuous_properties']
        )
        if result['alignment']['at_search_boundary']:
            failures.append('Shared phase optimum reaches the permitted search boundary')
        comparison['failures'] = failures
        comparison['maximum_error_diagnostics'] = diagnostics
        comparison['render_summary'] = read_json(stem.with_suffix('.summary.json'))
        result['captures'][str(bpm)] = comparison
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary or find_binary()
    manifest = read_json(REFERENCE / 'manifest.json')
    report = {bank: check_autoloop(bank, binary, args.output) for bank in manifest['banks']}
    (args.output / 'comparison.json').write_text(
        json.dumps(report, indent=2) + '\n', encoding='utf-8'
    )
    failed = False
    for bank, result in report.items():
        for bpm, capture in result['captures'].items():
            print(f'{bank} {bpm} BPM: {len(capture["failures"])} failed metrics')
            for message in capture['failures']:
                failed = True
                print(f'  {message}')
            for message in capture['maximum_error_diagnostics']:
                print(f'  Cut diagnostic: {message}')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
