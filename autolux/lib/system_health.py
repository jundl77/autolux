"""Small health summary and Prometheus export shared by the viewer and CLI."""

import math
import time

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from autolux.lib.telemetry_history import TELEMETRY_STALE_SECONDS, finite, renderer_is_fresh

MIN_BUFFER_TARGET_RATIO = 0.25
MAX_BUFFER_TARGET_RATIO = 3
MAX_DMX_LATENESS_SECONDS = 0.05
AUDIO_INPUT_STALE_SECONDS = 1
DMX_INPUT_STALE_SECONDS = 3
MILLISECONDS_PER_SECOND = 1000


def health_summary(snapshot, *, now=None):
    system = snapshot.get('system') or {}
    if not system:
        return {'state': 'down', 'cards': []}
    now = time.time() if now is None else now
    age = max(0, now - system.get('updated_at', 0))
    tx, rx = system.get('tx') or {}, system.get('rx') or {}
    tx_age = system.get('tx_age_seconds')
    rx_age = system.get('rx_age_seconds')
    tx_fresh = finite(tx_age) and tx_age + age < TELEMETRY_STALE_SECONDS
    rx_fresh = finite(rx_age) and rx_age + age < TELEMETRY_STALE_SECONDS
    link = tx_fresh and rx_fresh and tx.get('link_up') and rx.get('link_up')
    shed = snapshot.get('shed') or {}
    fault = shed.get('fault') or (
        'Holding the last intent' if shed.get('level', 'NONE') != 'NONE' else None
    )
    recent = system.get('recent') or {}
    glitches = sum(recent.values())
    audio_detail = (
        f"Last 10 s: {int(recent.get('underruns', 0))} underruns · "
        f"{int(recent.get('overruns', 0))} overruns · {int(recent.get('lost_frames', 0))} lost frames"
    )
    if recent.get('resyncs'):
        audio_detail += f" · {int(recent['resyncs'])} capture resyncs"
        gap = tx.get('capture_last_gap_seconds')
        if finite(gap):
            audio_detail += f' (last gap {gap * MILLISECONDS_PER_SECOND:.0f} ms)'
    if recent.get('dropped_frames'):
        audio_detail += f" · {int(recent['dropped_frames'])} dropped frames"
    if recent.get('latency_recoveries'):
        audio_detail += f" · {int(recent['latency_recoveries'])} latency recovery drops"
    peak = rx.get('audio_signal_peak')
    if peak is not None:
        audio_detail += (
            f" · peak {20 * math.log10(peak):.0f} dBFS" if peak > 0 else ' · digital silence'
        )
    fill, target = rx.get('audio_buffer_seconds'), rx.get('buffer_target_seconds')
    buffer_available = rx_fresh and finite(fill)
    target_available = finite(target) and target > 0
    buffer_detail = (
        f'Target {target * MILLISECONDS_PER_SECOND:.0f} ms'
        if target_available
        else 'Target unavailable'
    )
    correction = rx.get('clock_correction_ppm')
    if finite(correction):
        buffer_detail += f' · clock correction {correction:.0f} ppm'
    input_age = rx.get('audio_input_age_seconds')
    audio_fresh = rx_fresh and input_age is not None and input_age + age < AUDIO_INPUT_STALE_SECONDS
    dmx_age = tx.get('dmx_input_age_seconds')
    renderer = system.get('starfire') or {}
    renderer_ready = renderer_is_fresh(system, age) and not renderer.get('script_error')
    lights = (
        link
        and renderer_ready
        and rx.get('dmx_connected')
        and dmx_age is not None
        and dmx_age + age < DMX_INPUT_STALE_SECONDS
    )
    lateness = rx.get('dmx_lateness_seconds')
    cards = [
        (
            'Autolux',
            'Stopped' if age >= TELEMETRY_STALE_SECONDS else 'Degraded' if fault else 'Running',
            str(fault or 'Analysis and controller'),
            age < TELEMETRY_STALE_SECONDS and not fault,
        ),
        (
            'Radius link',
            'Connected' if link else 'Waiting / offline',
            '10 s heartbeat in both directions',
            bool(link),
        ),
        (
            'Audio',
            'No input' if not audio_fresh else 'Glitches' if glitches else 'Stable',
            audio_detail,
            audio_fresh and not glitches,
        ),
        (
            'Audio buffer',
            f'{fill * MILLISECONDS_PER_SECOND:.0f} ms' if buffer_available else 'Unavailable',
            buffer_detail,
            buffer_available
            and target_available
            and target * MIN_BUFFER_TARGET_RATIO <= fill <= target * MAX_BUFFER_TARGET_RATIO,
        ),
        (
            'Lighting',
            'Ready' if lights else 'Waiting / offline',
            str(
                renderer.get('script_error')
                or (
                    'Enttec output and Starfire renderer'
                    if renderer_ready
                    else 'Starfire telemetry unavailable'
                )
            ),
            bool(lights),
        ),
        (
            'Audio / lights',
            f'{lateness * MILLISECONDS_PER_SECOND:.0f} ms'
            if lights and lateness is not None
            else 'Unavailable',
            'Lighting scheduling lateness',
            bool(lights and finite(lateness) and lateness < MAX_DMX_LATENESS_SECONDS),
        ),
    ]
    if age >= TELEMETRY_STALE_SECONDS or not link:
        state = 'down'
    elif all(c[3] for c in cards) and not shed.get('sheds_per_min', 0):
        state = 'healthy'
    else:
        state = 'degraded'
    return {'state': state, 'cards': cards}


# Explicit, bounded metric set. Native counters are cumulative for the process lifetime.
GAUGES = {
    'audio_signal_peak': 'Peak received audio amplitude during the receiver telemetry interval.',
    'audio_max_gap_seconds': 'Largest audio delivery gap during the receiver telemetry interval.',
    'audio_buffer_seconds': 'Queued audio duration.',
    'buffer_target_seconds': 'Receiver jitter buffer target.',
    'heartbeat_age_seconds': 'Time since the last peer heartbeat.',
    'clock_correction_ppm': 'Playback sample clock correction in parts per million.',
    'dmx_lateness_seconds': 'Lighting scheduling lateness against audible audio.',
}
COUNTERS = {
    'capture_overruns_total': 'Capture device overruns.',
    'capture_discontinuities_total': 'Capture overruns bridged by re-anchoring the timeline.',
    'queue_dropped_frames_total': 'Audio frames dropped by a full transmitter queue.',
    'underruns_total': 'Receiver audio starvation episodes.',
    'device_underruns_total': 'Audio output device underruns.',
    'lost_frames_total': 'Missing audio frames observed at the receiver.',
    'concealed_frames_total': 'Audio frames replaced with silence.',
    'overflow_frames_total': 'Audio frames rejected by a full receiver queue.',
    'late_packets_total': 'Audio packets arriving after their accepted media position.',
    'latency_trims_total': 'Audio packets discarded to recover excessive latency.',
    'send_failures_total': 'Failed transport writes.',
    'invalid_packets_total': 'Rejected transport messages.',
    'dmx_dropped_total': 'Dropped or out-of-order lighting packets.',
    'dmx_frames_total': 'Lighting frames written to the Enttec output.',
}


class HealthCollector:
    def __init__(self, source):
        self.source = source

    def collect(self):
        snapshot = self.source.snapshot()
        system = snapshot.get('system') or {}
        age = max(0, time.time() - system.get('updated_at', 0))
        yield GaugeMetricFamily(
            'autolux_up',
            'Controller health updates are fresh.',
            value=int(age < TELEMETRY_STALE_SECONDS),
        )
        shed = snapshot.get('shed') or {}
        yield GaugeMetricFamily(
            'autolux_analysis_degraded',
            'Analysis is recovering from a fault.',
            value=int(bool(shed.get('fault'))),
        )
        renderer = system.get('starfire') or {}
        renderer_fresh = renderer_is_fresh(system, age)
        yield GaugeMetricFamily(
            'starfire_up',
            'Renderer heartbeat is fresh and its script is healthy.',
            value=int(renderer_fresh and not renderer.get('script_error')),
        )
        if renderer_fresh:
            for key, name, description, factory, scale in (
                (
                    'rendered_frames',
                    'starfire_rendered_frames_total',
                    'Frames rendered.',
                    CounterMetricFamily,
                    1,
                ),
                (
                    'skipped_frames',
                    'starfire_skipped_frames_total',
                    'Render deadlines skipped after a scheduling stall.',
                    CounterMetricFamily,
                    1,
                ),
                (
                    'queued_commands',
                    'starfire_queued_commands',
                    'Commands awaiting the next render frame.',
                    GaugeMetricFamily,
                    1,
                ),
                (
                    'last_render_ms',
                    'starfire_last_render_duration_seconds',
                    'Duration of the last completed render.',
                    GaugeMetricFamily,
                    1 / MILLISECONDS_PER_SECOND,
                ),
            ):
                value = renderer.get(key)
                if finite(value) and value >= 0:
                    yield factory(name, description, value=value * scale)
            history = snapshot.get('system_history') or []
            latest = history[-1] if history else {}
            for key, name, description in (
                (
                    'starfire_measured_render_frames_per_second',
                    'starfire_measured_render_frames_per_second',
                    'Rendered frame count divided by elapsed renderer time between reports.',
                ),
                (
                    'starfire_missed_render_deadlines_per_second',
                    'starfire_missed_render_deadlines_per_second',
                    'Skipped render deadlines per second between renderer reports.',
                ),
            ):
                value = latest.get(key)
                if finite(value) and latest.get('at') == system.get('sample_timestamp'):
                    yield GaugeMetricFamily(name, description, value=value)
        timing = (snapshot.get('timing_stats') or {}).get('mean_error_ms')
        if timing is not None:
            yield GaugeMetricFamily(
                'autolux_command_timing_error_seconds',
                'Mean absolute command scheduling error.',
                value=timing / MILLISECONDS_PER_SECOND,
            )
        up = GaugeMetricFamily(
            'radius_up',
            'Endpoint telemetry is fresh and its peer heartbeat is healthy.',
            labels=['node'],
        )
        for node in ('tx', 'rx'):
            info = system.get(node) or {}
            measured_age = system.get(f'{node}_age_seconds')
            fresh = measured_age is not None and measured_age + age < TELEMETRY_STALE_SECONDS
            up.add_metric([node], int(fresh and bool(info.get('up')) and bool(info.get('link_up'))))
        yield up
        for mapping, factory in ((GAUGES, GaugeMetricFamily), (COUNTERS, CounterMetricFamily)):
            for key, description in mapping.items():
                metric = factory(f'radius_{key}', description, labels=['node'])
                for node in ('tx', 'rx'):
                    value = (system.get(node) or {}).get(key)
                    measured_age = system.get(f'{node}_age_seconds')
                    if measured_age is None or measured_age + age >= TELEMETRY_STALE_SECONDS:
                        continue  # Missing measurements must not look like healthy zeros.
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        metric.add_metric([node], value)
                yield metric


def registry_for(source):
    registry = CollectorRegistry()
    registry.register(HealthCollector(source))
    return registry


def metrics(source):
    return generate_latest(registry_for(source))
