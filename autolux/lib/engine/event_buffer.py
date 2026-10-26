"""Thread-safe store of pipeline events — the only state the Dash thread shares."""

import copy
import math
import threading
from collections import deque

from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.telemetry_history import TelemetryHistory

CLASSIFIER_TRIGGER = 'classifier'
SILENCE_TRIGGER = 'silence'
STANDBY_INTENT = 'standby'

STOP_PERSISTENCE_SEC = 2.0


class EventBuffer:
    SNAPSHOT_WINDOW_SEC = 35.0

    def __init__(
        self, window_sec: float = 60.0, clock: Clock = SYSTEM_CLOCK, look_ahead_sec: float = 0.0
    ):
        self._lock = threading.Lock()
        self._window_sec = window_sec
        self._clock = clock
        self._look_ahead_sec = look_ahead_sec
        self._start_time: float | None = None
        self._end_time: float | None = None
        self._is_playing: bool = False
        self._beats: deque[dict] = deque(maxlen=None if window_sec == float('inf') else 3000)
        self._beats_detected: int = 0
        self._effects: list[dict] = []
        self._intents: list[dict] = []
        self._timing_log: list[dict] = []
        self._current_intent: str | None = None
        self._current_trigger: str | None = None
        self._sound_events: list[dict] = []
        self._decoder_state: dict = {}
        self._shed_state: dict = {}
        self._control_state: dict = {
            'paused': None,
            'master_brightness': None,
            'settings': None,
            'mode': None,
            'static': None,
        }
        self._palette: dict | None = None
        self._special: dict | None = None
        self._special_groups: dict[str, list[str]] = {}
        self._intense_at: float | None = None
        self._system_health: dict = {}
        self._system_history = TelemetryHistory()

    def start(self) -> None:
        with self._lock:
            self._start_time = self._clock.monotonic()

    def mark_end(self) -> None:
        with self._lock:
            self._end_time = self._clock.monotonic()

    def elapsed(self) -> float:
        return self._now()

    def _now(self) -> float:
        if self._start_time is None:
            return 0.0
        now = self._clock.monotonic()
        if self._end_time is not None and now > self._end_time:
            now = self._end_time
        return now - self._start_time

    def add_beat(self, bpm: float, change: bool, rms: float = 0.0) -> None:
        with self._lock:
            self._beats_detected += 1
            self._beats.append(
                {
                    't': self._now(),
                    'bpm': bpm,
                    'change': change,
                    'rms': round(rms, 4),
                }
            )

    def add_effect(
        self,
        name: str,
        effect_type: str,
        *,
        duration_seconds: float | None = None,
        selection: dict | None = None,
    ) -> None:
        if duration_seconds is not None and (
            not math.isfinite(duration_seconds) or duration_seconds <= 0
        ):
            raise ValueError('Effect duration must be finite and positive')
        with self._lock:
            now = self._now()
            for effect in reversed(self._effects):
                if effect['type'] == effect_type and (
                    effect_type == 'AUTOLOOP' or effect['name'] == name
                ):
                    effect['end'] = min(effect.get('end', now), now)
                    break
            effect = {'t': now, 'name': name, 'type': effect_type}
            if duration_seconds is not None:
                effect['end'] = now + duration_seconds
            if selection is not None:
                effect['selection'] = selection
            self._effects.append(effect)
            cutoff = now - self._window_sec * 2
            self._effects = [e for e in self._effects if e.get('end', now) >= cutoff]

    def stop_effects(self) -> None:
        with self._lock:
            now = self._now()
            for effect in self._effects:
                effect['end'] = min(effect.get('end', now), now)

    def set_playing(self, is_playing: bool) -> None:
        with self._lock:
            self._is_playing = is_playing
            now = self._now()
            self._sound_events.append({'t': now, 'playing': is_playing})

    def set_intent(
        self, intent: str, song_sec: float | None = None, trigger: str = CLASSIFIER_TRIGGER
    ) -> None:
        with self._lock:
            if (intent, trigger) == (self._current_intent, self._current_trigger):
                return
            self._current_intent = intent
            self._current_trigger = trigger
            now = self._now()
            if self._intents and 'end' not in self._intents[-1]:
                self._intents[-1]['end'] = now
            block = {'t': now, 'intent': intent, 'trigger': trigger}
            if song_sec is not None:
                block['song_t'] = round(float(song_sec), 6)
            self._intents.append(block)
            cutoff = now - self._window_sec * 2
            self._intents = [e for e in self._intents if e.get('end', now) >= cutoff]

    def set_timing_log(self, log: list[dict]) -> None:
        with self._lock:
            self._timing_log = list(log)

    def set_decoder_state(self, **state) -> None:
        with self._lock:
            self._decoder_state = dict(state)

    def set_shed_state(self, **state) -> None:
        with self._lock:
            self._shed_state = dict(state)

    def set_control_state(
        self,
        *,
        paused: bool,
        master_brightness: float,
        settings: dict,
        mode: str = 'auto',
        static: dict | None = None,
    ) -> None:
        with self._lock:
            self._control_state = {
                'paused': paused,
                'master_brightness': master_brightness,
                'settings': dict(settings),
                'mode': mode,
                'static': copy.deepcopy(static),
            }

    def operator_state(self) -> tuple[str | None, bool | None, str | None]:
        with self._lock:
            return (
                self._current_intent,
                self._control_state['paused'],
                self._control_state['mode'],
            )

    def set_palette(self, name: str, *, pinned: bool) -> None:
        with self._lock:
            self._palette = {'name': name, 'pinned': pinned}

    def set_special(
        self, name: str | None, group: str | None = None, duration_seconds: float | None = None
    ) -> None:
        with self._lock:
            if name is None:
                self._special = None
                return
            self._special = {'name': name, 'group': group}
            if duration_seconds is not None:
                self._special['end'] = self._now() + duration_seconds

    def set_special_groups(self, groups: dict[str, list[str]]) -> None:
        with self._lock:
            self._special_groups = copy.deepcopy(groups)

    def mark_intense(self) -> None:
        with self._lock:
            self._intense_at = self._now()

    def _strobe_cooldown_seconds(self, now: float) -> float | None:
        # drop_strobe_cooldown_seconds on the live settings, so a new intensity shows at once.
        settings = self._control_state['settings'] or {}
        cooldown, intensity = (
            settings.get('drop_strobe_cooldown_seconds'),
            settings.get('intensity'),
        )
        if cooldown is None or not intensity:
            return None
        if self._intense_at is None:
            return 0.0
        return max(0.0, cooldown / intensity - (now - self._intense_at))

    def set_system_health(self, state: dict) -> None:
        with self._lock:
            self._system_health = copy.deepcopy(state)
            self._system_history.append(state, self._timing_stats(self._timing_log))

    @staticmethod
    def _delivery(log: list[dict]) -> dict:
        errors_ms = [abs(e['actual_delta_sec'] - e['target_delta_sec']) * 1000 for e in log]
        deltas = [e['actual_delta_sec'] for e in log]
        return {
            'samples': len(log),
            'mean_delta_sec': sum(deltas) / len(deltas) if deltas else None,
            'mean_error_ms': sum(errors_ms) / len(errors_ms) if errors_ms else None,
            'max_error_ms': max(errors_ms) if errors_ms else None,
        }

    @classmethod
    def _timing_stats(cls, log: list[dict]) -> dict:
        labels: dict = {}
        for entry in log:
            labels.setdefault(entry['label'], []).append(entry)
        return dict(
            cls._delivery(log),
            by_label={label: cls._delivery(entries) for label, entries in sorted(labels.items())},
        )

    def sound_events(self) -> list:
        with self._lock:
            return list(self._sound_events)

    def _sound_events_since(self, cutoff: float) -> list:
        before = [e for e in self._sound_events if e['t'] < cutoff]
        return before[-1:] + [e for e in self._sound_events if e['t'] >= cutoff]

    def _beats_since(self, cutoff: float) -> list:
        recent = []
        for beat in reversed(self._beats):
            if beat['t'] < cutoff:
                break
            recent.append(beat)
        recent.reverse()
        return recent

    def snapshot(self) -> dict:
        with self._lock:
            now = self._now()
            cutoff = now - min(self._window_sec, self.SNAPSHOT_WINDOW_SEC + self._look_ahead_sec)
            timing_stats = self._timing_stats(self._timing_log)
            return {
                'now': now,
                'look_ahead_sec': self._look_ahead_sec,
                'is_playing': self._is_playing and self._end_time is None,
                'beats': self._beats_since(cutoff),
                'effects': [e for e in self._effects if e.get('end', now) >= cutoff],
                'intents': [e for e in self._intents if e.get('end', now) >= cutoff],
                'current_effect': next(
                    (
                        effect
                        for effect in reversed(self._effects)
                        if effect['type'] == 'AUTOLOOP' and effect.get('end', float('inf')) > now
                    ),
                    None,
                ),
                'bpm': self._beats[-1]['bpm'] if self._beats else 0.0,
                'beats_detected': self._beats_detected,
                'intent': self._current_intent,
                'sound_events': self._sound_events_since(cutoff),
                'timing_stats': timing_stats,
                'decoder': dict(self._decoder_state),
                'shed': dict(self._shed_state),
                **copy.deepcopy(self._control_state),
                'palette': copy.deepcopy(self._palette),
                'special': (
                    None
                    if self._special is None or self._special.get('end', now) < now
                    else dict(self._special)
                ),
                'special_groups': copy.deepcopy(self._special_groups),
                'strobe_cooldown_seconds': self._strobe_cooldown_seconds(now),
                'system': copy.deepcopy(self._system_health),
                'system_history': self._system_history.snapshot(),
            }

    def to_report(self, timing_log: list[dict] | None = None) -> dict:
        with self._lock:
            now = self._now()

            all_effects = [
                {**effect, 'end': min(effect.get('end', now), now)} for effect in self._effects
            ]

            all_intents = list(self._intents)
            if all_intents and 'end' not in all_intents[-1]:
                all_intents[-1] = {**all_intents[-1], 'end': now}

            tlog = timing_log if timing_log is not None else self._timing_log
            errors_ms = [abs(e['actual_delta_sec'] - e['target_delta_sec']) * 1000 for e in tlog]
            durations = [e['end'] - e['t'] for e in all_effects if 'end' in e]
            unique_effect_names = {e['name'] for e in all_effects}
            all_beats = list(self._beats)

            intent_distribution: dict[str, float] = {}
            for entry in all_intents:
                if 'end' not in entry:
                    continue
                dur = entry['end'] - entry['t']
                intent_distribution[entry['intent']] = (
                    intent_distribution.get(entry['intent'], 0.0) + dur
                )
            dominant_intent = (
                max(intent_distribution, key=intent_distribution.__getitem__)
                if intent_distribution
                else None
            )

            report = {
                'duration_sec': now,
                'beats': all_beats,
                'effects': all_effects,
                'intents': all_intents,
                'timing_log': tlog,
                'metrics': {
                    'look_ahead_sec': self._look_ahead_sec,
                    'beats_detected': len(all_beats),
                    'bpm_last': all_beats[-1]['bpm'] if all_beats else 0.0,
                    'timing_error_mean_ms': (sum(errors_ms) / len(errors_ms) if errors_ms else 0.0),
                    'timing_error_max_ms': max(errors_ms) if errors_ms else 0.0,
                    'unique_effects_count': len(unique_effect_names),
                    'effect_changes_count': len(all_effects),
                    'avg_effect_duration_sec': (
                        sum(durations) / len(durations) if durations else 0.0
                    ),
                    'unique_effect_names': sorted(unique_effect_names),
                    'intent_changes_count': len(all_intents),
                    'unique_intents_count': len(intent_distribution),
                    'intent_distribution_sec': {
                        k: round(v, 2) for k, v in sorted(intent_distribution.items())
                    },
                    'dominant_intent': dominant_intent,
                },
            }
            # Offline replays have no hardware telemetry; retain their deterministic schema.
            if self._system_health:
                report['system'] = copy.deepcopy(self._system_health)
                report['system_history'] = self._system_history.snapshot()
            return report
