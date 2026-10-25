"""Compare source analysis and semantic lighting behavior from deterministic simulation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

ANALYSIS_BEAT_COLUMNS = ('t', 'bpm', 'change', 'rms')
ANALYSIS_DIGEST_KEYS = (
    'beats_detected',
    'beat_times_hash',
    'beat_columns_hash',
    'bpm_median',
    'bpm_last',
    'rms_mean',
    'duration_sec',
    'sound_events',
)

# Matches the command-timing tolerance tests/test_simulation.py holds the queue to.
TIMING_ACCURACY_MAX_MS = 10.0

_STREAM_NAMES = ('sound', 'lighting')


def _hash(values) -> str:
    canonical = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _median(xs):
    if not xs:
        return 0.0
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def _normalise_streams(streams: dict | None) -> dict:
    streams = streams or {}
    return {name: list(streams.get(name, ())) for name in _STREAM_NAMES}


def _non_decreasing(values) -> bool:
    values = list(values)
    return all(a <= b for a, b in zip(values, values[1:]))


def _queue_errors_ms(report: dict) -> list:
    return [abs(e['actual_delta_sec'] - e['target_delta_sec']) * 1000 for e in report['timing_log']]


def _queue_error_buffers(report: dict) -> int:
    import math

    from autolux.lib.audio_config import BUFFER_SIZE, SAMPLE_RATE

    period_ms = BUFFER_SIZE / SAMPLE_RATE * 1000
    return int(math.ceil(max(_queue_errors_ms(report), default=0.0) / period_ms))


def _sound_events(events: list) -> list:
    return [{'t': round(e['t'], 3), 'playing': e['playing']} for e in events]


def survives_digest(report: dict, streams: dict | None = None) -> dict:
    beats = report['beats']
    metrics = report['metrics']
    streams = _normalise_streams(streams)
    return {
        'beats_detected': metrics['beats_detected'],
        'beat_times_hash': _hash([round(b['t'], 4) for b in beats]),
        'beat_columns_hash': _hash([[b[c] for c in ANALYSIS_BEAT_COLUMNS] for b in beats]),
        'bpm_median': round(_median(sorted(b['bpm'] for b in beats if b['bpm'] > 0)), 4),
        'bpm_last': metrics['bpm_last'],
        'rms_mean': round(sum(b['rms'] for b in beats) / len(beats), 6) if beats else 0.0,
        'duration_sec': round(report['duration_sec'], 3),
        'sound_events': _sound_events(streams['sound']),
    }


def _command_effect(command: dict) -> tuple[str, str] | None:
    if command['kind'] == 'select':
        return ('SEQUENCE', f"{command['bank']}/{command['sequence']}")
    if command['kind'] == 'set_override':
        return ('ACCENT', command['value']['id'])
    return None


def _effects_match_report(events: list, report_effects: list) -> bool:
    applied = [_command_effect(event['command']) for event in events]
    return [effect for effect in applied if effect is not None] == [
        (event['type'], event['name']) for event in report_effects
    ]


def _intent_at(intents: list, t: float) -> str | None:
    current = None
    for block in intents:
        if block['t'] <= t + 1e-9:
            current = block['intent']
    return current


def _effects_match_intents(report: dict) -> bool:
    from autolux.lib.engine.effect_definitions import LightIntent, SEQUENCE_BY_LIGHT_INTENT

    pools = {
        intent.value: {f'{bank}/{sequence}'}
        for intent, (bank, sequence) in SEQUENCE_BY_LIGHT_INTENT.items()
    }
    pools[LightIntent.DROP.value].add('drop_arrival')
    return all(
        effect['name'] in pools.get(_intent_at(report['intents'], effect['t']), ())
        for effect in report['effects']
    )


def relations_digest(report: dict, streams: dict | None = None) -> dict:
    streams = _normalise_streams(streams)
    events = streams['lighting']
    errors_ms = _queue_errors_ms(report)
    return {
        'lighting_arrives_in_order': _non_decreasing(e['at_seconds'] for e in events),
        'lighting_matches_report': _effects_match_report(events, report['effects']),
        'sequences_match_intents': _effects_match_intents(report),
        'queue_error_within_tolerance': max(errors_ms, default=0.0) < TIMING_ACCURACY_MAX_MS,
    }


def informational_digest(report: dict, streams: dict | None = None) -> dict:
    metrics = report['metrics']
    streams = _normalise_streams(streams)
    lighting = streams['lighting']
    digest = {
        'intent_changes_count': metrics['intent_changes_count'],
        'effect_changes_count': metrics['effect_changes_count'],
        'unique_intents_count': metrics['unique_intents_count'],
        'dominant_intent': metrics['dominant_intent'],
        'intent_distribution_sec': metrics['intent_distribution_sec'],
        'lighting': {
            'commands': len(lighting),
            'kinds': sorted({event['command']['kind'] for event in lighting}),
            'ordering_hash': _hash([event['command'] for event in lighting]),
        },
        'timing': {
            'target_delta_sec': sorted(
                {round(e['target_delta_sec'], 4) for e in report['timing_log']}
            ),
            'commands': len(report['timing_log']),
        },
    }
    return digest


def degradation_digest(report: dict, streams: dict | None = None) -> dict:
    from autolux.lib.engine.effect_definitions import LightIntent

    beats = report['beats']
    intents = report['intents']
    classified = [e for e in intents if e['intent'] != LightIntent.ATMOSPHERIC.value]
    return {
        'beats_detected': report['metrics']['beats_detected'],
        'beat_times_hash': _hash([round(b['t'], 4) for b in beats]),
        'sound_events': _sound_events(_normalise_streams(streams)['sound']),
        'intent_blocks': len(intents),
        'classified_blocks': len(classified),
        'atmospheric_blocks': len(intents) - len(classified),
        'intents_held': sorted({e['intent'] for e in intents}),
        'effect_changes': report['metrics']['effect_changes_count'],
    }


def held_start_to_end(degradation: dict) -> bool:
    return (
        degradation['intent_blocks'] >= 1
        and degradation['classified_blocks'] <= 1
        and len([i for i in degradation['intents_held'] if i != 'atmospheric']) <= 1
        and degradation['effect_changes'] <= degradation['atmospheric_blocks'] + 1
    )


def digest_report(
    report: dict, streams: dict | None = None, *, wall_elapsed: float | None = None
) -> dict:
    digest = {
        'survives': survives_digest(report, streams),
        'relations': relations_digest(report, streams),
        'informational': informational_digest(report, streams),
        'degradation': degradation_digest(report, streams),
    }
    if wall_elapsed is not None:
        digest['speed'] = {
            'wall_elapsed_sec': round(wall_elapsed, 3),
            'realtime_factor': round(report['duration_sec'] / max(wall_elapsed, 1e-9), 2),
        }
    return digest


def check_digest(name: str, digest: dict, fixture: dict) -> list:
    if name not in fixture:
        return [f'{name}: not in the fixture']
    failures = []
    recorded = fixture[name]['survives']
    # Analysis invariants have their own contract; renderer payloads are checked
    # by the semantic relations and Starfire's full-DMX replay tests.
    expected = {key: recorded[key] for key in ANALYSIS_DIGEST_KEYS if key in recorded}
    if digest['survives'] != expected:
        moved = sorted(set(digest['survives']) | set(expected))
        moved = [k for k in moved if expected.get(k) != digest['survives'].get(k)]
        failures.append(f'{name}: survivors moved -> {", ".join(moved)}')
    broken = sorted(k for k, v in digest['relations'].items() if v is not True)
    if broken:
        failures.append(f'{name}: relations broken -> {", ".join(broken)}')
    return failures


def capture_streams(components: dict) -> dict:
    return {
        'sound': components['event_buffer'].sound_events(),
        'lighting': components['lighting_client'].events,
    }


async def digest_track(path: str) -> dict:
    from autolux.lib.audio_config import BUFFER_SIZE, SAMPLE_RATE
    from autolux.simulate.evaluator import report_checksum
    from autolux.simulate.fake_audio_client import FileAudioClient
    from autolux.simulate.runner import run_fast_simulation_components

    wall_start = time.monotonic()
    components, command_queue = await run_fast_simulation_components(
        FileAudioClient(SAMPLE_RATE, BUFFER_SIZE, path)
    )
    wall_elapsed = time.monotonic() - wall_start
    report = components['event_buffer'].to_report(command_queue.get_timing_log())
    digest = digest_report(report, capture_streams(components), wall_elapsed=wall_elapsed)
    digest['report_checksum'] = report_checksum(report)
    return digest


def fixture_key(path: str) -> str:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from eval_assets import opaque_name
    from select_eval_set import EVAL_SET_FILE, load_eval_set

    stem = Path(path).stem
    for track in load_eval_set(EVAL_SET_FILE)['tracks']:
        youtube_id = track['youtube_id']
        if opaque_name(youtube_id) == stem:
            return f'{youtube_id}.mp3'
    return Path(path).name


def _drop_machine_dependent_speed(digests: dict) -> None:
    for digest in digests.values():
        digest.pop('speed', None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('tracks', nargs='+')
    ap.add_argument(
        '--write',
        metavar='PATH',
        help='write the digests to PATH as a JSON object keyed by track name',
    )
    ap.add_argument(
        '--check',
        metavar='PATH',
        help='compare the survivors and relations against a committed '
        'fixture and exit non-zero on any difference',
    )
    args = ap.parse_args()

    digests = {}
    for track in args.tracks:
        name = fixture_key(track)
        print(f'--- {name}')
        digests[name] = asyncio.run(digest_track(track))
        print(json.dumps(digests[name], indent=2, sort_keys=True))

    if args.write:
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        _drop_machine_dependent_speed(digests)
        out.write_text(json.dumps(digests, indent=2, sort_keys=True) + '\n', newline='\n')
        print(f'\nwrote {out}')

    if args.check:
        fixture = json.loads(Path(args.check).read_text())
        failures = [
            failure
            for name, digest in digests.items()
            for failure in check_digest(name, digest, fixture)
        ]
        print(
            '\n'
            + ('\n'.join(failures) if failures else f'all {len(digests)} tracks match {args.check}')
        )
        raise SystemExit(1 if failures else 0)


if __name__ == '__main__':
    main()
