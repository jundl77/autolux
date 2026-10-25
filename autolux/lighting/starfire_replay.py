"""Convert recorded analysis into musical-time Starfire input, without audio.

Beats are recorded before the playback queue; intents carry their source boundary
in ``song_t`` and their delivered time in ``t``. Prefer the source boundary and
subtract look-ahead only for older reports lacking it. This reproduces musical
decisions, not delivery latency or unrecorded accents.
"""

from __future__ import annotations

import math

REPLAY_SCHEMA_VERSION = 1
DEFAULT_FRAME_RATE_HZ = 40
MAX_FRAME_RATE_HZ = 100
MAX_TEMPO_BPM = 1_000.0
SEQUENCE_BY_INTENT = {
    'atmospheric': ('atmospheric', 'blue_tide'),
    'breakdown': ('breakdown', 'violet_space'),
    'buildup': ('buildup', 'current'),
    'drop': ('drop', 'blue_impact'),
}


def _nonnegative(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError(f'{name} must be a number')
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f'{name} exceeds the finite numeric range') from error
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{name} must be finite and nonnegative')
    return number


def _records(report: dict, name: str) -> list[dict]:
    records = report.get(name)
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError(f'report.{name} must be a list of records')
    return records


def report_to_replay(report: dict, *, frame_rate_hz: int = DEFAULT_FRAME_RATE_HZ) -> dict:
    """Return source-time beat/selection commands with a terminal lighting stop.

    Reports do not retain beat ordinals, downbeats or accents. Beat positions are
    reconstructed from detection order, not a known bar/downbeat origin.
    Zero-tempo warm-up detections do not anchor the clock; later zero tempo holds
    the last known positive tempo.
    """
    if not isinstance(report, dict):
        raise TypeError('report must be an object')
    if type(frame_rate_hz) is not int or not 1 <= frame_rate_hz <= MAX_FRAME_RATE_HZ:
        raise ValueError(f'frame rate must be an integer in 1..{MAX_FRAME_RATE_HZ}')
    duration = _nonnegative(report.get('duration_sec'), 'report.duration_sec')
    metrics = report.get('metrics')
    if not isinstance(metrics, dict):
        raise TypeError('report.metrics must include look_ahead_sec')
    delay = _nonnegative(metrics.get('look_ahead_sec'), 'report.metrics.look_ahead_sec')
    beats = _records(report, 'beats')
    intents = _records(report, 'intents')
    events = []
    last_bpm = None
    previous_beat_time = 0.0
    for beat_index, beat in enumerate(beats):
        at = _nonnegative(beat.get('t'), 'beat.t')
        if not previous_beat_time <= at <= duration:
            raise ValueError('beat times must be ordered and within the report duration')
        previous_beat_time = at
        bpm = _nonnegative(beat.get('bpm'), 'beat.bpm')
        if bpm > MAX_TEMPO_BPM:
            raise ValueError(f'beat.bpm exceeds {MAX_TEMPO_BPM:g}')
        if bpm > 0:
            last_bpm = bpm
        if last_bpm is not None:
            events.append(
                {
                    'at_seconds': at,
                    'command': {'kind': 'beat', 'position_beats': beat_index, 'bpm': last_bpm},
                }
            )
    if last_bpm is None:
        raise ValueError('report has no beats with a usable positive tempo')

    for intent in intents:
        delivered_at = _nonnegative(intent.get('t'), 'intent.t')
        if delivered_at > duration:
            raise ValueError('intent delivery time exceeds report duration')
        if intent.get('trigger') == 'silence':
            at = delivered_at
            command = {'kind': 'stop'}
        else:
            song_time = intent.get('song_t')
            at = (
                _nonnegative(song_time, 'intent.song_t')
                if song_time is not None
                else max(0.0, delivered_at - delay)
            )
            if at > duration:
                raise ValueError('intent source time exceeds report duration')
            name = intent.get('intent')
            if not isinstance(name, str) or name not in SEQUENCE_BY_INTENT:
                raise ValueError(f'unknown lighting intent: {name!r}')
            bank, sequence = SEQUENCE_BY_INTENT[name]
            command = {
                'kind': 'select',
                'bank': bank,
                'sequence': sequence,
                'transition': {'kind': 'cut'},
            }
        events.append({'at_seconds': at, 'command': command})

    # Stable ordering preserves supplied intent order; beat anchors precede controls.
    events.sort(key=lambda event: (event['at_seconds'], event['command']['kind'] != 'beat'))
    selected = None
    commands = []
    for event in events:
        command = event['command']
        if command['kind'] == 'select':
            identity = (command['bank'], command['sequence'])
            if identity == selected:
                continue
            selected = identity
        elif command['kind'] == 'stop':
            selected = None
        commands.append(event)
    if not commands or commands[-1] != {'at_seconds': duration, 'command': {'kind': 'stop'}}:
        commands.append({'at_seconds': duration, 'command': {'kind': 'stop'}})
    return {
        'schema_version': REPLAY_SCHEMA_VERSION,
        'duration_seconds': duration,
        'frame_rate_hz': frame_rate_hz,
        'events': commands,
    }
