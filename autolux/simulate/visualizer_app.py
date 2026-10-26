import argparse
import http.client
import json
import logging
import os
import re
import threading
import time
from statistics import median

import dash
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html

from autolux.lib.engine.effect_definitions import DEFAULT_INTENSITY, SHOW_SETTING_RANGES
from autolux.lib.engine.event_buffer import STOP_PERSISTENCE_SEC
from autolux.lib.engine.static_mode import ALL_FIXTURES, PRESET_COLOURS, colour_hex
from autolux.lib.system_health import health_summary
from autolux.lib.telemetry_history import TELEMETRY_STALE_SECONDS, finite
from autolux.lib.ui_bridge import (
    CONTROL_PATH,
    DASHBOARD_BIND_ADDRESS,
    DASHBOARD_MDNS_HOSTNAME,
    DEFAULT_DASHBOARD_PORT,
    PALETTE_POLICY,
    SNAPSHOT_HOST,
    SNAPSHOT_PATH,
    advertise_dashboard_mdns,
    snapshot_port,
)
from autolux.lighting.fixture_setup import FixtureStore
from autolux.lighting.palettes import BREAKDOWN_SUFFIX, PALETTES
from autolux.simulate import system_metrics
from autolux.simulate.fixture_panel import FIXTURE_CSS, build_fixture_panel
from autolux.simulate.runner import TIMING_TOLERANCE_SEC

TITLE = 'Autolux'
TIMELINE_WINDOW_SEC = 30.0
TIMELINE_LEAD_SEC = 0.5
# Drawn past the lead so the strip the browser translates in from the right is
# always future the audience has not reached, never a gap in the show.
TIMELINE_PAD_SEC = 1.0
TIMELINE_FULL_SEC = TIMELINE_WINDOW_SEC + TIMELINE_LEAD_SEC + TIMELINE_PAD_SEC
NOW_CURSOR_X = TIMELINE_WINDOW_SEC / TIMELINE_FULL_SEC
TIMELINE_MARGIN_L_PX = 8
TIMELINE_MARGIN_R_PX = 8
# The cursor's fraction is of the AXIS, not of the container: ignoring the
# figure margins parked it a constant ~7 px right of the axis' now, which at
# this scale is ~150 ms of claimed future -- measured as a -146 ms crossing
# bias on every marker, and worse the narrower the window.
NOW_CURSOR_LEFT = (
    f'calc({TIMELINE_MARGIN_L_PX}px + {NOW_CURSOR_X * 100:.4f}% - '
    f'{NOW_CURSOR_X:.6f} * {TIMELINE_MARGIN_L_PX + TIMELINE_MARGIN_R_PX}px)'
)
DARK_BG = '#0d1117'
CARD_BG = '#111827'
BORDER = '#1e2937'
OK_COLOR = '#3fb950'
WARN_COLOR = '#f0883e'
MUTED = '#6e7681'
POSTERIOR_FILL = '#58a6ff'

BEAT_MARKER_SIZE = 16
BEATS_PER_BAR = 4
BAR_LABEL_EVERY = 4
BAR_SPAN_TOLERANCE = 1.5
DOWNBEAT_COLOR = 'rgba(88,166,255,0.55)'
BEAT_TICK_COLOR = 'rgba(88,166,255,0.22)'

REFRESH_MS = 250
TELEMETRY_REFRESH_SECONDS = 1
VIEW_EVERY_TICKS = 2
VIEW_REFRESH_MS = REFRESH_MS * VIEW_EVERY_TICKS
STALL_RELEASE_MS = 4000
STALE_REFRESHES = 12

INTENT_CONFIG = {
    'atmospheric': {
        'primary': '#1565c0',
        'label': 'ATMOSPHERIC',
    },
    'breakdown': {
        'primary': '#7b1fa2',
        'label': 'BREAKDOWN',
    },
    'buildup': {
        'primary': '#e65100',
        'label': 'BUILDUP',
    },
    'drop': {
        'primary': '#b71c1c',
        'label': 'DROP',
    },
    'standby': {
        'primary': '#8d6e63',
        'label': 'STANDBY',
    },
}

_DEFAULT_CONFIG = {
    'primary': '#2d3f52',
    'label': '—',
}


def _intent_config(intent_key):
    return INTENT_CONFIG.get(intent_key, _DEFAULT_CONFIG)


def _bridged(stop_t: float, start_t: float) -> bool:
    return stop_t < start_t <= stop_t + STOP_PERSISTENCE_SEC


def _elided(event: dict, snapshot: dict) -> bool:
    others = snapshot.get('sound_events', [])
    if event['playing']:
        return any(_bridged(other['t'], event['t']) for other in others if not other['playing'])
    return any(_bridged(event['t'], other['t']) for other in others if other['playing'])


def _room_stops(snapshot: dict) -> list:
    return [
        event['t'] + max(snapshot.get('look_ahead_sec', 0.0), STOP_PERSISTENCE_SEC)
        for event in snapshot.get('sound_events', [])
        if not event['playing'] and not _elided(event, snapshot)
    ]


def _room_sound_events(snapshot: dict) -> list:
    delay = snapshot.get('look_ahead_sec', 0.0)
    now = snapshot.get('now', 0.0)
    heard = []
    for event in snapshot.get('sound_events', []):
        if _elided(event, snapshot):
            continue
        if event['playing']:
            reaches_room_at = event['t'] + delay
        else:
            reaches_room_at = event['t'] + max(delay, STOP_PERSISTENCE_SEC)
        if reaches_room_at <= now:
            heard.append(dict(event, t=reaches_room_at))
    return heard


def _room_markers(snapshot: dict) -> list:
    # Heard boundaries, plus the stop still travelling: that one rides the lead
    # strip and crosses the cursor at exactly its room instant -- the instant
    # the display ends the song, so afterwards there is no axis to draw it on.
    # A resume inside the window elides it before the room ever reaches it.
    now = snapshot.get('now', 0.0)
    heard = [(event['t'], event['playing']) for event in _room_sound_events(snapshot)]
    pending = [(stop, False) for stop in _room_stops(snapshot) if stop > now]
    return heard + pending


def _room_beats(snapshot: dict, horizon_sec: float = 0.0) -> list:
    delay = snapshot.get('look_ahead_sec', 0.0)
    limit = snapshot.get('now', 0.0) + horizon_sec
    beats = []
    for beat in snapshot.get('beats', []):
        reaches_room_at = beat['t'] + delay
        if reaches_room_at <= limit:
            beats.append(dict(beat, t=reaches_room_at))
    return beats


def _heard_beats(snapshot: dict) -> list:
    return _room_beats(snapshot)


def _clock_text(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f'{minutes}:{secs:02d}'


def _last_heard(snapshot: dict) -> dict | None:
    heard = _room_sound_events(snapshot)
    if not heard:
        return None
    return max(reversed(heard), key=lambda event: event['t'])


def _song_origin(snapshot: dict) -> float | None:
    last = _last_heard(snapshot)
    if last is not None:
        return last['t'] if last['playing'] else None
    starts = [e['t'] for e in snapshot.get('sound_events', []) if e['playing']]
    if not starts:
        return 0.0
    return starts[-1] + snapshot.get('look_ahead_sec', 0.0)


def _room_is_playing(snapshot: dict) -> bool:
    now = snapshot.get('now', 0.0)
    live = bool(snapshot.get('is_playing')) or any(stop > now for stop in _room_stops(snapshot))
    last = _last_heard(snapshot)
    if last is not None:
        return last['playing'] and live
    return not snapshot.get('sound_events') and live


def _room_bpm(snapshot: dict) -> float:
    heard = _heard_beats(snapshot)
    return heard[-1]['bpm'] if heard else snapshot.get('bpm', 0.0)


def _display_now(snapshot: dict, origin: float | None) -> float:
    if origin is None:
        return 0.0
    return max(0.0, snapshot.get('now', 0.0) - origin)


def _song_and_room(snapshot: dict) -> tuple:
    origin = _song_origin(snapshot)
    if origin is None or not any(e['playing'] for e in snapshot.get('sound_events', [])):
        return None, None
    delay = snapshot.get('look_ahead_sec', 0.0)
    return (max(0.0, snapshot.get('now', 0.0) - origin + delay), _display_now(snapshot, origin))


def _anchor(snapshot: dict) -> dict:
    # Every beat is stamped one look-ahead before the room hears it, so the
    # whole drawable window -- the lead included -- is known in advance and the
    # browser can land each marker at exactly its room instant.
    origin = _song_origin(snapshot)
    now = _display_now(snapshot, origin)
    beats = (
        []
        if origin is None
        else [
            round(b['t'] - origin, 3)
            for b in _room_beats(snapshot, TIMELINE_LEAD_SEC + TIMELINE_PAD_SEC)
            if b['t'] - origin >= max(0.0, now - TIMELINE_WINDOW_SEC)
        ]
    )
    song, room = _song_and_room(snapshot)
    return {
        'now': now,
        'beats': beats,
        'song': song,
        'room': room,
        'span': TIMELINE_WINDOW_SEC,
        'lead': TIMELINE_LEAD_SEC,
        'pad': TIMELINE_PAD_SEC,
    }


def _bar_grid(snapshot: dict, origin: float) -> tuple:
    state = snapshot.get('decoder') or {}
    edges = state.get('bar_edges') or []
    if len(edges) < 2:
        return [], []
    delay = snapshot.get('look_ahead_sec', 0.0)
    first_bar = state.get('first_bar') or 0
    spans = [b - a for a, b in zip(edges, edges[1:])]
    bar_sec = state.get('bar_sec') or median(spans)

    shapes, annotations = [], []
    for index, start in enumerate(edges[:-1]):
        at = start + delay - origin
        span = spans[index]
        shapes.append(
            dict(
                type='line',
                xref='x',
                yref='paper',
                x0=at,
                x1=at,
                y0=0.0,
                y1=0.5,
                line=dict(color=DOWNBEAT_COLOR, width=1.4),
            )
        )
        if span <= bar_sec * BAR_SPAN_TOLERANCE:
            for beat in range(1, BEATS_PER_BAR):
                tick = at + span * beat / BEATS_PER_BAR
                shapes.append(
                    dict(
                        type='line',
                        xref='x',
                        yref='paper',
                        x0=tick,
                        x1=tick,
                        y0=0.06,
                        y1=0.20,
                        line=dict(color=BEAT_TICK_COLOR, width=0.8),
                    )
                )
        bar = first_bar + index
        if bar % BAR_LABEL_EVERY == 0:
            annotations.append(
                dict(
                    x=at,
                    y=0.50,
                    xref='x',
                    yref='paper',
                    text=str(bar),
                    showarrow=False,
                    xanchor='left',
                    yanchor='bottom',
                    font=dict(color=DOWNBEAT_COLOR, size=9, family='monospace'),
                )
            )
    return shapes, annotations


def _build_timeline(snapshot: dict) -> go.Figure:
    origin = _song_origin(snapshot)
    now = _display_now(snapshot, origin)
    x0 = now - TIMELINE_WINDOW_SEC
    x1 = now + TIMELINE_LEAD_SEC + TIMELINE_PAD_SEC
    left = max(x0, 0.0)

    shapes, annotations = [], []

    if origin is not None:
        grid_shapes, grid_labels = _bar_grid(snapshot, origin)
        shapes.extend(grid_shapes)
        annotations.extend(grid_labels)

    for entry in snapshot.get('intents', []) if origin is not None else ():
        start = entry['t'] - origin
        end = entry['end'] - origin if 'end' in entry else x1
        t_start = max(start, left)
        t_end = min(end, x1)
        if t_end <= t_start:
            continue
        cfg = _intent_config(entry['intent'])
        color = cfg['primary']
        shapes.append(
            dict(
                type='rect',
                xref='x',
                yref='paper',
                x0=t_start,
                x1=t_end,
                y0=0.52,
                y1=0.96,
                fillcolor=color,
                opacity=0.80,
                line_width=0,
            )
        )
        if t_end - t_start > 1.5:
            annotations.append(
                dict(
                    x=(t_start + t_end) / 2,
                    y=0.74,
                    xref='x',
                    yref='paper',
                    text=cfg['label'],
                    showarrow=False,
                    font=dict(color='rgba(255,255,255,0.85)', size=10, family='monospace'),
                )
            )

    if origin is not None:
        for t_room, is_start in _room_markers(snapshot):
            t = t_room - origin
            if t < left or t > x1:
                continue
            color = '#3fb950' if is_start else '#f85149'
            label = '▶ START' if is_start else '■ STOP'
            shapes.append(
                dict(
                    type='line',
                    xref='x',
                    yref='paper',
                    x0=t,
                    x1=t,
                    y0=0,
                    y1=1,
                    line=dict(color=color, width=1.5, dash='dash'),
                )
            )
            annotations.append(
                dict(
                    x=t,
                    y=0.04,
                    xref='x',
                    yref='paper',
                    text=label,
                    showarrow=False,
                    font=dict(color=color, size=9, family='monospace'),
                    xanchor='left' if is_start else 'right',
                )
            )

    # Beats are deliberately NOT drawn here: a figure push arrives most of a
    # second after the beat it carries, so the browser owns the markers (see
    # ANIMATION_JS) and this figure carries everything that is known ahead.
    fig = go.Figure()
    fig.update_layout(
        shapes=shapes,
        annotations=annotations,
        xaxis=dict(
            range=[x0, x1],
            dtick=5.0,
            tickformat='.0f',
            ticksuffix='s',
            gridcolor='#1a2332',
            color='#6e7681',
            showline=False,
            minor=dict(dtick=1.0, showgrid=True, gridcolor='#151e2b', gridwidth=0.8),
        ),
        yaxis=dict(range=[0, 1], showticklabels=False, showgrid=False),
        plot_bgcolor=DARK_BG,
        paper_bgcolor=DARK_BG,
        height=175,
        margin=dict(l=TIMELINE_MARGIN_L_PX, r=TIMELINE_MARGIN_R_PX, t=6, b=36),
        uirevision='timeline',
        showlegend=False,
    )
    return fig


def _build_decoder(snapshot: dict) -> list:
    state = snapshot.get('decoder') or {}
    classes = state.get('classes') or []
    if not classes:
        return [html.Span('decoder: no decoder on this run', style={'color': MUTED})]

    posterior = state.get('posterior')
    bars = []
    for index, name in enumerate(classes):
        value = 0.0 if posterior is None else float(posterior[index])
        bars.append(
            html.Div(
                [
                    html.Div(
                        name, style={'color': MUTED, 'fontSize': '11px', 'marginBottom': '4px'}
                    ),
                    html.Div(
                        html.Div(
                            style={
                                'width': f'{value * 100:.1f}%',
                                'height': '100%',
                                'background': POSTERIOR_FILL,
                                'borderRadius': '3px',
                                'transition': 'width 0.15s ease',
                            }
                        ),
                        style={
                            'height': '10px',
                            'background': '#161d27',
                            'borderRadius': '3px',
                            'overflow': 'hidden',
                        },
                    ),
                ],
                style={'flex': '1'},
            )
        )

    committed = state.get('committed_bar')
    cursor = (
        'no bar committed yet'
        if committed is None
        else (
            f'bar {state.get("observed_bar")} observed  →  committed '
            f'{committed} {state.get("committed_label")}  ·  '
            f'lag {state.get("lag_bars")} bars'
        )
    )
    if posterior is None:
        cursor += '  ·  no evidence at this bar'
    latency = state.get('chain_latency_sec')
    if latency is not None:
        cursor += f'  ·  chain {latency:.1f}s'

    return [
        html.Div(bars, style={'display': 'flex', 'gap': '10px'}),
        html.Div(cursor, style={'color': MUTED, 'fontSize': '12px', 'marginTop': '8px'}),
    ]


def _dot(colour: str):
    return html.Span(className='dot', style={'background': colour})


def _build_legend() -> list:
    return [
        html.Span([_dot(cfg['primary']), cfg['label'].capitalize()])
        for cfg in INTENT_CONFIG.values()
    ]


def _selection_tags(effect: dict) -> str:
    selection = effect.get('selection') or {}
    tags = [f'E{selection["energy"]}'] if 'energy' in selection else []
    tags += [selection[key] for key in ('family', 'movement') if selection.get(key)]
    return ' · '.join(tags)


HEALTH_COLORS = {'healthy': OK_COLOR, 'degraded': WARN_COLOR, 'down': system_metrics.RED}


def _health(snapshot: dict) -> str:
    health = health_summary(snapshot)['state']
    timing = (snapshot.get('timing_stats') or {}).get('by_label') or {}
    if health == 'healthy' and any(
        stream['mean_error_ms'] > TIMING_TOLERANCE_SEC * 1000 for stream in timing.values()
    ):
        return 'degraded'
    return health


def _build_show_status(snapshot: dict) -> list:
    cfg = _intent_config(snapshot.get('intent'))
    effect = snapshot.get('current_effect') or {}
    name = effect.get('name')
    detail = ' · '.join(
        part
        for part in (name, _selection_tags(effect), (effect.get('selection') or {}).get('reason'))
        if part
    )
    palette = snapshot.get('palette') or {}
    palette_name = palette.get('name') or '—'
    bpm = _room_bpm(snapshot)
    health = _health(snapshot)
    items = [
        html.Span([_dot(cfg['primary']), cfg['label'].capitalize()]),
        html.Span(name.rsplit('/', 1)[-1].replace('_', ' ').title() if name else '—', title=detail),
        html.Span(palette_name),
        html.Span(f'{bpm:.0f} BPM' if bpm else '— BPM'),
        html.Span([_dot(HEALTH_COLORS[health]), health.capitalize()], id='health-indicator'),
    ]
    special = snapshot.get('special')
    if special:
        items.append(
            html.Span(f'fx {special.get("name", "—") if isinstance(special, dict) else special}')
        )
    cooldown = snapshot.get('strobe_cooldown_seconds')
    if finite(cooldown):
        items.append(html.Span(f'strobe in {cooldown:.0f} s' if cooldown > 0 else 'strobe ready'))
    return items


def _build_show_timing(snapshot: dict) -> list:
    song, room = _song_and_room(snapshot)
    return [
        html.Span('Room '),
        html.Span('—' if room is None else _clock_text(room), id='room-clock'),
        html.Span(' · Source '),
        html.Span('—' if song is None else _clock_text(song), id='song-clock'),
        html.Span(f' · delay {snapshot.get("look_ahead_sec", 0):g} s'),
    ]


SETTING_DISPLAY = {
    'drop_strobe_cooldown_seconds': ('Drop strobe cooldown', 's'),
    'strobe_burst_seconds': ('Drop strobe burst', 's'),
    'crossfade_beats': ('Crossfade', 'beats'),
    'quiet_brightness': ('Quiet brightness', '%'),
    'digital_silence_seconds': ('Standby after silence', 's'),
    'strobe_max_rate_hz': ('Max strobe rate', 'Hz'),
    'strobe_max_burst_seconds': ('Max strobe burst', 's'),
    'palette_temperature_bias': ('Palette warmth (cold −1 … warm +1)', ''),
    'intensity': ('Intensity', '%'),
}
SETTING_LABELS = {
    name: f'{label} ({unit})' if unit and unit != '%' else label
    for name, (label, unit) in SETTING_DISPLAY.items()
}
PERCENT_SETTINGS = frozenset(name for name, (_, unit) in SETTING_DISPLAY.items() if unit == '%')
FORM_SETTINGS = tuple(
    name for name in SHOW_SETTING_RANGES if name not in ('intensity', 'palette_temperature_bias')
)
SETTINGS_ORDER = (
    'crossfade_beats',
    'quiet_brightness',
    'digital_silence_seconds',
    'strobe_max_rate_hz',
    'strobe_max_burst_seconds',
    'strobe_burst_seconds',
    'drop_strobe_cooldown_seconds',
)
SETTING_IDS = {f'control-{name}': name for name in FORM_SETTINGS}
BRIGHTNESS_TRIGGERS = ('control-brightness', 'control-brightness-live')
INTENSITY_TRIGGERS = ('control-intensity',)
INTENSITY_LEVELS = (('calm', 0.3), ('measured', 0.7), ('wild', 1.0))
WARMTH = 'palette_temperature_bias'
PALETTE_SWATCH_SLOTS = ('primary', 'secondary', 'accent', 'cold')
ACCENT = '#73b6ff'
PLAIN_DECIMAL_TEXT = re.compile(r'-?(\d+\.?\d*|\.\d+)')
NEEDS_A_NUMBER = '{label} needs a number'
NOT_A_NUMBER = '{label}: "{raw}" is not a number'
LIVE_FADER_INTERVAL_MS = 50
CONTROL_SYNC_HOLD_MS = 1000
CONTROL_EXPECT_SECONDS = 2.0
DIRECT_CONTROL_ENV = 'AUTOLUX_DIRECT_CONTROL'
RELAY_PATH = '/control-relay'


class ControlExpectations:
    def __init__(self, hold_seconds: float = CONTROL_EXPECT_SECONDS, clock=time.monotonic):
        self._hold_seconds = hold_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._expected: dict[str, tuple[object, float]] = {}

    def expect(self, **values) -> None:
        now = self._clock()
        with self._lock:
            self._expected.update({key: (value, now) for key, value in values.items()})

    def apply(self, snapshot: dict) -> dict:
        now = self._clock()
        held = {}
        with self._lock:
            for key, (value, at) in list(self._expected.items()):
                if snapshot.get(key) == value or now - at >= self._hold_seconds:
                    del self._expected[key]
                else:
                    held[key] = value
        return {**snapshot, **held}


def expected_control_values(command: dict, reply: dict) -> dict:
    action = reply.get('accepted')
    if action in ('pause', 'play'):
        return {'paused': action == 'pause'}
    if action == 'master':
        return {'master_brightness': command['level']}
    if action == 'settings':
        return {'settings': command['settings']}
    if action == 'palette' and command['name'] != PALETTE_POLICY:
        return {'palette': {'name': command['name'], 'pinned': True}}
    if action == 'mode':
        return {'mode': command['mode']}
    return {}


def decimal_text(value: float) -> str:
    return f'{value:.6g}'


def setting_text(name: str, value: float) -> str:
    return decimal_text(value * 100 if name in PERCENT_SETTINGS else value)


def parse_decimal(name: str, raw) -> float:
    text = '' if raw is None else str(raw).strip().replace(',', '.')
    if not text:
        raise ValueError(NEEDS_A_NUMBER.format(label=SETTING_LABELS[name]))
    if not PLAIN_DECIMAL_TEXT.fullmatch(text):
        raise ValueError(NOT_A_NUMBER.format(label=SETTING_LABELS[name], raw=raw))
    return round(float(text) / 100, 6) if name in PERCENT_SETTINGS else float(text)


def control_command(
    trigger, paused, level, settings, *, palette=None, intensity=DEFAULT_INTENSITY, warmth=None
) -> dict | None:
    if trigger == 'control-toggle':
        return {'action': 'play' if paused else 'pause'}
    if trigger == 'control-next':
        return {'action': 'next_autoloop'}
    if trigger == 'control-palette':
        return {'action': 'palette', 'name': palette}
    if trigger in BRIGHTNESS_TRIGGERS:
        return {'action': 'master', 'level': level}
    if trigger in INTENSITY_TRIGGERS:
        return {'action': 'intensity', 'level': level}
    if trigger in SETTING_IDS:
        return {
            'action': 'settings',
            'settings': {
                **{name: parse_decimal(name, raw) for name, raw in zip(FORM_SETTINGS, settings)},
                'intensity': intensity,
                **({WARMTH: warmth} if warmth is not None else {}),
            },
        }
    return None


def setting_range_text(name: str) -> str:
    low, high = SHOW_SETTING_RANGES[name]
    return f'{setting_text(name, low)}–{setting_text(name, high)}'


def _setting_input(name):
    label, unit = SETTING_DISPLAY[name]
    return html.Div(
        [
            html.Label(label, htmlFor=f'control-{name}', className='setting-name'),
            html.Div(
                [
                    dcc.Input(
                        id=f'control-{name}',
                        type='text',
                        inputMode='decimal',
                        pattern=r'-?[0-9]*[.,]?[0-9]*',
                        debounce=0.15,
                        disabled=True,
                    ),
                    html.Span(unit, className='setting-unit'),
                ],
                title=f'{setting_range_text(name)} {unit}'.strip(),
                className='setting-value',
            ),
            html.Span(id=f'control-{name}-error', role='alert', className='setting-error'),
        ],
        className='setting-row',
    )


def _build_settings():
    return html.Details(
        [
            html.Summary('Settings', className='settings-toggle'),
            html.Div([_setting_input(name) for name in SETTINGS_ORDER], className='settings-form'),
        ],
        id='settings-drawer',
        className='settings',
    )


def _register_settings_drawer(app) -> None:
    limits = [
        [
            setting_text(name, low),
            setting_text(name, high),
            SETTING_DISPLAY[name][1],
            NEEDS_A_NUMBER.format(label=SETTING_LABELS[name]),
            NOT_A_NUMBER.format(label=SETTING_LABELS[name], raw='{raw}'),
        ]
        for name in FORM_SETTINGS
        for low, high in [SHOW_SETTING_RANGES[name]]
    ]
    app.clientside_callback(
        r'''function(...values) {
            const decimal = new RegExp('^(?:' + '''
        + json.dumps(PLAIN_DECIMAL_TEXT.pattern)
        + r''' + ')$');
            return '''
        + json.dumps(limits)
        + r'''.map(([low, high, unit, needs, notNumber], index) => {
                const raw = values[index];
                const text = raw === null || raw === undefined
                    ? '' : String(raw).trim().replace(',', '.');
                if (!text) {
                    return 'Rejected: ' + needs;
                }
                if (!decimal.test(text)) {
                    return 'Rejected: ' + notNumber.replace('{raw}', raw);
                }
                const value = parseFloat(text);
                if (value < parseFloat(low) || value > parseFloat(high)) {
                    return 'Rejected: must be within ' + low + '–' + high + ' ' + unit;
                }
                return '';
            });
        }''',
        *[Output(f'control-{name}-error', 'children') for name in FORM_SETTINGS],
        *[Input(f'control-{name}', 'value') for name in FORM_SETTINGS],
        prevent_initial_call=True,
    )


def _with_base_palette(snapshot: dict) -> dict:
    palette = snapshot.get('palette') or {}
    if not palette.get('name'):
        return snapshot
    # The show reports the section's variant (ice_breakdown); the dropdown and the pin speak base names.
    return {
        **snapshot,
        'palette': {**palette, 'name': palette['name'].removesuffix(BREAKDOWN_SUFFIX)},
    }


# The dropdown is the operator's: a poll writes it only when the show reports a different pin
# than the last poll did, so a pick survives the stale polls before its pin lands.
# Poll-driven writes to operator controls go through set_props rather than Outputs: an Output
# would make the control's own callback wait for every in-flight poll before it may run.
PALETTE_SYNC_JS = (
    """function(state) {
    const palette = state && state.palette;
    if (!palette) {
        return true;
    }
    const shown = palette.pinned ? palette.name : """
    + json.dumps(PALETTE_POLICY)
    + """;
    if (shown !== window.autoluxPaletteSynced) {
        window.autoluxPaletteSynced = shown;
        window.dash_clientside.set_props('control-palette', {value: shown});
    }
    return false;
}"""
)


def _rgb_css(rgb) -> str:
    return 'rgb({}, {}, {})'.format(*(round(channel * 255) for channel in rgb))


def _palette_options() -> list:
    def option(name, colours):
        return html.Span(
            [*(_dot(colour) for colour in colours), html.Span(name)], className='palette-option'
        )

    return [{'label': option('auto', ['transparent'] * 4), 'value': PALETTE_POLICY}] + [
        {
            'label': option(
                palette.name, [_rgb_css(getattr(palette, s)) for s in PALETTE_SWATCH_SLOTS]
            ),
            'value': palette.name,
        }
        for palette in PALETTES
    ]


def _fader(slider_id: str, value_id: str, **slider):
    return [
        html.Div(
            dcc.Slider(
                id=slider_id,
                min=0,
                max=1,
                step=0.01,
                marks=None,
                updatemode='mouseup',
                allow_direct_input=False,
                **slider,
            ),
            className='fader-track',
        ),
        html.Span('—', id=value_id, className='fader-value'),
    ]


def _build_control_card() -> list:
    return [
        html.Div('Show control', className='section-label'),
        html.Section(
            [
                html.Div(id='show-status', className='show-status'),
                html.Div(
                    [
                        html.Button(
                            '\u23f8\ufe0e',
                            id='control-toggle',
                            className='icon-button',
                            title='Play / pause',
                            disabled=True,
                        ),
                        html.Button(
                            '\u23ed\ufe0e',
                            id='control-next',
                            className='icon-button',
                            title='Next autoloop',
                            disabled=True,
                        ),
                        html.Span('Master brightness', className='control-label'),
                        html.Div(
                            _fader('control-brightness', 'control-brightness-value', disabled=True),
                            className='fader',
                        ),
                        dcc.Store(id='control-brightness-live'),
                    ],
                    className='control-row control-transport',
                ),
                dcc.Tabs(
                    id='control-mode',
                    value='auto',
                    className='mode-tabs',
                    children=[
                        dcc.Tab(
                            label=mode.upper(),
                            value=mode,
                            style=STATIC_TAB_STYLE,
                            selected_style=STATIC_SELECTED_TAB_STYLE,
                        )
                        for mode in ('auto', 'static')
                    ],
                ),
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span('Intensity', className='control-label'),
                                html.Fieldset(
                                    dcc.RadioItems(
                                        id='control-intensity',
                                        options=[
                                            {'label': word, 'value': level}
                                            for word, level in INTENSITY_LEVELS
                                        ],
                                        inline=True,
                                        className='segmented',
                                    ),
                                    id='control-intensity-set',
                                    disabled=True,
                                ),
                                html.Span(id='control-intensity-value', className='fader-value'),
                                html.Span('Colors', className='control-label'),
                                dcc.Dropdown(
                                    id='control-palette',
                                    options=_palette_options(),
                                    clearable=False,
                                    searchable=False,
                                    disabled=True,
                                    className='palette-select',
                                ),
                            ],
                            className='control-row',
                        ),
                        _build_settings(),
                    ],
                    id='auto-mode-panel',
                ),
                html.Div(_build_static_panel(), id='static-mode-panel', style={'display': 'none'}),
                html.P(id='control-status', role='status', className='control-status'),
                dcc.Store(id='control-state'),
                dcc.Store(id='control-synced'),
                dcc.Store(id='control-touched'),
                dcc.Store(id='control-mode-synced'),
            ],
            id='show-control',
        ),
    ]


STATIC_PRESETS = {
    'white': 'White',
    'warm': 'Warm',
    'red': 'Red',
    'blue': 'Blue',
    'uv': 'UV',
    'blackout': 'Blackout',
    'off': 'Off',
}
STATIC_PRESET_IDS = {f'static-preset-{name}': name for name in STATIC_PRESETS}
STATIC_BUTTONS = {'static-blacklight': 'blacklight', 'static-strobe': 'strobe'}
HEX_COLOUR_TEXT = re.compile(r'#?[0-9a-fA-F]{6}')
STATIC_TAB_STYLE = {
    'backgroundColor': 'transparent',
    'color': '#8293a6',
    'border': '0',
    'borderBottom': '2px solid transparent',
    'padding': '0 16px',
    'lineHeight': '40px',
    'fontSize': '11px',
    'letterSpacing': '.08em',
    'flex': 'none',
}
STATIC_SELECTED_TAB_STYLE = {
    **STATIC_TAB_STYLE,
    'color': '#e6edf3',
    'borderBottomColor': ACCENT,
}


def static_control_command(trigger, target, colour) -> dict | None:
    if trigger in STATIC_PRESET_IDS:
        preset = STATIC_PRESET_IDS[trigger]
        # Presets always light the whole rig, so a chosen group gets the chip as a look instead.
        if target != ALL_FIXTURES and preset in PRESET_COLOURS:
            return {
                'action': 'static',
                'kind': 'look',
                'target': target,
                'colour': colour_hex(PRESET_COLOURS[preset]),
            }
        if target != ALL_FIXTURES and preset == 'off':
            return {'action': 'static', 'kind': 'look', 'target': target, 'dimmer': 0.0}
        return {'action': 'static', 'kind': 'preset', 'preset': preset}
    if trigger in STATIC_BUTTONS:
        return {'action': 'static', 'kind': STATIC_BUTTONS[trigger]}
    if trigger == 'static-colour':
        text = (colour or '').strip()
        if not HEX_COLOUR_TEXT.fullmatch(text):
            raise ValueError(f'"{colour}" is not a #rrggbb colour')
        return {
            'action': 'static',
            'kind': 'look',
            'target': target,
            'colour': '#' + text.lstrip('#').lower(),
        }
    return None


def _build_static_panel():
    return html.Section(
        [
            html.Div(
                [
                    *[
                        html.Button(label, id=f'static-preset-{name}')
                        for name, label in STATIC_PRESETS.items()
                    ],
                    dcc.Input(
                        id='static-colour',
                        type='color',
                        value=colour_hex(PRESET_COLOURS['warm']),
                        debounce=0.3,
                        # Restoring the last chosen colour sends nothing: the callback skips initial calls.
                        persistence=True,
                        persistence_type='local',
                    ),
                ],
                className='control-row',
            ),
            html.Div(
                [
                    html.Span('Target', className='control-label'),
                    dcc.RadioItems(
                        id='static-target',
                        options=[{'label': 'All', 'value': 'all'}],
                        value='all',
                        inline=True,
                        className='segmented',
                    ),
                ],
                className='control-row',
            ),
            html.Div(
                [
                    html.Button('Blacklight', id='static-blacklight', title='UV ignores master'),
                    html.Button('Strobe', id='static-strobe', title='Capped burst; press to stop'),
                ],
                className='control-row',
            ),
            html.P(id='static-status', role='status', className='control-status'),
        ],
        id='static-control',
    )


def _register_direct_auto_controls(app) -> None:
    # Visual state flips here at click time; the bridge round trip never gates it.
    app.clientside_callback(
        """function(toggles, nexts, palette, intensity, mode, state) {
            const control = window.autoluxControl;
            const nu = window.dash_clientside.no_update;
            const triggered = (window.dash_clientside.callback_context.triggered || [])
                .map((t) => t.prop_id);
            const shown = control.view(state) || {};
            const status = 'control-status';
            if (triggered.includes('control-toggle.n_clicks')) {
                const paused = !!shown.paused;
                control.hold({paused: !paused});
                control.dispatch(control.build.toggle(paused), status, ['paused']);
                return paused ? '\u23f8\ufe0e' : '\u25b6\ufe0e';
            }
            if (triggered.includes('control-next.n_clicks')) {
                control.dispatch(control.build.next(), status);
            } else if (triggered.includes('control-palette.value')) {
                const pin = shown.palette || {};
                const current = pin.pinned ? pin.name : """
        + json.dumps(PALETTE_POLICY)
        + """;
                if (palette && palette !== current) {
                    control.dispatch(control.build.palette(palette), status);
                }
            } else if (triggered.includes('control-intensity.value')) {
                const current = (shown.settings || {}).intensity;
                if (intensity !== null && intensity !== undefined
                        && (current === undefined || Math.abs(intensity - current) >= 0.005)) {
                    control.dispatch(control.build.intensity(intensity), status);
                }
            } else if (triggered.includes('control-mode.value')) {
                if (mode && mode !== shown.mode) {
                    control.hold({mode: mode});
                    control.dispatch(control.build.mode(mode), 'static-status', ['mode']);
                }
            }
            return nu;
        }""",
        Output('control-toggle', 'children', allow_duplicate=True),
        Input('control-toggle', 'n_clicks'),
        Input('control-next', 'n_clicks'),
        Input('control-palette', 'value'),
        Input('control-intensity', 'value'),
        Input('control-mode', 'value'),
        State('control-state', 'data'),
        prevent_initial_call=True,
    )


STATIC_CHIP_IDS = (*STATIC_PRESET_IDS, *STATIC_BUTTONS)


def _register_direct_static_controls(app) -> None:
    app.clientside_callback(
        """function(...args) {
            const control = window.autoluxControl;
            const ids = """
        + json.dumps(list(STATIC_CHIP_IDS))
        + """;
            const target = args[ids.length];
            const classes = args.slice(ids.length + 1);
            const triggered = (window.dash_clientside.callback_context.triggered || [])
                .map((t) => t.prop_id.split('.')[0]);
            const chip = ids.find((id) => triggered.includes(id));
            if (!chip) {
                return classes.map(() => window.dash_clientside.no_update);
            }
            const before = classes[ids.indexOf(chip)] || '';
            control.dispatch(control.build.static(chip, target), 'static-status').then((reply) => {
                if (reply && reply.error) {
                    window.dash_clientside.set_props(chip, {className: before});
                }
            });
            if (chip === 'static-strobe') {
                return ids.map((id, i) => id === chip
                    ? (classes[i] === 'active' ? '' : 'active') : classes[i] || '');
            }
            return ids.map((id, i) => id === chip ? 'active'
                : id === 'static-strobe' ? classes[i] || '' : '');
        }""",
        *[Output(name, 'className') for name in STATIC_CHIP_IDS],
        *[Input(name, 'n_clicks') for name in STATIC_CHIP_IDS],
        State('static-target', 'value'),
        *[State(name, 'className') for name in STATIC_CHIP_IDS],
        prevent_initial_call=True,
    )


def _register_static_callbacks(app, snapshot_source, expectations, can_control, direct) -> None:
    app.clientside_callback(
        '''function(mode) {
            return [{display: mode === 'static' ? 'none' : 'block'},
                    {display: mode === 'static' ? 'block' : 'none'}];
        }''',
        Output('auto-mode-panel', 'style'),
        Output('static-mode-panel', 'style'),
        Input('control-mode', 'value'),
    )
    app.clientside_callback(
        '''function(state) {
            const ready = !!state && !!state.mode && !!state.static;
            const statics = ready ? state.static : {targets: ['all'], blacklight: false};
            const options = statics.targets.map((target) =>
                ({label: target === 'all' ? 'All' : target, value: target}));
            return [options, !statics.blacklight];
        }''',
        Output('static-target', 'options'),
        Output('static-blacklight', 'disabled'),
        Input('control-state', 'data'),
    )

    @app.callback(
        Output('control-mode-synced', 'data'),
        Input('control-state', 'data'),
        State('control-mode-synced', 'data'),
        prevent_initial_call=True,
    )
    def show_mode_on_load(state, synced):
        mode = (state or {}).get('mode')
        if synced or mode is None:
            raise dash.exceptions.PreventUpdate
        dash.set_props('control-mode', {'value': mode})
        return True

    if direct:
        _register_direct_static_controls(app)
    operator_inputs = (
        []
        if direct
        else [
            Input('control-mode', 'value'),
            *[Input(name, 'n_clicks') for name in STATIC_PRESET_IDS],
            *[Input(name, 'n_clicks') for name in STATIC_BUTTONS],
        ]
    )

    @app.callback(
        Output('static-status', 'children'),
        Output('control-state', 'data', allow_duplicate=True),
        *operator_inputs,
        Input('static-colour', 'value'),
        State('static-target', 'value'),
        State('control-state', 'data'),
        prevent_initial_call=True,
    )
    def send_static(*_):
        mode = dash.ctx.inputs.get('control-mode.value')
        colour = dash.ctx.inputs['static-colour.value']
        target = dash.ctx.states['static-target.value']
        state = dash.ctx.states.get('control-state.data') or {}
        trigger = dash.ctx.triggered_id
        if not can_control:
            raise dash.exceptions.PreventUpdate
        if trigger == 'control-mode':
            if mode == state.get('mode'):
                raise dash.exceptions.PreventUpdate
            command = {'action': 'mode', 'mode': mode}
        else:
            if trigger == 'static-colour' and not colour:
                raise dash.exceptions.PreventUpdate
            try:
                command = static_control_command(trigger, target, colour)
            except ValueError as error:
                return f'Rejected: {error}', dash.no_update
            if command is None:
                raise dash.exceptions.PreventUpdate
        reply = snapshot_source.control(command)
        if 'error' in reply:
            return f'Rejected: {reply["error"]}', dash.no_update
        if command['action'] == 'mode':
            expectations.expect(mode=command['mode'])
            return '', {**state, 'mode': command['mode']}
        return '', dash.no_update


def _metric_card(label, value, detail):
    return html.Div(
        [
            html.Div(label, className='stat-label'),
            html.Div(value, className='stat-value'),
            html.Div(detail, className='metric-detail'),
        ],
        className='performance-stat',
    )


def _latest_telemetry(snapshot, now):
    history = snapshot.get('system_history') or []
    if not history or not 0 <= now - history[-1]['at'] < TELEMETRY_STALE_SECONDS:
        return {}
    return history[-1]


def _metric_text(value, unit='', precision=0):
    return f'{value:.{precision}f}{unit}' if finite(value) else '—'


def _status_metric(label, value, detail):
    return html.Span(
        [html.Span(label, className='stat-label'), html.Span(value, className='status-value')],
        title=detail,
        className='status-metric',
    )


def _build_performance_overview(snapshot, now):
    sample = _latest_telemetry(snapshot, now)
    recent = (snapshot.get('system') or {}).get('recent') or {}
    interruptions = (
        recent['underruns'] + recent['overruns']
        if sample and all(finite(recent.get(key)) for key in ('underruns', 'overruns'))
        else None
    )
    target = _metric_text(sample.get('target_ms'), ' ms')
    return [
        _status_metric(
            'Buffer',
            _metric_text(sample.get('buffer_ms'), ' ms'),
            f'Receiver buffer · target {target} · network jitter reserve',
        ),
        _status_metric(
            'Interruptions',
            _metric_text(interruptions),
            'Audio underruns + overruns · last 10 s',
        ),
        _status_metric(
            'DMX late',
            _metric_text(sample.get('dmx_lateness_ms'), ' ms', 1),
            'DMX queue lateness · scheduled frame lateness before USB output',
        ),
        _status_metric(
            'FPS',
            _metric_text(sample.get('starfire_measured_render_frames_per_second'), precision=1),
            'Renderer FPS · measured Starfire render rate',
        ),
    ]


def _build_renderer_overview(snapshot, now):
    sample = _latest_telemetry(snapshot, now)
    return [
        _metric_card(
            'Renderer FPS',
            _metric_text(sample.get('starfire_measured_render_frames_per_second'), precision=1),
            'Between renderer health reports',
        ),
        _metric_card(
            'Last render',
            _metric_text(sample.get('starfire_last_render_duration_ms'), ' ms', 2),
            'Most recently reported frame',
        ),
        _metric_card(
            'Queued commands',
            _metric_text(sample.get('starfire_queued_commands')),
            'Awaiting the next render frame',
        ),
        _metric_card(
            'Skipped frames',
            _metric_text(sample.get('starfire_skipped_frames')),
            'Missed render deadlines · this session',
        ),
    ]


def _build_command_diagnostics(snapshot):
    streams = (snapshot.get('timing_stats') or {}).get('by_label') or {}
    if not streams:
        return html.Div('No scheduled commands yet.', className='diagnostic-empty')
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th('Stream'),
                        html.Th('Commands'),
                        html.Th('Mean error'),
                        html.Th('Max error'),
                    ]
                )
            ),
            html.Tbody(
                [
                    html.Tr(
                        [
                            html.Td(label.replace('_', ' ').title()),
                            html.Td(stream['samples']),
                            html.Td(_metric_text(stream.get('mean_error_ms'), ' ms', 1)),
                            html.Td(_metric_text(stream.get('max_error_ms'), ' ms', 1)),
                        ]
                    )
                    for label, stream in streams.items()
                ]
            ),
        ],
        className='command-diagnostics',
    )


STYLESHEET = (
    '''
* { box-sizing: border-box; }
body { margin: 0; background: #0b1018; color: #e6edf3; font-family: 'Segoe UI', sans-serif; }
.dashboard { max-width: 1680px; margin: auto; padding: 26px 32px 32px; }
.section-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; }
.masthead { display: flex; justify-content: flex-start; align-items: center; gap: 28px;
            margin-bottom: 22px; padding-bottom: 16px; border-bottom: 1px solid #1b2735; }
.masthead h1 { font-size: 22px; font-weight: 650; margin: 0; letter-spacing: -.6px; }
.dashboard-nav { flex: 0 0 auto; }
.dashboard-tabs { gap: 6px; }
.dashboard-tab:hover { color: #e6edf3 !important; background: #131c27 !important; }
.subtitle, .section-heading p { color: #9baabb; font-size: 12px; margin: 0; }
.section-heading { margin: 22px 0 12px; }
h2 { font-size: 14px; font-weight: 600; margin: 0; }
.stat-label { font-size: 10px; color: #9baabb; margin-bottom: 6px; }
.stat-value { display: flex; align-items: baseline; gap: 10px; font-size: 15px; font-weight: 600;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.status-secondary { display: flex; justify-content: space-between; align-items: baseline;
                    flex-wrap: wrap; gap: 8px 24px; padding: 8px 0;
                    border-bottom: 1px solid #1b2735; color: #9baabb; font-size: 12px; }
.show-timing span:nth-child(even) { color: #e6edf3; font-weight: 600; }
.status-metrics { display: flex; flex-wrap: wrap; gap: 6px 22px; }
.status-metric { display: inline-flex; align-items: baseline; gap: 7px; cursor: help; }
.status-metric .stat-label { margin: 0; }
.status-value { color: #e6edf3; font-weight: 600; }
.performance-overview { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
                        gap: 20px; padding: 16px 4px 2px; }
.performance-stat { min-width: 0; font-variant-numeric: tabular-nums; }
.performance-stat .stat-label { margin-bottom: 5px; }
.performance-stat .stat-value { font-size: 20px; }
.metric-detail { margin-top: 5px; color: #8293a6; font-size: 11px; }
.show-panel { border: 1px solid #273344; border-radius: 8px; overflow: hidden; background: #0d1117; }
.charts-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.chart-panel { min-width: 0; background: #111923; border: 1px solid #273344; border-radius: 8px; overflow: hidden; }
.chart-panel h3 { margin: 16px 16px 7px; font-size: 13px; font-weight: 600; }
.panel-description { margin: 0 16px 4px; color: #9baabb; font-size: 11px; line-height: 1.5; min-height: 50px; }
.telemetry-notice { padding: 10px 14px; border-left: 2px solid #f6c177; background: #29251f;
                    color: #f6c177; margin-bottom: 12px; font-size: 12px; }
.diagnostics-grid { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(340px, 1fr);
                    border-bottom: 1px solid #273344; }
.diagnostics { padding: 16px 18px; min-width: 0; }
.diagnostics + .diagnostics { border-left: 1px solid #273344; }
.diagnostics h2 { margin: 0; font-size: 14px; color: #e6edf3; }
.diagnostics-content { padding-top: 16px; font-size: 12px; }
.diagnostic-empty { color: #8293a6; }
.command-diagnostics { width: 100%; border-collapse: collapse; font-size: 11px;
                       font-variant-numeric: tabular-nums; }
.command-diagnostics th { color: #8293a6; font-weight: 500; padding-bottom: 7px; }
.command-diagnostics td { padding: 4px 0; }
.command-diagnostics th, .command-diagnostics td { text-align: right; }
.command-diagnostics th:first-child, .command-diagnostics td:first-child { text-align: left; }
.footer { display: flex; justify-content: flex-end; color: #8293a6; font-size: 11px; margin-top: 18px; }
.footer a { color: #9baabb; }
.ss-beat { position: absolute; width: 2px; margin-left: -1px;
           height: '''
    + str(BEAT_MARKER_SIZE)
    + '''px;
           left: -9999px;  /* off-screen until seated against the live range */
           background: rgba(168,218,220,0.65); }
'''
)

INDEX_TEMPLATE = (
    '''<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>'''
    + STYLESHEET
    + '''</style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>'''
)

ANIMATION_JS = (
    '''
function(sync, selected) {
    const ds = window.dash_clientside;
    const a = ds.ss = ds.ss || {};
    a.selected = selected;
    if (selected !== 'control' || document.hidden) {
        cancelAnimationFrame(a.animationFrame);
        a.running = false;
        return ds.no_update;
    }
    if (!sync) return ds.no_update;

    if (a.sync !== sync) {
        a.frozen = a.now === sync.now || sync.now < a.now;
        Object.assign(a, sync);
        a.sync = sync;
        a.at = performance.now();
    }
    if (a.running) return ds.no_update;
    a.running = true;

    const RESEAT_PX = 24;
    const HALF_MARKER_PX = '''
    + str(BEAT_MARKER_SIZE // 2)
    + ''';

    const clock = (s) => {
        const t = Math.max(0, Math.floor(s));
        const m = Math.floor(t / 60);
        return m + ':' + String(t % 60).padStart(2, '0');
    };

    const tick = (id, prefix, base, drift) => {
        const el = document.getElementById(id);
        if (!el || base == null) return;
        const text = prefix + clock(base + drift);
        if (el.textContent !== text) el.textContent = text;
    };

    const plotWidth = (gd) => {
        const fl = gd._fullLayout;
        if (!fl) return 0;
        if (fl.xaxis && fl.xaxis._length) return fl.xaxis._length;
        return fl.width - fl.margin.l - fl.margin.r;
    };

    const plotHeight = (gd) => {
        const fl = gd._fullLayout;
        if (!fl) return 0;
        if (fl.yaxis && fl.yaxis._length) return fl.yaxis._length;
        return fl.height - fl.margin.t - fl.margin.b;
    };

    // The anchor ships every drawable beat instant (the show knows each beat
    // one look-ahead before the room hears it), so the markers are DOM nodes
    // on the translated layer: they scroll on the compositor and cross the
    // fixed cursor at exactly their room instant, instead of popping in most
    // of a second late on the next figure push.
    const syncBeats = () => {
        const layer = document.getElementById('beat-layer');
        if (!layer || a.markerList === a.beats) return;
        a.markerList = a.beats || [];
        a.markers = a.markers || {};
        const keep = new Set(a.markerList.map((t) => t.toFixed(3)));
        let changed = false;
        for (const key of Object.keys(a.markers)) {
            if (keep.has(key)) continue;
            a.markers[key].remove();
            delete a.markers[key];
            changed = true;
        }
        for (const t of a.markerList) {
            const key = t.toFixed(3);
            if (a.markers[key]) continue;
            const mark = document.createElement('div');
            mark.className = 'ss-beat';
            mark.dataset.t = key;
            layer.appendChild(mark);
            a.markers[key] = mark;
            changed = true;
        }
        if (changed) a.seatedAt = null;
    };

    // Seated in axis px only when the range or the marker set moved; between
    // seats the wrapper's transform is the only thing that moves them.
    const seatBeats = (gd) => {
        if (!gd || !gd.layout || !gd.layout.xaxis) return;
        const range = gd.layout.xaxis.range;
        const width = plotWidth(gd);
        if (!range || !width) return;
        const stamp = range[0] + ':' + range[1] + ':' + width;
        if (a.seatedAt === stamp) return;
        a.seatedAt = stamp;
        const fl = gd._fullLayout;
        const pps = width / (range[1] - range[0]);
        const top = fl.margin.t + plotHeight(gd) * 0.75 - HALF_MARKER_PX;
        for (const key of Object.keys(a.markers || {})) {
            const mark = a.markers[key];
            mark.style.left = (fl.margin.l
                + (parseFloat(key) - range[0]) * pps).toFixed(2) + 'px';
            mark.style.top = top.toFixed(2) + 'px';
        }
    };

    // Scrolling by relayout re-renders every beat marker on every frame, which
    // measured 3.2 of 4.7 ms and 40% of the main thread.  Translating a wrapper
    // moves the same pixels on the compositor, and the axis is only re-seated
    // once the offset would expose the padded strip.
    const scroll = (now) => {
        const gd = document.querySelector('#timeline .js-plotly-plot');
        const el = document.getElementById('timeline-scroll');
        if (!gd || !el || !window.Plotly || !gd.layout || !gd.layout.xaxis) return;
        const range = gd.layout.xaxis.range;
        const width = plotWidth(gd);
        if (!range || !width) return;
        const pps = width / (range[1] - range[0]);
        let dx = (now + a.lead + a.pad - range[1]) * pps;
        if (dx < 0 || dx > RESEAT_PX) {
            window.Plotly.relayout(
                gd, {'xaxis.range': [now - a.span, now + a.lead + a.pad]});
            dx = 0;
        }
        el.style.transform = 'translate3d(' + (-dx).toFixed(2) + 'px,0,0)';
    };

    const frame = () => {
        if (a.selected !== 'control' || document.hidden) {
            a.running = false;
            return;
        }
        const drift = a.frozen ? 0 : Math.min(1.5, (performance.now() - a.at) / 1000);
        const now = a.now + drift;
        syncBeats();
        scroll(now);
        seatBeats(document.querySelector('#timeline .js-plotly-plot'));
        tick('room-clock', '', a.room, drift);
        tick('song-clock', '', a.song, drift);
        a.animationFrame = requestAnimationFrame(frame);
    };
    a.animationFrame = requestAnimationFrame(frame);
    return ds.no_update;
}
'''
)


_GATE_STATE_JS = '''
    const ds = window.dash_clientside = window.dash_clientside || {};
    const gate = ds.ss_gate = ds.ss_gate ||
        {inflight: {}, sent: null, behind: 0};
    const streamOf = (outputs) => {
        const list = Array.isArray(outputs) ? outputs : [outputs];
        for (let i = 0; i < list.length; i++) {
            if (!list[i]) continue;
            if (list[i].id === 'sync') return 'anchor';
        }
        return null;
    };
'''

REQUEST_PRE_JS = (
    'function(payload) {'
    + _GATE_STATE_JS
    + '''
    const stream = streamOf(payload.outputs);
    if (stream) gate.inflight[stream] = Date.now();
}'''
)

CALLBACK_RESOLVED_JS = (
    'function(callback, result) {'
    + _GATE_STATE_JS
    + '''
    const outputs = Array.isArray(callback.outputs) ? callback.outputs : [callback.outputs];
    if (result.error && outputs.some((output) => output && output.id === 'fixture-server-result')) {
        // A failed fetch bypasses Dash's runningOff cleanup.
        ds.set_props('config-panel', {'aria-busy': 'false'});
        ds.set_props('fixture-status', {children:
            'Save/reload could not be confirmed. Your draft is intact; retry or reload the saved patch.'});
    }
    const stream = streamOf(callback.outputs);
    if (!stream) return;
    delete gate.inflight[stream];
    if (result.error) {
        gate.sent = null;
        gate.behind = 0;
        gate.viewAt = gate.metricsAt = 0;
        return;
    }
    if (stream !== 'anchor') return;
    const fresh = result.data && result.data.sync && result.data.sync.data;
    if (!fresh) return;
    const landed = ds.ss || {};
    if (gate.sent !== null && landed.now !== gate.sent) {
        gate.behind += 1;
        if (gate.behind >= '''
    + str(STALE_REFRESHES)
    + ''') {
            console.warn('[viewer] ' + gate.behind + ' refreshes answered with '
                + 'fresh data the page never took (server said ' + gate.sent
                + 's, page still on ' + landed.now + 's) — the anchor is not '
                + 'reaching the layout');
            gate.behind = 0;
        }
    } else {
        gate.behind = 0;
    }
    gate.sent = fresh.now;
}'''
)

GATE_JS = (
    'function(n, selected) {'
    + _GATE_STATE_JS
    + '''
    const changed = gate.tab !== selected || gate.hidden;
    gate.tab = selected;
    gate.hidden = document.hidden;
    if (changed) gate.viewAt = gate.metricsAt = 0;
    if (selected === 'config' || document.hidden) return ds.no_update;
    const now = Date.now();
    const free = (stream) => {
        const since = gate.inflight[stream];
        if (since == null) return true;
        const waited = Date.now() - since;
        if (waited < '''
    + str(STALL_RELEASE_MS)
    + ''') return false;
        console.warn('[viewer] the ' + stream + ' refresh has not answered in '
            + (waited / 1000).toFixed(1) + 's — re-arming it');
        delete gate.inflight[stream];
        return true;
    };
    const metrics = now - (gate.metricsAt || 0) >= '''
    + str(TELEMETRY_REFRESH_SECONDS * 1000)
    + ''';
    if (!free('anchor') || (selected === 'telemetry' && !metrics)) return ds.no_update;
    const timeline = selected === 'control' && now - (gate.viewAt || 0) >= '''
    + str(VIEW_REFRESH_MS)
    + ''';
    if (timeline) gate.viewAt = now;
    if (metrics) gate.metricsAt = now;
    return {tick: n, tab: selected, timeline: timeline, metrics: metrics};
}'''
)


BLANK_SNAPSHOT = {
    'now': 0.0,
    'look_ahead_sec': 0.0,
    'is_playing': False,
    'beats': [],
    'effects': [],
    'intents': [],
    'sound_events': [],
    'current_effect': None,
    'bpm': 0.0,
    'beats_detected': 0,
    'intent': None,
    'timing_stats': {},
    'decoder': {},
    'shed': {},
}

POLL_TIMEOUT_SEC = 1.0


class SnapshotPoller:
    """The show over a socket, in place of the EventBuffer this used to hold."""

    def __init__(
        self,
        host: str = SNAPSHOT_HOST,
        port: int = snapshot_port(DEFAULT_DASHBOARD_PORT),
        timeout: float = POLL_TIMEOUT_SEC,
    ):
        self._host, self._port, self._timeout = host, port, timeout
        self.port = port
        self._connection = None
        self._last = dict(BLANK_SNAPSHOT)
        self._answering = True
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f'http://{self._host}:{self._port}{SNAPSHOT_PATH}'

    def snapshot(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return self._last
        try:
            try:
                connection = self._connection
                if connection is None:
                    connection = self._connection = http.client.HTTPConnection(
                        self._host, self._port, timeout=self._timeout
                    )
                connection.request('GET', SNAPSHOT_PATH)
                self._last = json.loads(connection.getresponse().read())
                if not self._answering:
                    logging.info('[viewer] the show is answering again')
                    self._answering = True
            except (OSError, http.client.HTTPException, ValueError) as error:
                self._connection = None
                if self._answering:
                    logging.warning(
                        f'[viewer] the show stopped answering on '
                        f'{self.url} ({error!r}) — holding the last frame'
                    )
                    self._answering = False
            return self._last
        finally:
            self._lock.release()

    def control(self, command: dict) -> dict:
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(
                'POST',
                CONTROL_PATH,
                json.dumps(command).encode(),
                {'Content-Type': 'application/json'},
            )
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException):
            return {'error': 'the show is not answering'}
        finally:
            connection.close()
        try:
            reply = json.loads(body)
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            return {'error': f'the show refused the command (HTTP {response.status})'}
        return reply


def direct_control_default() -> bool:
    return os.environ.get(DIRECT_CONTROL_ENV, '1') != '0'


def control_page_config(snapshot_source, direct: bool) -> dict:
    return {
        'direct': direct,
        'bridgePort': getattr(snapshot_source, 'port', None),
        'relayPath': RELAY_PATH,
        'holdMs': int(CONTROL_EXPECT_SECONDS * 1000),
        'allFixtures': ALL_FIXTURES,
        'presetColours': {name: colour_hex(rgb) for name, rgb in PRESET_COLOURS.items()},
    }


def build_app(
    snapshot_source,
    fixture_store: FixtureStore | None = None,
    direct_control: bool | None = None,
) -> dash.Dash:
    app = dash.Dash(
        __name__,
        title=TITLE,
        update_title=None,
        eager_loading=True,
        hooks={'request_pre': REQUEST_PRE_JS, 'callback_resolved': CALLBACK_RESOLVED_JS},
    )
    can_control = callable(getattr(snapshot_source, 'control', None))
    direct = can_control and (
        direct_control_default() if direct_control is None else direct_control
    )
    app.index_string = INDEX_TEMPLATE.replace('</style>', FIXTURE_CSS + '</style>').replace(
        '{%css%}',
        '{%css%}<script>window.autoluxControlConfig = '
        + json.dumps(control_page_config(snapshot_source, direct))
        + ';</script>',
    )
    from flask import jsonify, request
    from prometheus_client import make_wsgi_app
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    from autolux.lib.system_health import registry_for

    @app.server.before_request
    def reject_stale_dashboard_callback():
        if request.path == '/_dash-update-component' and request.method == 'POST':
            payload = request.get_json(silent=True)
            output = payload.get('output') if isinstance(payload, dict) else None
            if not isinstance(output, str) or output not in app.callback_map:
                return {'error': 'Dashboard changed. Refresh this page.'}, 409

    app.server.wsgi_app = DispatcherMiddleware(
        app.server.wsgi_app,
        {
            '/metrics': make_wsgi_app(registry_for(snapshot_source)),
        },
    )
    tab_style = {
        'backgroundColor': 'transparent',
        'color': '#8293a6',
        'border': '0',
        'borderBottom': '2px solid transparent',
        'padding': '10px 20px',
        'fontSize': '13px',
        'fontWeight': '500',
        'flex': 'none',
    }
    selected_tab_style = {**tab_style, 'color': '#e6edf3', 'borderBottomColor': '#73b6ff'}
    config_panel = (
        build_fixture_panel(app, fixture_store)
        if fixture_store is not None
        else html.Section(
            [
                html.Div(html.H2('Fixture setup'), className='section-heading'),
                html.P('Fixture setup is unavailable in this session.', className='subtitle'),
            ],
            id='config-panel',
            style={'display': 'none'},
        )
    )
    app.layout = html.Div(
        [
            html.Header(
                [
                    html.H1('Autolux'),
                    dcc.Tabs(
                        id='dashboard-tab',
                        value='control',
                        className='dashboard-tabs',
                        parent_className='dashboard-nav',
                        children=[
                            dcc.Tab(
                                label=label,
                                value=label.lower(),
                                className='dashboard-tab',
                                style=tab_style,
                                selected_style=selected_tab_style,
                            )
                            for label in ('Control', 'Telemetry', 'Config')
                        ],
                    ),
                ],
                className='masthead',
            ),
            html.Div(id='telemetry-alerts'),
            html.Section(
                [
                    *_build_control_card(),
                    html.Div(
                        [
                            html.Span('Show timeline', className='section-label'),
                            html.Div(_build_legend(), className='legend'),
                        ],
                        className='section-row',
                    ),
                    html.Section(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            dcc.Graph(
                                                id='timeline', config={'displayModeBar': False}
                                            ),
                                            html.Div(
                                                id='beat-layer',
                                                style={
                                                    'position': 'absolute',
                                                    'top': '0',
                                                    'left': '0',
                                                    'right': '0',
                                                    'bottom': '0',
                                                    'pointerEvents': 'none',
                                                },
                                            ),
                                        ],
                                        id='timeline-scroll',
                                        style={'willChange': 'transform', 'position': 'relative'},
                                    ),
                                    html.Div(
                                        id='now-cursor',
                                        style={
                                            'position': 'absolute',
                                            'top': '0',
                                            'bottom': '0',
                                            'left': NOW_CURSOR_LEFT,
                                            'width': '1px',
                                            'background': 'rgba(255,255,255,0.25)',
                                            'pointerEvents': 'none',
                                        },
                                    ),
                                ],
                                style={
                                    'position': 'relative',
                                    'overflow': 'hidden',
                                    'background': DARK_BG,
                                    'borderBottom': f'1px solid {BORDER}',
                                },
                            ),
                            html.Div(
                                [
                                    html.Section(
                                        [
                                            html.H2('Analysis diagnostics'),
                                            html.Div(id='decoder', className='diagnostics-content'),
                                        ],
                                        className='diagnostics',
                                    ),
                                    html.Section(
                                        [
                                            html.H2('Command diagnostics'),
                                            html.Div(
                                                id='command-diagnostics',
                                                className='diagnostics-content',
                                            ),
                                        ],
                                        className='diagnostics',
                                    ),
                                ],
                                className='diagnostics-grid',
                            ),
                        ],
                        className='show-panel',
                    ),
                ],
                id='control-panel',
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(id='show-timing', className='show-timing'),
                            html.Div(id='performance-metrics', className='status-metrics'),
                        ],
                        className='status-secondary',
                    ),
                    html.Div(
                        [html.H2('Starfire'), html.P('Measured rendering performance')],
                        className='section-heading',
                    ),
                    html.Div(id='renderer-metrics', className='performance-overview'),
                    html.Div(
                        [
                            html.H2('System history'),
                            html.P('Last 5 minutes · UTC · gaps indicate unavailable data'),
                        ],
                        className='section-heading',
                    ),
                    html.Div(system_metrics.build_system_health_panels(), className='charts-grid'),
                ],
                id='telemetry-panel',
                style={'display': 'none'},
            ),
            config_panel,
            html.Footer(
                [
                    html.A('Prometheus metrics', href='/metrics'),
                ],
                className='footer',
            ),
            dcc.Interval(id='tick', interval=REFRESH_MS),
            dcc.Store(id='gate'),
            dcc.Store(id='sync'),
            dcc.Store(id='timeline-data'),
            dcc.Store(id='telemetry-figures'),
            dcc.Store(id='anim'),
        ],
        className='dashboard',
    )

    app.clientside_callback(
        '''function(selected) {
            const panels = ['control', 'telemetry', 'config'].map((tab) =>
                ({display: selected === tab ? 'block' : 'none'}));
            return [...panels, {display: selected === 'config' ? 'none' : 'block'}];
        }''',
        Output('control-panel', 'style'),
        Output('telemetry-panel', 'style'),
        Output('config-panel', 'style'),
        Output('telemetry-alerts', 'style'),
        Input('dashboard-tab', 'value'),
    )

    app.clientside_callback(
        '''function(figure, selected) {
            if (selected !== 'control' || !figure) throw window.dash_clientside.PreventUpdate;
            return figure;
        }''',
        Output('timeline', 'figure'),
        Input('timeline-data', 'data'),
        Input('dashboard-tab', 'value'),
    )
    app.clientside_callback(
        '''function(figures, selected) {
            if (selected !== 'telemetry' || !figures) throw window.dash_clientside.PreventUpdate;
            return figures;
        }''',
        [Output(f'metric-{panel}', 'figure') for panel, *_ in system_metrics.PANELS],
        Input('telemetry-figures', 'data'),
        Input('dashboard-tab', 'value'),
    )

    app.clientside_callback(
        GATE_JS,
        Output('gate', 'data'),
        Input('tick', 'n_intervals'),
        Input('dashboard-tab', 'value'),
    )

    expectations = ControlExpectations()

    @app.server.route(RELAY_PATH, methods=['POST'])
    def relay_control():
        command = request.get_json(silent=True)
        if not can_control:
            return jsonify({'error': 'this dashboard cannot control the show'}), 403
        if not isinstance(command, dict):
            return jsonify({'error': 'a command must be a JSON object'}), 400
        reply = snapshot_source.control(command)
        if 'error' not in reply:
            expectations.expect(**expected_control_values(command, reply))
        return jsonify(reply)

    @app.callback(
        [
            Output('sync', 'data'),
            Output('show-status', 'children'),
            Output('show-timing', 'children'),
            Output('decoder', 'children'),
            Output('command-diagnostics', 'children'),
            Output('timeline-data', 'data'),
            Output('telemetry-alerts', 'children'),
            Output('performance-metrics', 'children'),
            Output('renderer-metrics', 'children'),
            Output('control-state', 'data'),
            Output('telemetry-figures', 'data'),
        ],
        Input('gate', 'data'),
        prevent_initial_call=True,
    )
    def refresh(requested):
        if not requested or requested.get('tab') not in ('control', 'telemetry'):
            raise dash.exceptions.PreventUpdate
        snap = expectations.apply(_with_base_palette(snapshot_source.snapshot()))
        now = time.time()
        sync = status = show_timing = decoder = commands = dash.no_update
        timeline = dash.no_update
        notices = performance = renderer = control = charts = dash.no_update
        on_control = requested['tab'] == 'control'
        due = requested.get('metrics')
        if on_control or due:
            sync = _anchor(snap)
            status = _build_show_status(snap)
            show_timing = _build_show_timing(snap)
            decoder = _build_decoder(snap)
            commands = _build_command_diagnostics(snap)
            if can_control:
                control = {
                    key: snap.get(key)
                    for key in (
                        'paused',
                        'master_brightness',
                        'settings',
                        'intent',
                        'palette',
                        'mode',
                        'static',
                    )
                }
                settings = control['settings'] or {}
                control['settings_text'] = {
                    name: setting_text(name, value) for name, value in settings.items()
                }
            if requested.get('timeline') or not on_control:
                timeline = _build_timeline(snap)
        if due:
            performance = _build_performance_overview(snap, now)
            charts = system_metrics.build_system_health_charts(snap, now=now)
            renderer = _build_renderer_overview(snap, now)
            notices = system_metrics.build_telemetry_notices(snap, now=now)
        return (
            sync,
            *(
                status,
                show_timing,
                decoder,
                commands,
                timeline,
                notices,
                performance,
                renderer,
                control,
                charts,
            ),
        )

    setting_names = json.dumps(list(FORM_SETTINGS))
    app.clientside_callback(
        '''function(state) {
            state = window.autoluxControl ? window.autoluxControl.view(state) : state;
            const ready = !!state && state.paused !== null && state.paused !== undefined;
            const settable = !ready || !state.settings;
            const skippable = ready && !state.paused && !!state.intent
                && state.intent !== 'standby';
            return [ready && state.paused ? '\u25b6\ufe0e' : '\u23f8\ufe0e', !ready,
                    !skippable, !ready || state.master_brightness === null, settable,
                    ...'''
        + setting_names
        + '''.map(() => settable)];
        }''',
        Output('control-toggle', 'children'),
        Output('control-toggle', 'disabled'),
        Output('control-next', 'disabled'),
        Output('control-brightness', 'disabled'),
        Output('control-intensity-set', 'disabled'),
        *[Output(f'control-{name}', 'disabled') for name in FORM_SETTINGS],
        Input('control-state', 'data'),
    )
    app.clientside_callback(
        '''function(state, drag, value, synced) {
            const nu = window.dash_clientside.no_update;
            if (!state || state.master_brightness === null || !state.settings) {
                throw window.dash_clientside.PreventUpdate;
            }
            synced = synced || {};
            const names = '''
        + setting_names
        + ''';
            const text = state.settings_text || {};
            const touched = Date.now() - (window.autoluxControlTouchedAt || 0) < '''
        + str(CONTROL_SYNC_HOLD_MS)
        + ''';
            const dragging = drag !== null && drag !== undefined && drag !== value;
            const focused = document.activeElement ? document.activeElement.id : '';
            const next = {...synced};
            const set = window.dash_clientside.set_props;
            if (!touched && !dragging && state.master_brightness !== synced.master) {
                next.master = state.master_brightness;
                set('control-brightness', {value: next.master});
            }
            if (!touched && state.settings.intensity !== synced.intensity) {
                next.intensity = state.settings.intensity;
                set('control-intensity', {value: next.intensity});
            }
            const fields = names.map((name) => {
                if (touched || focused === 'control-' + name || text[name] === synced[name]) {
                    return nu;
                }
                next[name] = text[name];
                return text[name];
            });
            return [...fields, next];
        }''',
        *[Output(f'control-{name}', 'value') for name in FORM_SETTINGS],
        Output('control-synced', 'data'),
        Input('control-state', 'data'),
        State('control-brightness', 'drag_value'),
        State('control-brightness', 'value'),
        State('control-synced', 'data'),
    )
    if direct:
        app.clientside_callback(
            '''function(drag, released, state) {
                const level = drag === null || drag === undefined ? released : drag;
                if (level === null || level === undefined) {
                    return '—';
                }
                const shown = window.autoluxControl.view(state) || {};
                if (level !== shown.master_brightness && level !== window.autoluxMasterSent) {
                    window.autoluxControlTouchedAt = Date.now();
                    window.autoluxMasterSent = level;
                    window.autoluxControl.hold({master_brightness: level});
                    window.autoluxControl.sendLatest(
                        'master', window.autoluxControl.build.master(level), 'control-status');
                }
                return Math.round(level * 100) + '%';
            }''',
            Output('control-brightness-value', 'children'),
            Input('control-brightness', 'drag_value'),
            Input('control-brightness', 'value'),
            State('control-state', 'data'),
            prevent_initial_call=True,
        )
        _register_direct_auto_controls(app)
    else:
        app.clientside_callback(
            '''function(level) {
                window.autoluxControlTouchedAt = Date.now();
                const label = level === null || level === undefined
                    ? '—' : Math.round(level * 100) + '%';
                const now = Date.now();
                if (level === null || level === undefined
                        || now - (window.autoluxFaderSentAt || 0) < '''
            + str(LIVE_FADER_INTERVAL_MS)
            + ''') {
                    return [label, window.dash_clientside.no_update];
                }
                window.autoluxFaderSentAt = now;
                return [label, level];
            }''',
            Output('control-brightness-value', 'children'),
            Output('control-brightness-live', 'data'),
            Input('control-brightness', 'drag_value'),
        )
    app.clientside_callback(
        '''function(level) {
            const presets = '''
        + json.dumps([level for _, level in INTENSITY_LEVELS])
        + ''';
            return level === null || level === undefined || presets.includes(level)
                ? '' : Math.round(level * 100) + '%';
        }''',
        Output('control-intensity-value', 'children'),
        Input('control-intensity', 'value'),
    )
    app.clientside_callback(
        PALETTE_SYNC_JS,
        Output('control-palette', 'disabled'),
        Input('control-state', 'data'),
    )

    operator_inputs = (
        []
        if direct
        else [
            Input('control-toggle', 'n_clicks'),
            Input('control-next', 'n_clicks'),
            Input('control-palette', 'value'),
            Input('control-brightness', 'value'),
            Input('control-brightness-live', 'data'),
            Input('control-intensity', 'value'),
        ]
    )

    @app.callback(
        Output('control-status', 'children'),
        Output('control-state', 'data', allow_duplicate=True),
        *operator_inputs,
        *[Input(f'control-{name}', 'value') for name in FORM_SETTINGS],
        State('control-state', 'data'),
        prevent_initial_call=True,
    )
    def send_control(*_):
        values = dash.ctx.inputs
        state = dash.ctx.states.get('control-state.data') or {}
        palette = values.get('control-palette.value')
        released = values.get('control-brightness.value')
        live = values.get('control-brightness-live.data')
        intensity_picked = values.get('control-intensity.value')
        settings = [values[f'control-{name}.value'] for name in FORM_SETTINGS]
        intensity = (state.get('settings') or {}).get('intensity', DEFAULT_INTENSITY)
        trigger = dash.ctx.triggered_id
        faders = {
            'control-brightness': (released, state.get('master_brightness')),
            'control-brightness-live': (live, state.get('master_brightness')),
            'control-intensity': (intensity_picked, intensity),
        }
        level, current = faders.get(trigger, (None, None))
        if trigger in faders and (
            level is None or (current is not None and abs(level - current) < 0.005)
        ):
            raise dash.exceptions.PreventUpdate
        try:
            command = control_command(
                trigger,
                state.get('paused'),
                level,
                settings,
                palette=palette,
                intensity=intensity,
                warmth=(state.get('settings') or {}).get(WARMTH),
            )
        except ValueError as error:
            return f'Rejected: {error}', dash.no_update
        if command is None or not can_control:
            raise dash.exceptions.PreventUpdate
        if command['action'] == 'settings' and command['settings'] == state.get('settings'):
            raise dash.exceptions.PreventUpdate
        shown = state.get('palette') or {}
        if command['action'] == 'palette' and palette == (
            shown.get('name') if shown.get('pinned') else PALETTE_POLICY
        ):
            raise dash.exceptions.PreventUpdate
        reply = snapshot_source.control(command)
        if 'error' in reply:
            return f'Rejected: {reply["error"]}', dash.no_update
        expected = expected_control_values(command, reply)
        expectations.expect(**expected)
        if 'paused' in expected:
            return '', {**state, 'paused': expected['paused']}
        return '', dash.no_update

    _register_settings_drawer(app)
    _register_static_callbacks(app, snapshot_source, expectations, can_control, direct)

    app.clientside_callback(
        ANIMATION_JS,
        Output('anim', 'data'),
        Input('sync', 'data'),
        Input('dashboard-tab', 'value'),
    )
    return app


def run_app(
    snapshot_source, port: int = DEFAULT_DASHBOARD_PORT, fixture_store: FixtureStore | None = None
) -> None:
    from autolux.lib.logging_config import configure_logging

    configure_logging()
    app = build_app(snapshot_source, fixture_store=fixture_store)
    with advertise_dashboard_mdns(port):
        logging.info(
            '[dashboard] http://%s:%d (listening on %s)',
            DASHBOARD_MDNS_HOSTNAME.rstrip('.'),
            port,
            DASHBOARD_BIND_ADDRESS,
        )
        app.run(host=DASHBOARD_BIND_ADDRESS, port=port, debug=False)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description='The lighting visualizer, in its own process: it polls the '
        'show for snapshots and owns every pixel and every callback.'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help=f'Dashboard port (default: {DEFAULT_DASHBOARD_PORT})',
    )
    parser.add_argument(
        '--snapshot-port',
        type=int,
        default=None,
        help='Port the show serves /snapshot on (default: --port + 1)',
    )
    args = parser.parse_args(argv)
    port = args.snapshot_port if args.snapshot_port is not None else snapshot_port(args.port)
    run_app(SnapshotPoller(port=port), port=args.port)


if __name__ == '__main__':
    main()
