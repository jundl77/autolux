"""Time-series panels for the existing Autolux dashboard."""

from datetime import datetime, timezone
import math
import time

from dash import dcc, html
import plotly.graph_objects as go

from autolux.lib.telemetry_history import TELEMETRY_STALE_SECONDS, HISTORY_WINDOW_SECONDS, finite
from autolux.lib.system_health import AUDIO_INPUT_STALE_SECONDS, DMX_INPUT_STALE_SECONDS

BLUE, TEAL, AMBER, RED = '#73b6ff', '#59d5be', '#f6c177', '#ef818c'
PANELS = (
    (
        'buffer',
        'Receiver audio buffer',
        'Queued audio absorbs uneven network delivery. The target adds playback latency; lights follow the same audio clock.',
        'ms',
        (
            ('buffer_ms', 'Queued', BLUE, False),
            ('target_ms', 'Target', TEAL, False),
            ('delivery_gap_ms', 'Max delivery gap', AMBER, False),
        ),
    ),
    (
        'signal',
        'Received audio level',
        'Peak per receiver report, before the output device. 0 dBFS is full scale; −90 is the display floor, including silence.',
        'dBFS',
        (('signal_peak', 'Audio peak', TEAL, False),),
    ),
    (
        'interruptions',
        'Audio interruptions',
        'New events per second. Queue starvation, output-device underruns and capture overruns identify where audio stalls.',
        'events/s',
        (
            ('starvations', 'RX queue', RED, True),
            ('device_underruns', 'Output device', AMBER, True),
            ('capture_overruns', 'TX capture', BLUE, True),
        ),
    ),
    (
        'loss',
        'Audio loss & overflow',
        'Missing audio frames observed at RX and frames rejected by full queues, per second. Missing frames can include TX drops.',
        'frames/s',
        (
            ('lost_frames', 'Missing at RX', RED, True),
            ('tx_dropped_frames', 'TX overflow', AMBER, True),
            ('rx_dropped_frames', 'RX overflow', BLUE, True),
        ),
    ),
    (
        'timing',
        'Lighting & command timing',
        'DMX lateness against audible audio; controller mean scheduling error for its retained commands. Software timing only.',
        'ms',
        (
            ('dmx_lateness_ms', 'DMX lateness', TEAL, False),
            ('command_error_ms', 'Controller mean', BLUE, False),
        ),
    ),
    (
        'lighting',
        'Lighting delivery',
        'Frames written to the Enttec output per second. Drops are rejected or out-of-order lighting frames.',
        'frames/s',
        (('dmx_frames', 'DMX output', TEAL, True), ('dmx_dropped', 'Dropped', RED, True)),
    ),
)


def build_metric_series(history, key, *, rate=False):
    """Preserve gaps and counter resets; never interpolate an unknown interval."""
    x, y = [], []
    previous = None
    for sample in history:
        stamp, value = sample['at'], sample.get(key)
        gap = stamp - previous['at'] if previous else None
        if gap is not None and gap > TELEMETRY_STALE_SECONDS:
            x.append(datetime.fromtimestamp(previous['at'] + 0.001, timezone.utc))
            y.append(None)
        if rate:
            before = previous.get(key) if previous else None
            value = (
                (value - before) / gap
                if finite(value)
                and finite(before)
                and 0 < gap <= TELEMETRY_STALE_SECONDS
                and value >= before
                else None
            )
        elif key == 'signal_peak':
            value = (
                max(-90, 20 * math.log10(value))
                if finite(value) and value > 0
                else (-90 if value == 0 else None)
            )
        x.append(datetime.fromtimestamp(stamp, timezone.utc))
        y.append(value if finite(value) else None)
        previous = sample
    return x, y


def build_system_health_charts(snapshot, *, now=None):
    now = time.time() if now is None else now
    history = [
        s
        for s in snapshot.get('system_history', [])
        if now - HISTORY_WINDOW_SECONDS <= s['at'] <= now
    ]
    results = []
    for panel, _, _, unit, traces in PANELS:
        fig = go.Figure()
        measured = False
        for key, name, color, rate in traces:
            x, y = build_metric_series(history, key, rate=rate)
            measured |= any(value is not None for value in y)
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=y,
                    name=name,
                    mode='lines',
                    connectgaps=False,
                    line={
                        'color': color,
                        'width': 1.8,
                        'dash': 'dot' if key == 'target_ms' else 'solid',
                        'shape': 'hv' if rate else 'linear',
                    },
                    hovertemplate=f'%{{y:.1f}} {unit}<extra>{name}</extra>',
                )
            )
        fig.update_layout(
            height=218,
            margin=dict(l=48, r=12, t=36, b=32),
            paper_bgcolor='#111923',
            plot_bgcolor='#111923',
            font=dict(family='Segoe UI, sans-serif', size=11, color='#9baabb'),
            legend=dict(orientation='h', x=0, y=1.22, font=dict(size=10)),
            xaxis=dict(
                type='date',
                range=[
                    datetime.fromtimestamp(now - HISTORY_WINDOW_SECONDS, timezone.utc),
                    datetime.fromtimestamp(now, timezone.utc),
                ],
                tickformat='%H:%M:%S',
                nticks=4,
                showgrid=False,
                zeroline=False,
            ),
            yaxis=dict(
                title=dict(text=unit, font=dict(size=10)),
                gridcolor='#223040',
                zerolinecolor='#334456',
                rangemode='tozero',
            ),
            hovermode='x unified',
            uirevision=f'{panel}-{(snapshot.get("system") or {}).get("session")}',
        )
        if panel == 'signal':
            fig.update_yaxes(range=[-90, 0], dtick=30)
        if not measured:
            fig.add_annotation(
                text='No measurements in this window',
                x=0.5,
                y=0.5,
                xref='paper',
                yref='paper',
                showarrow=False,
                font=dict(color='#9baabb'),
            )
        results.append(fig)
    return results


def build_system_health_panels():
    return [
        html.Section(
            [
                html.H3(title),
                html.P(description, className='panel-description'),
                dcc.Graph(
                    id=f'metric-{panel}',
                    responsive=True,
                    style={'height': '218px'},
                    config={'displayModeBar': False},
                ),
            ],
            className='chart-panel',
        )
        for panel, title, description, _, _ in PANELS
    ]


def build_telemetry_notices(snapshot, *, now=None):
    now = time.time() if now is None else now
    system = snapshot.get('system') or {}
    messages = []
    age = max(0, now - system.get('updated_at', now))
    if not system:
        messages.append(
            'No Radius telemetry yet. Transport graphs will populate when measurements arrive.'
        )
    elif age >= TELEMETRY_STALE_SECONDS:
        messages.append(
            f'Controller telemetry is {age:.0f} s old. Showing the last received show frame.'
        )
    else:
        for node, name in [('tx', 'Transmitter'), ('rx', 'Receiver')]:
            measured_age = system.get(f'{node}_age_seconds')
            info = system.get(node) or {}
            if not finite(measured_age) or measured_age + age >= TELEMETRY_STALE_SECONDS:
                messages.append(f'{name} telemetry unavailable; gaps are not zero measurements.')
            elif not info.get('link_up'):
                messages.append(f'{name} heartbeat link is down.')
            elif node == 'rx' and not info.get('dmx_connected'):
                messages.append('Enttec output is disconnected.')
            if (
                node == 'rx'
                and finite(measured_age)
                and measured_age + age < TELEMETRY_STALE_SECONDS
            ):
                input_age = info.get('audio_input_age_seconds')
                if not finite(input_age) or input_age + age >= AUDIO_INPUT_STALE_SECONDS:
                    messages.append('No recent audio packets at the receiver.')
        tx = system.get('tx') or {}
        dmx_age = tx.get('dmx_input_age_seconds')
        tx_age = system.get('tx_age_seconds')
        if (
            finite(tx_age)
            and tx_age + age < TELEMETRY_STALE_SECONDS
            and (not finite(dmx_age) or dmx_age + age >= DMX_INPUT_STALE_SECONDS)
        ):
            messages.append('No recent lighting input from Starfire.')
    shed = snapshot.get('shed') or {}
    fault = shed.get('fault')
    if fault or shed.get('level', 'NONE') != 'NONE':
        messages.append(f'Analysis is holding the last intent{f": {fault}" if fault else "."}')
    if shed.get('sheds_per_min', 0):
        messages.append(f'Analysis skips: {shed["sheds_per_min"]}/min.')
    return [html.Div(message, className='telemetry-notice', role='status') for message in messages]
