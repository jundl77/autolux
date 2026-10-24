"""Bounded, detached measurements for the dashboard's five-minute window."""

from collections import deque
import math

HISTORY_WINDOW_SECONDS = 300
TELEMETRY_STALE_SECONDS = 3

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


class TelemetryHistory:
    """Owned by EventBuffer; callers hold its lock. One sample per native report."""

    def __init__(self):
        self.samples = deque(maxlen=HISTORY_WINDOW_SECONDS + 1)
        self.session = None

    def append(self, state, timing):
        stamp = state.get('sample_timestamp')
        if not finite(stamp):
            return
        session = state.get('session')
        if session != self.session:
            self.samples.clear()
            self.session = session
        if self.samples and stamp <= self.samples[-1]['at']:
            return
        sample = {'at': stamp}
        for key, (node, field, scale) in TELEMETRY_FIELD_SCALES.items():
            age = state.get(f'{node}_age_seconds')
            fresh = finite(age) and 0 <= age < TELEMETRY_STALE_SECONDS
            value = (state.get(node) or {}).get(field) if fresh else None
            sample[key] = value * scale if finite(value) else None
        sample['command_error_ms'] = timing.get('mean_error_ms')
        self.samples.append(sample)
        while self.samples and self.samples[0]['at'] < stamp - HISTORY_WINDOW_SECONDS:
            self.samples.popleft()

    def snapshot(self):
        return [dict(sample) for sample in self.samples]
