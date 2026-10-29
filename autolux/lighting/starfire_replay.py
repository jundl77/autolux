from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autolux.lib.clients.starfire_client import rms_to_normalized_level
from autolux.lib.engine.autoloop_policy import (
    LOUDNESS_TREND_BEATS,
    SelectionContext,
    loudness_trend,
)
from autolux.lib.engine.cue import Cue
from autolux.lib.engine.cue_director import GAP_REFERENCE_BEATS, SectionRequest
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOPS,
    STARFIRE_MAX_STROBE_HZ,
    LightIntent,
    ShowSettings,
)
from autolux.lib.engine.light_engine import BUILD_INTENTS
from autolux.lib.engine.section_decoder import BEATS_PER_BAR
from autolux.lighting.autoloop_catalogue import (
    AUTOLOOP_DIRECTORY,
    discover_autoloops,
    read_autoloop_meta,
    read_overlay_meta,
)
from autolux.lighting.palettes import DEFAULT_PALETTE, palette_records
from autolux.lighting.special_catalogue import Venue

DEFAULT_FRAME_RATE_HZ = 40
MAX_FRAME_RATE_HZ = 100
MAX_TEMPO_BPM = 1_000.0
AUTOLOOP_BY_INTENT = {intent.value: loops[0] for intent, loops in DEFAULT_AUTOLOOPS.items()}
PREVIEW_KIND = 'starfire_preview'
PREVIEW_CELL_FIELDS = 5
OVERVIEW_COLUMNS = 240
HEADROOM = 0.9
WHITE_MIN_CHANNEL = 0.9
PHRASE_BARS = 16
SECTION_COLOURS = 2
SECOND_COLOUR_SHARE = 0.1


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


class _NoLights:
    @staticmethod
    def groups() -> dict:
        return {}


@dataclass(frozen=True)
class _Section:
    at: float
    intent: LightIntent | None


def _start_beat(placed: list[tuple[float, int, float]], at: float) -> tuple[float, int, float]:
    near = [beat for beat in placed if abs(beat[0] - at) <= 30.0 / beat[2]]
    return min(near, key=lambda beat: abs(beat[0] - at), default=None)


def _levels_before(levels: list[tuple[float, float]], start: tuple[float, int, float]) -> tuple:
    opens = start[0] - 30.0 / start[2]
    before = [level for at, level in levels if at < opens]
    return tuple(before[-max(GAP_REFERENCE_BEATS, LOUDNESS_TREND_BEATS) :])


@dataclass(frozen=True)
class _Planning:
    seed: int
    venue: Venue
    settings: tuple[tuple[float, ShowSettings], ...]

    def settings_at(self, at: float) -> ShowSettings:
        current = self.settings[0][1]
        for since, settings in self.settings:
            if since <= at:
                current = settings
        return current


def _planning(report: dict) -> _Planning:
    planning = report.get('planning')
    if planning is None:
        return _Planning(0, Venue(), ((0.0, ShowSettings()),))
    if not isinstance(planning, dict):
        raise TypeError('report.planning must be an object')
    seed = planning.get('seed')
    if type(seed) is not int or seed < 0:
        raise ValueError('report.planning.seed must be a nonnegative integer')
    venue = planning.get('venue')
    if not isinstance(venue, dict):
        raise TypeError('report.planning.venue must be an object')
    changes = planning.get('settings')
    if not isinstance(changes, list) or not changes:
        raise ValueError('report.planning.settings must list the settings in force')
    if not all(isinstance(change, dict) for change in changes):
        raise TypeError('report.planning.settings entries must be objects')
    settings = tuple(
        (
            _nonnegative(change.get('t'), 'report.planning.settings.t'),
            ShowSettings.from_dict(change.get('settings')),
        )
        for change in changes
    )
    return _Planning(seed, Venue(**venue), settings)


def _cue_events(
    sections: list[_Section],
    placed: list[tuple[float, int, float]],
    levels: list[tuple[float, float]],
    delay: float,
    plan: Callable[[int, SectionRequest], Cue],
    reset: Callable[[], None],
) -> list[dict]:
    events = []
    previous = drop_ended_at = build_started_at = None
    new_track = True
    for moment, section in enumerate(sections):
        at, intent = section.at, section.intent
        if intent is None:
            events.append({'at_seconds': at, 'command': {'kind': 'clear_cues'}})
            reset()
            previous = drop_ended_at = build_started_at = None
            new_track = True
            continue
        # The room's history carried into the section, as light_engine's _plan_context has it.
        if previous is LightIntent.DROP and intent is not LightIntent.DROP:
            drop_ended_at = at
        start = _start_beat(placed, at)
        if start is None:
            bank, autoloop = AUTOLOOP_BY_INTENT[intent.value]
            command = {'kind': 'select', 'bank': bank, 'autoloop': autoloop}
            command |= {'palette': DEFAULT_PALETTE, 'transition': {'kind': 'cut'}}
            events.append({'at_seconds': at, 'command': command})
        else:
            beat_at, index, bpm = start
            building = (
                intent in (LightIntent.BUILDUP, LightIntent.DROP) and previous in BUILD_INTENTS
            )
            bar_seconds = BEATS_PER_BAR * 60.0 / bpm
            levels_before = _levels_before(levels, start)
            context = SelectionContext(
                intent,
                seconds_since_drop=None if drop_ended_at is None else at - drop_ended_at,
                build_bars=(at - build_started_at) / bar_seconds
                if building and build_started_at is not None
                else 0.0,
                loudness=loudness_trend(levels_before),
                new_track=new_track,
            )
            sent_at = max(0.0, beat_at - delay)
            request = SectionRequest(
                start_beat_index=index,
                intent=intent,
                context=context,
                room_sec=beat_at,
                bpm=bpm,
                levels_before=levels_before,
                lead_beats=(beat_at - sent_at) * bpm / 60.0,
            )
            cue = plan(moment, request)
            events.append({'at_seconds': sent_at, 'command': cue.command()})
            new_track = False
        if intent not in BUILD_INTENTS:
            build_started_at = None
        elif previous not in BUILD_INTENTS:
            build_started_at = at
        previous = intent
    return events


def report_to_replay(
    report: dict,
    *,
    frame_rate_hz: int = DEFAULT_FRAME_RATE_HZ,
    autoloops: Path = AUTOLOOP_DIRECTORY,
) -> dict:
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
    placed: list[tuple[float, int, float]] = []
    levels: list[tuple[float, float]] = []
    last_bpm = None
    previous_beat_time = 0.0
    for beat_index, beat in enumerate(beats):
        at = _nonnegative(beat.get('t'), 'beat.t')
        if not previous_beat_time <= at <= duration:
            raise ValueError('beat times must be ordered and within the report duration')
        previous_beat_time = at
        if 'rms' in beat:
            levels.append((at, rms_to_normalized_level(_nonnegative(beat['rms'], 'beat.rms'))))
            events.append(
                {
                    'at_seconds': at,
                    'command': {
                        'kind': 'set_normalized_rms_level',
                        'level': rms_to_normalized_level(_nonnegative(beat['rms'], 'beat.rms')),
                    },
                }
            )
        bpm = _nonnegative(beat.get('bpm'), 'beat.bpm')
        if bpm > MAX_TEMPO_BPM:
            raise ValueError(f'beat.bpm exceeds {MAX_TEMPO_BPM:g}')
        if bpm > 0:
            last_bpm = bpm
        if last_bpm is not None:
            placed.append((at, beat_index, last_bpm))
            events.append(
                {
                    'at_seconds': at,
                    'command': {
                        'kind': 'beat',
                        'beat_index': beat_index,
                        'bar_phase': None,
                        'beats_per_bar': BEATS_PER_BAR,
                        'bpm': last_bpm,
                    },
                }
            )
    if last_bpm is None:
        raise ValueError('report has no beats with a usable positive tempo')

    sections: list[_Section] = []
    stops = []
    for intent in intents:
        delivered_at = _nonnegative(intent.get('t'), 'intent.t')
        if delivered_at > duration:
            raise ValueError('intent delivery time exceeds report duration')
        if intent.get('trigger') == 'silence':
            sections.append(_Section(delivered_at, None))
            stops.append({'at_seconds': delivered_at, 'command': {'kind': 'stop'}})
            continue
        song_time = intent.get('song_t')
        at = (
            _nonnegative(song_time, 'intent.song_t')
            if song_time is not None
            else max(0.0, delivered_at - delay)
        )
        if at > duration:
            raise ValueError('intent source time exceeds report duration')
        name = intent.get('intent')
        if not isinstance(name, str) or name not in AUTOLOOP_BY_INTENT:
            raise ValueError(f'unknown lighting intent: {name!r}')
        if not sections or sections[-1].intent is not LightIntent(name):
            sections.append(_Section(at, LightIntent(name)))
    planning = _planning(report)
    in_force = [planning.settings_at(0.0)]
    controller = EffectController(
        _NoLights(),
        settings=lambda: in_force[0],
        autoloops=discover_autoloops(autoloops),
        autoloop_meta=read_autoloop_meta(autoloops),
        overlay_meta=read_overlay_meta(autoloops),
        seed=planning.seed,
        venue=planning.venue,
    )

    def plan(moment: int, request: SectionRequest) -> Cue:
        in_force[0] = planning.settings_at(request.room_sec + delay)
        return controller.plan_cue(moment, request)

    events += _cue_events(sections, placed, levels, delay, plan, controller.reset_cues) + stops
    events.sort(key=lambda event: (event['at_seconds'], event['command']['kind'] != 'beat'))
    if not events or events[-1] != {'at_seconds': duration, 'command': {'kind': 'stop'}}:
        events.append({'at_seconds': duration, 'command': {'kind': 'stop'}})
    return {
        'duration_seconds': duration,
        'frame_rate_hz': frame_rate_hz,
        'palettes': palette_records(),
        'events': events,
    }


@dataclass(frozen=True)
class Preview:
    # Each frame's `c` holds one [i, r, g, b, strobe_hz, direction?, gobo?] per header cell.

    header: dict
    frames: list[dict]

    @property
    def rate_hz(self) -> int:
        return self.header['frame_rate_hz']


def is_preview(text: str) -> bool:
    first = text.partition('\n')[0]
    try:
        header = json.loads(first)
    except ValueError:
        return False
    return isinstance(header, dict) and header.get('kind') == PREVIEW_KIND


def load_preview(text: str) -> Preview:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not is_preview(lines[0]):
        raise ValueError(f'not a {PREVIEW_KIND} file')
    header = json.loads(lines[0])
    rate = header.get('frame_rate_hz')
    cells = header.get('cells')
    if type(rate) is not int or not 1 <= rate <= MAX_FRAME_RATE_HZ:
        raise ValueError(f'preview frame rate must be an integer in 1..{MAX_FRAME_RATE_HZ}')
    if not isinstance(cells, list) or not all(
        isinstance(cell, dict) and isinstance(cell.get('fixture'), str) for cell in cells
    ):
        raise ValueError('preview cells must name their fixture')
    groups = header.get('groups')
    if not isinstance(groups, dict):
        raise TypeError('preview groups must be an object')
    if not all(
        isinstance(members, list)
        and all(type(cell) is int and 0 <= cell < len(cells) for cell in members)
        for members in groups.values()
    ):
        raise ValueError('preview group members must be header cell indices')
    frames = [json.loads(line) for line in lines[1:]]
    if len(frames) != header.get('frames'):
        raise ValueError(
            f'preview has {len(frames)} frames, its header says {header.get("frames")}'
        )
    for frame in frames:
        values = frame.get('c') if isinstance(frame, dict) else None
        if not isinstance(values, list) or len(values) != len(cells):
            raise ValueError('every preview frame needs one entry per header cell')
        if not all(isinstance(cell, list) and len(cell) >= PREVIEW_CELL_FIELDS for cell in values):
            raise ValueError('a preview cell needs intensity, RGB and strobe')
    return Preview(header, frames)


def bar_starts(preview: Preview) -> list[tuple[int, int]]:
    beats_per_bar = preview.header.get('beats_per_bar')
    if not beats_per_bar:
        return []
    starts, previous = [], None
    for index, frame in enumerate(preview.frames):
        bar = math.floor(frame['beat'] / beats_per_bar)
        if bar != previous:
            starts.append((bar + 1, index))
            previous = bar
    return starts


def _runs(preview: Preview, key) -> list[dict]:
    runs = []
    for index, frame in enumerate(preview.frames):
        name = key(frame)
        if runs and runs[-1]['name'] == name and runs[-1]['end'] == index:
            runs[-1]['end'] = index + 1
        elif name is not None:
            runs.append({'name': name, 'start': index, 'end': index + 1})
    return runs


def _overlay_spans(preview: Preview) -> list[dict]:
    spans, open_spans = [], {}
    for index, frame in enumerate(preview.frames):
        sounding = set(frame.get('overlays', ()))
        for name in set(open_spans) - sounding:
            open_spans.pop(name)
        for name in sorted(sounding):
            if name in open_spans:
                open_spans[name]['end'] = index + 1
            else:
                open_spans[name] = {'name': name, 'start': index, 'end': index + 1}
                spans.append(open_spans[name])
    return spans


def _sections(preview: Preview) -> list[dict]:
    sections = []
    for index, frame in enumerate(preview.frames):
        base = '/'.join(filter(None, (frame.get('bank'), frame.get('autoloop')))) or None
        if not sections or sections[-1]['base'] != base:
            sections.append(
                {'base': base, 'bank': frame.get('bank'), 'start': index, 'end': index, 'light': {}}
            )
        section = sections[-1]
        section['end'] = index + 1
        for intensity, red, green, blue, *_ in frame['c']:
            if intensity > 0:
                colour = (round(red, 2), round(green, 2), round(blue, 2))
                section['light'][colour] = section['light'].get(colour, 0.0) + intensity
    for section in sections:
        light = section.pop('light')
        total = sum(light.values())
        ranked = sorted(light.items(), key=lambda item: -item[1])[:SECTION_COLOURS]
        section['colours'] = [
            list(colour)
            for rank, (colour, share) in enumerate(ranked)
            if rank == 0 or share >= SECOND_COLOUR_SHARE * total
        ]
    return sections


def preview_overview(preview: Preview, columns: int = OVERVIEW_COLUMNS) -> dict:
    frames = preview.frames
    count = min(columns, len(frames))
    groups = {name: members for name, members in preview.header['groups'].items() if members}
    low = {name: [1.0] * count for name in groups}
    high = {name: [0.0] * count for name in groups}
    white, faults, strobe = [0] * count, [0] * count, [0.0] * count
    sizes = [0] * count
    strobing = dict.fromkeys(groups, 0)
    strobing_any = 0
    for index, frame in enumerate(frames):
        column = index * count // len(frames)
        sizes[column] += 1
        cells = frame['c']
        for name, members in groups.items():
            levels = [cells[cell][0] for cell in members]
            low[name][column] = min(low[name][column], *levels)
            high[name][column] = max(high[name][column], *levels)
            strobing[name] += any(cells[cell][4] > 0 for cell in members)
        white[column] += any(cell[0] > 0 and min(cell[1:4]) >= WHITE_MIN_CHANNEL for cell in cells)
        faults[column] += bool(frame.get('fault'))
        fastest = max(cell[4] for cell in cells)
        strobe[column] = max(strobe[column], fastest)
        strobing_any += fastest > 0

    def share(counts):
        return [round(total / size, 3) for total, size in zip(counts, sizes, strict=True)]

    return {
        'columns': count,
        'headroom': HEADROOM,
        'groups': {name: {'low': low[name], 'high': high[name]} for name in groups},
        'white': share(white),
        'strobe': strobe,
        'strobe_max_hz': STARFIRE_MAX_STROBE_HZ,
        'faults': share(faults),
        'strobe_seconds': round(strobing_any / preview.rate_hz, 3),
        'group_strobe_seconds': {
            name: round(frames_on / preview.rate_hz, 3) for name, frames_on in strobing.items()
        },
        'phrases': [frame for bar, frame in bar_starts(preview) if (bar - 1) % PHRASE_BARS == 0],
        'sections': _sections(preview),
        'intents': _runs(preview, lambda frame: frame.get('intent') or frame.get('bank')),
        'overlays': _overlay_spans(preview),
    }
