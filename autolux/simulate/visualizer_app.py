import argparse
import http.client
import json
import logging
import threading
import time
from statistics import median

import dash
import plotly.graph_objects as go
from dash import Input, Output, State, dash_table, dcc, html

from autolux.lib.engine.event_buffer import STOP_PERSISTENCE_SEC
from autolux.lib.system_health import health_summary
from autolux.lib.telemetry_history import TELEMETRY_STALE_SECONDS, finite
from autolux.lib.ui_bridge import (
    DASHBOARD_BIND_ADDRESS,
    DASHBOARD_MDNS_HOSTNAME,
    DEFAULT_DASHBOARD_PORT,
    SNAPSHOT_HOST,
    SNAPSHOT_PATH,
    advertise_dashboard_mdns,
    snapshot_port,
)
from autolux.lighting.fixture_setup import FixtureStore
from autolux.simulate import system_metrics
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
    return f'{minutes}min {secs}sec' if minutes else f'{secs}sec'


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


def _build_legend() -> list:
    return [
        html.Span(
            f'■ {cfg["label"]}',
            style={'color': cfg['primary'], 'marginRight': '14px', 'fontSize': '12px'},
        )
        for cfg in INTENT_CONFIG.values()
    ]


def _build_stat_card(label, value):
    return html.Div(
        [html.Div(label, className='stat-label'), html.Div(value, className='stat-value')],
        className='show-stat',
    )


def _build_show_overview(snapshot: dict) -> list:
    bpm = _room_bpm(snapshot)
    song, room = _song_and_room(snapshot)
    song_text = '—' if song is None else _clock_text(song)
    room_text = '—' if room is None else _clock_text(room)
    cfg = _intent_config(snapshot.get('intent'))
    playing = _room_is_playing(snapshot)
    health = health_summary(snapshot)['state']
    timing = (snapshot.get('timing_stats') or {}).get('by_label') or {}
    if health == 'healthy' and any(
        stream['mean_error_ms'] > TIMING_TOLERANCE_SEC * 1000 for stream in timing.values()
    ):
        health = 'degraded'
    health_color = {'healthy': OK_COLOR, 'degraded': WARN_COLOR, 'down': system_metrics.RED}[health]
    effect = snapshot.get('current_effect') or {}
    effect_name = effect.get('name', '—')
    autoloop_name = effect_name.rsplit('/', 1)[-1].replace('_', ' ').title()
    return [
        _build_stat_card(
            'Health',
            html.Span(f'● {health.title()}', id='health-indicator', style={'color': health_color}),
        ),
        _build_stat_card(
            'Playback', html.Span('PLAYING' if playing else 'PAUSED', style={'color': MUTED})
        ),
        _build_stat_card(
            'Tempo', html.Span(f'{bpm:.0f} BPM' if bpm else '— BPM', style={'color': '#73b6ff'})
        ),
        _build_stat_card('Section', html.Span(cfg['label'], style={'color': cfg['primary']})),
        _build_stat_card('Autoloop', html.Span(autoloop_name, title=effect_name)),
        _build_stat_card('Room position', html.Span(room_text, id='room-clock')),
        _build_stat_card('Source position', html.Span(song_text, id='song-clock')),
        _build_stat_card('Show delay', html.Span(f'{snapshot.get("look_ahead_sec", 0):g} s')),
    ]


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
        _metric_card(
            'Receiver buffer',
            _metric_text(sample.get('buffer_ms'), ' ms'),
            f'Target {target} · network jitter reserve',
        ),
        _metric_card(
            'Audio interruptions', _metric_text(interruptions), 'Underruns + overruns · last 10 s'
        ),
        _metric_card(
            'DMX queue lateness',
            _metric_text(sample.get('dmx_lateness_ms'), ' ms', 1),
            'Scheduled frame lateness before USB output',
        ),
        _metric_card(
            'Renderer FPS',
            _metric_text(sample.get('starfire_measured_render_frames_per_second'), precision=1),
            'Measured Starfire render rate',
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
.overview-panel { border: 1px solid #273344; border-radius: 8px; background: #111923; }
.show-overview { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); }
.show-stat { padding: 14px 16px;
             min-width: 0; font-variant-numeric: tabular-nums; }
.show-stat:last-child { border-right: 0; }
.stat-label { font-size: 10px; color: #9baabb; margin-bottom: 8px; }
.stat-value { display: flex; gap: 14px; font-size: 15px; font-weight: 600; white-space: nowrap; }
.performance-overview { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
                        gap: 20px; padding: 16px 4px 2px; }
.performance-stat { min-width: 0; font-variant-numeric: tabular-nums; }
.performance-stat .stat-label { margin-bottom: 5px; }
.performance-stat .stat-value { font-size: 20px; }
.metric-detail { margin-top: 5px; color: #8293a6; font-size: 11px; }
.show-stat:nth-child(4) .stat-value { font-size: 15px; }
.show-stat:nth-child(4) .stat-value span { filter: brightness(1.7); }
.show-stat:nth-child(5) .stat-value { font-size: 15px; }
.show-panel { border: 1px solid #273344; border-radius: 8px; overflow: hidden; background: #0d1117; }
.show-panel .section-heading { margin: 0; padding: 14px 18px 6px; }
.legend { padding: 12px 18px 0; }
.legend > span { display: inline-block; white-space: nowrap; filter: brightness(1.7); }
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
.fixture-actions { display: flex; gap: 10px; margin-top: 16px; }
.fixture-actions button { border: 1px solid #334456; background: #162332; color: #dbe7f3;
                          padding: 8px 12px; border-radius: 5px; cursor: pointer; }
.fixture-actions button:hover { background: #223449; }
#fixture-status { color: #9baabb; font-size: 12px; }
#config-panel details { margin-top: 22px; color: #9baabb; font-size: 12px; line-height: 1.8; }
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
function(sync) {
    const ds = window.dash_clientside;
    const a = ds.ss = ds.ss || {};
    if (ds.ss_gate) delete ds.ss_gate.inflight['anchor'];
    if (!sync) return ds.no_update;

    a.frozen = a.now === sync.now || sync.now < a.now;
    Object.assign(a, sync);
    a.at = performance.now();
    if (a.running) return ds.no_update;
    a.running = true;

    const RESEAT_PX = 24;
    const HALF_MARKER_PX = '''
    + str(BEAT_MARKER_SIZE // 2)
    + ''';

    const clock = (s) => {
        const t = Math.max(0, Math.floor(s));
        const m = Math.floor(t / 60);
        return m ? m + 'min ' + (t % 60) + 'sec' : t + 'sec';
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
        const drift = a.frozen ? 0 : Math.min(1.5, (performance.now() - a.at) / 1000);
        const now = a.now + drift;
        syncBeats();
        scroll(now);
        seatBeats(document.querySelector('#timeline .js-plotly-plot'));
        tick('room-clock', '', a.room, drift);
        tick('song-clock', '', a.song, drift);
        requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
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
            if (list[i].id === 'timeline') return 'view';
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

VIEW_LANDED_JS = (
    'function(drawn) {'
    + _GATE_STATE_JS
    + '''
    delete gate.inflight['view'];
    return drawn;
}'''
)

CALLBACK_RESOLVED_JS = (
    'function(callback, result) {'
    + _GATE_STATE_JS
    + '''
    const stream = streamOf(callback.outputs);
    if (!stream) return;
    if (result.error) {
        delete gate.inflight[stream];
        gate.sent = null;
        gate.behind = 0;
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
    'function(n) {'
    + _GATE_STATE_JS
    + '''
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
    return [free('anchor') ? n : ds.no_update,
            (n % '''
    + str(VIEW_EVERY_TICKS)
    + ''' === 0 && free('view'))
                ? n : ds.no_update];
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


def _fixture_rows(patch):
    groups = patch.get('groups', {})
    rows = []
    for fixture in patch['fixtures']:
        row = {key: fixture[key] for key in ('id', 'profile', 'universe', 'address')}
        row['_original_id'] = fixture['id']
        row['groups'] = ', '.join(
            name for name, members in groups.items() if fixture['id'] in members
        )
        for axis in ('pan', 'tilt'):
            limits = fixture.get(axis, {})
            row.update(
                {
                    f'{axis}_min': limits.get('min', 0.0),
                    f'{axis}_max': limits.get('max', 1.0),
                    f'{axis}_inverted': limits.get('inverted', False),
                }
            )
        rows.append(row)
    return rows


def _fixture_patch(rows, previous):
    fixtures, groups = [], {}
    renamed = {}
    for row in rows:
        fixture = {key: row.get(key) for key in ('id', 'profile', 'universe', 'address')}
        original = row.get('_original_id')
        if original:
            if original in renamed:
                raise ValueError('A fixture row was duplicated. Add a new fixture instead.')
            renamed[original] = fixture['id']
        for key in ('universe', 'address'):
            value = fixture[key]
            if isinstance(value, float) and value.is_integer():
                fixture[key] = int(value)
        for axis in ('pan', 'tilt'):
            fixture[axis] = {
                'min': row.get(f'{axis}_min'),
                'max': row.get(f'{axis}_max'),
                'inverted': row.get(f'{axis}_inverted'),
            }
        fixtures.append(fixture)
        membership = row.get('groups') or ''
        if not isinstance(membership, str):
            raise TypeError('Group membership must be comma-separated names.')
        for name in membership.split(','):
            if name := name.strip():
                groups.setdefault(name, []).append(fixture['id'])
    for name, members in groups.items():
        prior = [renamed.get(member, member) for member in previous.get('groups', {}).get(name, [])]
        groups[name] = [member for member in prior if member in members] + [
            member for member in members if member not in prior
        ]
    return {'schema_version': previous['schema_version'], 'fixtures': fixtures, 'groups': groups}


def _build_fixture_setup(app, store):
    patch, revision = store.load()
    profiles = store.profiles()
    columns = [
        {'name': 'Fixture', 'id': 'id'},
        {'name': 'Profile', 'id': 'profile', 'presentation': 'dropdown'},
        {'name': 'Universe', 'id': 'universe', 'type': 'numeric'},
        {'name': 'Address', 'id': 'address', 'type': 'numeric'},
        {'name': 'Groups', 'id': 'groups'},
    ]
    for axis in ('pan', 'tilt'):
        columns.extend(
            [
                {'name': f'{axis.title()} min', 'id': f'{axis}_min', 'type': 'numeric'},
                {'name': f'{axis.title()} max', 'id': f'{axis}_max', 'type': 'numeric'},
                {'name': f'Invert {axis}', 'id': f'{axis}_inverted', 'presentation': 'dropdown'},
            ]
        )
    dropdown = {
        'profile': {'options': [{'label': name, 'value': name} for name in profiles]},
        **{
            f'{axis}_inverted': {
                'options': [{'label': 'No', 'value': False}, {'label': 'Yes', 'value': True}]
            }
            for axis in ('pan', 'tilt')
        },
    }
    fixtures = html.Section(
        [
            html.Div(
                [
                    html.H2('Fixture patch'),
                    html.P(
                        'Save for the next Starfire session. This page does not send light commands.'
                    ),
                ],
                className='section-heading',
            ),
            html.P(
                'Addresses start at 1; universes start at 0. Groups use comma-separated names. '
                'Movement limits are normalized from 0 to 1.',
                className='subtitle',
            ),
            dash_table.DataTable(
                id='fixture-table',
                columns=columns,
                data=_fixture_rows(patch),
                dropdown=dropdown,
                editable=True,
                row_deletable=True,
                style_table={'overflowX': 'auto', 'marginTop': '16px'},
                style_cell={
                    'backgroundColor': CARD_BG,
                    'color': '#e6edf3',
                    'borderColor': BORDER,
                    'fontFamily': 'Segoe UI',
                    'fontSize': '12px',
                    'padding': '10px',
                    'textAlign': 'left',
                },
                style_header={'fontWeight': '600', 'backgroundColor': DARK_BG},
                style_cell_conditional=[{'if': {'column_id': 'profile'}, 'minWidth': '280px'}],
                css=[
                    {
                        'selector': '.Select-value-label, .Select-input input',
                        'rule': 'color: #e6edf3 !important;',
                    },
                    {
                        'selector': '.Select-menu-outer',
                        'rule': f'background-color: {CARD_BG}; color: #e6edf3;',
                    },
                    {
                        'selector': '.Select-option',
                        'rule': f'background-color: {CARD_BG}; color: #e6edf3;',
                    },
                    {
                        'selector': '.Select-option.is-focused, .Select-option.is-selected',
                        'rule': f'background-color: {BORDER}; color: #e6edf3;',
                    },
                ],
            ),
            html.Div(
                [
                    html.Button('Add fixture', id='fixture-add'),
                    html.Button('Reload saved patch', id='fixture-reload'),
                    html.Button('Save patch', id='fixture-save'),
                ],
                className='fixture-actions',
            ),
            html.P('Changes are validated before saving.', id='fixture-status', role='status'),
            html.Details(
                [
                    html.Summary('Available fixture profiles'),
                    html.Ul(
                        [
                            html.Li([html.Strong(name), ': ', profile['notes']])
                            for name, profile in profiles.items()
                        ]
                    ),
                ]
            ),
            dcc.Store(id='fixture-patch', data=patch),
            dcc.Store(id='fixture-revision', data=revision),
        ],
        id='config-panel',
        style={'display': 'none'},
    )

    @app.callback(
        Output('fixture-table', 'data'),
        Output('fixture-patch', 'data'),
        Output('fixture-revision', 'data'),
        Output('fixture-status', 'children'),
        Input('fixture-add', 'n_clicks'),
        Input('fixture-reload', 'n_clicks'),
        Input('fixture-save', 'n_clicks'),
        State('fixture-table', 'data'),
        State('fixture-patch', 'data'),
        State('fixture-revision', 'data'),
        prevent_initial_call=True,
    )
    def edit_patch(_add, _reload, _save, rows, previous, expected_revision):
        unchanged = (dash.no_update, dash.no_update, dash.no_update)
        try:
            if dash.ctx.triggered_id == 'fixture-add':
                profile = 'generic_rgb' if 'generic_rgb' in profiles else next(iter(profiles))
                added = _fixture_rows(
                    {'fixtures': [{'id': '', 'profile': profile, 'universe': 0, 'address': 1}]}
                )
                return (
                    rows + added,
                    dash.no_update,
                    dash.no_update,
                    'New fixture added. Set its name and address before saving.',
                )
            if dash.ctx.triggered_id == 'fixture-reload':
                saved, version = store.load()
                return _fixture_rows(saved), saved, version, 'Saved patch loaded.'
            saved = _fixture_patch(rows, previous)
            version = store.save(saved, expected_revision)
            return (
                _fixture_rows(saved),
                saved,
                version,
                'Patch saved. It will be used by the next Starfire session.',
            )
        except (KeyError, AttributeError) as error:
            return *unchanged, f'Patch unchanged: saved fixture data is malformed ({error}).'
        except (TypeError, ValueError, OSError, RuntimeError) as error:
            return *unchanged, f'Patch not saved: {error}'

    return fixtures


def build_app(snapshot_source, fixture_store: FixtureStore | None = None) -> dash.Dash:
    app = dash.Dash(
        __name__,
        title=TITLE,
        eager_loading=True,
        hooks={'request_pre': REQUEST_PRE_JS, 'callback_resolved': CALLBACK_RESOLVED_JS},
    )
    app.index_string = INDEX_TEMPLATE
    from flask import request
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
        _build_fixture_setup(app, fixture_store)
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
                    html.Section(
                        html.Div(id='metrics', className='show-overview'),
                        className='overview-panel',
                    ),
                    html.Div(id='performance-metrics', className='performance-overview'),
                    html.Div(
                        [html.H2('Show timeline'), html.P('Room playback · last 30 seconds')],
                        className='section-heading',
                    ),
                    html.Section(
                        [
                            html.Div(_build_legend(), className='legend'),
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
            dcc.Store(id='view-gate'),
            dcc.Store(id='sync'),
            dcc.Store(id='anim'),
            dcc.Store(id='drawn'),
            dcc.Store(id='taken'),
        ],
        className='dashboard',
    )

    @app.callback(
        Output('control-panel', 'style'),
        Output('telemetry-panel', 'style'),
        Output('config-panel', 'style'),
        Input('dashboard-tab', 'value'),
    )
    def show_tab(selected):
        return tuple(
            {'display': 'block' if selected == tab else 'none'}
            for tab in ('control', 'telemetry', 'config')
        )

    app.clientside_callback(
        GATE_JS, [Output('gate', 'data'), Output('view-gate', 'data')], Input('tick', 'n_intervals')
    )

    latest: dict = {}

    @app.callback(
        [
            Output('sync', 'data'),
            Output('metrics', 'children'),
            Output('decoder', 'children'),
            Output('command-diagnostics', 'children'),
        ],
        Input('gate', 'data'),
        prevent_initial_call=True,
    )
    def refresh(_):
        snap = latest['snapshot'] = snapshot_source.snapshot()
        return (
            _anchor(snap),
            _build_show_overview(snap),
            _build_decoder(snap),
            _build_command_diagnostics(snap),
        )

    @app.callback(
        [Output('timeline', 'figure'), Output('drawn', 'data')],
        Input('view-gate', 'data'),
        prevent_initial_call=True,
    )
    def refresh_view(tick):
        snap = latest.get('snapshot') or snapshot_source.snapshot()
        if snap is latest.get('drawn'):
            return dash.no_update, tick
        latest['drawn'] = snap
        return _build_timeline(snap), tick

    @app.callback(
        [Output(f'metric-{panel}', 'figure') for panel, *_ in system_metrics.PANELS]
        + [
            Output('telemetry-alerts', 'children'),
            Output('performance-metrics', 'children'),
            Output('renderer-metrics', 'children'),
        ],
        Input('sync', 'data'),
        prevent_initial_call=True,
    )
    def refresh_telemetry(_):
        # Reuse the one show snapshot and limit chart rendering to once per second.
        now = time.time()
        if now - latest.get('charts_at', 0) < TELEMETRY_REFRESH_SECONDS:
            return [dash.no_update] * (len(system_metrics.PANELS) + 3)
        latest['charts_at'] = now
        snap = latest.get('snapshot') or {}
        return system_metrics.build_system_health_charts(snap, now=now) + [
            system_metrics.build_telemetry_notices(snap, now=now),
            _build_performance_overview(snap, now),
            _build_renderer_overview(snap, now),
        ]

    app.clientside_callback(ANIMATION_JS, Output('anim', 'data'), Input('sync', 'data'))
    app.clientside_callback(VIEW_LANDED_JS, Output('taken', 'data'), Input('drawn', 'data'))
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
