"""Bounded, detached measurements for the dashboard's five-minute window."""

import math
from collections import deque

HISTORY_WINDOW_SECONDS = 300
TELEMETRY_STALE_SECONDS = 3
RENDERER_TELEMETRY_STALE_SECONDS = 30

RENDERER_FIELDS = {
    'starfire_last_render_duration_ms': 'last_render_ms',
    'starfire_skipped_frames': 'skipped_frames',
    'starfire_queued_commands': 'queued_commands',
}
RENDERER_RATES = {
    'starfire_measured_render_frames_per_second': 'rendered_frames',
    'starfire_missed_render_deadlines_per_second': 'skipped_frames',
}

# UI units are explicit here; Prometheus continues exporting native SI units.
TELEMETRY_FIELD_SCALES = {
    'buffer_ms': ('rx', 'audio_buffer_seconds', 1000),
    'target_ms': ('rx', 'buffer_target_seconds', 1000),
    'delivery_gap_ms': ('rx', 'audio_max_gap_seconds', 1000),
    'signal_peak': ('rx', 'audio_signal_peak', 1),
    'dmx_lateness_ms': ('rx', 'dmx_lateness_seconds', 1000),
    'starvations': ('rx', 'underruns_total', 1),
    'device_underruns': ('rx', 'device_underruns_total', 1),
    'capture_overruns': ('tx', 'capture_overruns_total', 1),
    'lost_frames': ('rx', 'lost_frames_total', 1),
    'rx_dropped_frames': ('rx', 'overflow_frames_total', 1),
    'tx_dropped_frames': ('tx', 'queue_dropped_frames_total', 1),
    'dmx_frames': ('rx', 'dmx_frames_total', 1),
    'dmx_dropped': ('rx', 'dmx_dropped_total', 1),
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def renderer_is_fresh(system, age):
    renderer = system.get('starfire') or {}
    measured_age = renderer.get('age_seconds')
    return (
        finite(age)
        and 0 <= age < TELEMETRY_STALE_SECONDS
        and finite(measured_age)
        and 0 <= measured_age
        and measured_age + age < RENDERER_TELEMETRY_STALE_SECONDS
    )


class TelemetryHistory:
    """Owned by EventBuffer; callers hold its lock. One sample per native report."""

    def __init__(self):
        self.samples = deque(maxlen=HISTORY_WINDOW_SECONDS + 1)
        self.session = None
        self._reset_renderer()

    def _reset_renderer(self):
        self._renderer_report = None
        self._renderer_rates = dict.fromkeys(RENDERER_RATES)

    def _renderer_sample(self, state):
        sample = dict.fromkeys(RENDERER_FIELDS)
        if not renderer_is_fresh(state, 0):
            self._reset_renderer()
            return sample | self._renderer_rates
        renderer = state['starfire']
        for key, field in RENDERER_FIELDS.items():
            value = renderer.get(field)
            sample[key] = value if finite(value) and value >= 0 else None
        elapsed = renderer.get('elapsed_seconds')
        counters = [renderer.get(field) for field in RENDERER_RATES.values()]
        if not all(finite(value) and value >= 0 for value in [elapsed, *counters]):
            self._reset_renderer()
            return sample | self._renderer_rates
        current = (elapsed, *counters)
        previous = self._renderer_report
        if previous != current:
            self._renderer_rates = dict.fromkeys(RENDERER_RATES)
            if previous is not None:
                interval = elapsed - previous[0]
                increments = [value - before for value, before in zip(counters, previous[1:])]
                if 0 < interval < RENDERER_TELEMETRY_STALE_SECONDS and all(
                    value >= 0 for value in increments
                ):
                    self._renderer_rates = {
                        key: increment / interval
                        for key, increment in zip(RENDERER_RATES, increments)
                    }
            self._renderer_report = current
        return sample | self._renderer_rates

    def append(self, state, timing):
        stamp = state.get('sample_timestamp')
        if not finite(stamp):
            return
        session = state.get('session')
        if session != self.session:
            self.samples.clear()
            self.session = session
            self._reset_renderer()
        if self.samples and stamp <= self.samples[-1]['at']:
            return
        if self.samples and stamp - self.samples[-1]['at'] >= RENDERER_TELEMETRY_STALE_SECONDS:
            self._reset_renderer()
        sample = {'at': stamp}
        for key, (node, field, scale) in TELEMETRY_FIELD_SCALES.items():
            age = state.get(f'{node}_age_seconds')
            fresh = finite(age) and 0 <= age < TELEMETRY_STALE_SECONDS
            value = (state.get(node) or {}).get(field) if fresh else None
            sample[key] = value * scale if finite(value) else None
        sample['command_error_ms'] = timing.get('mean_error_ms')
        sample.update(self._renderer_sample(state))
        self.samples.append(sample)
        while self.samples and self.samples[0]['at'] < stamp - HISTORY_WINDOW_SECONDS:
            self.samples.popleft()

    def snapshot(self):
        return [dict(sample) for sample in self.samples]
