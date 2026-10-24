"""Translate current native measurements into a bounded health snapshot."""

from collections import deque
import json
from pathlib import Path
import time


class HealthMonitor:
    def __init__(self, path, session=None):
        self.path, self.session = Path(path), session
        self._history = deque(maxlen=120)
        self._last = {}
        self._read_at = 0

    def snapshot(self):
        now = time.time()
        if now - self._read_at >= 0.5:
            try:
                value = json.loads(self.path.read_text())
                if self.session is not None and value.get('session') != self.session:
                    raise ValueError('Native health belongs to a different session')
                self._last = value
            except (OSError, json.JSONDecodeError):
                pass
            self._read_at = now
        report = self._last
        tx, rx = report.get('tx') or {}, report.get('rx') or {}
        age = max(0, now - report['timestamp']) if 'timestamp' in report else None
        rx_age = report.get('rx_age_seconds')
        counters = {
            'underruns': rx.get('underruns_total', 0) + rx.get('device_underruns_total', 0),
            'overruns': tx.get('capture_overruns_total', 0),
            'lost_frames': rx.get('lost_frames_total', 0),
            'dropped_frames': tx.get('queue_dropped_frames_total', 0)
            + rx.get('overflow_frames_total', 0),
            'latency_recoveries': rx.get('latency_trims_total', 0),
        }
        if age is not None and age < 3:
            if not self._history or now - self._history[-1][0] >= 0.5:
                self._history.append((now, counters))
            while len(self._history) > 1 and self._history[1][0] < now - 10:
                self._history.popleft()
        baseline = self._history[0][1] if self._history else counters
        return {
            'updated_at': now,
            'tx': tx,
            'rx': rx,
            'tx_age_seconds': age,
            'rx_age_seconds': None if age is None or rx_age is None else age + rx_age,
            'recent': {key: max(0, value - baseline[key]) for key, value in counters.items()},
        }
