"""Musical lighting commands; process and protobuf ownership stays in the CLI."""

from __future__ import annotations

import math
from collections.abc import Callable

from autolux.lighting.palettes import DEFAULT_PALETTE

RMS_LEVEL_FLOOR_DBFS = -48.0
RMS_LEVEL_CEILING_DBFS = -12.0


def rms_to_normalized_level(rms: float) -> float:
    """Map smoothed signal amplitude to a bounded loudness control, not emotion."""
    if not math.isfinite(rms) or rms < 0:
        raise ValueError('RMS must be finite and nonnegative')
    if rms == 0:
        return 0.0
    dbfs = 20.0 * math.log10(rms)
    return min(
        1.0,
        max(
            0.0,
            (dbfs - RMS_LEVEL_FLOOR_DBFS) / (RMS_LEVEL_CEILING_DBFS - RMS_LEVEL_FLOOR_DBFS),
        ),
    )


class StarfireClient:
    """Submit commands to a bounded nonblocking transport or simulation recorder."""

    def __init__(self, send_command: Callable[[dict], None]):
        self._send_command = send_command
        self.selected_autoloop: tuple[str, str] | None = None
        self.palette: str = DEFAULT_PALETTE

    def beat(self, beat_index: int, bar_phase: int | None, beats_per_bar: int, bpm: float) -> None:
        self._send_command(
            {
                'kind': 'beat',
                'beat_index': beat_index,
                'bar_phase': bar_phase,
                'beats_per_bar': beats_per_bar,
                'bpm': bpm,
            }
        )

    def set_normalized_rms_level(self, level: float) -> None:
        self._send_command({'kind': 'set_normalized_rms_level', 'level': level})

    def select_autoloop(
        self,
        bank: str,
        autoloop: str,
        *,
        palette: str | None = None,
        transition: dict | None = None,
    ) -> bool:
        identity = bank, autoloop
        palette = palette or self.palette
        if identity == self.selected_autoloop and palette == self.palette:
            return False
        command = {'kind': 'select', 'bank': bank, 'autoloop': autoloop, 'palette': palette}
        if transition is not None:
            command['transition'] = transition
        self._send_command(command)
        self.selected_autoloop = identity
        self.palette = palette
        return True

    def set_palette(self, name: str) -> None:
        self._send_command({'kind': 'set_palette', 'name': name})
        self.palette = name

    def set_override(self, override: dict) -> None:
        self._send_command({'kind': 'set_override', 'value': override})

    def clear_override(self, override_id: str) -> None:
        self._send_command({'kind': 'clear_override', 'id': override_id})

    def set_master(self, intensity: float, fade_seconds: float = 0.0) -> None:
        command = {'kind': 'set_master', 'intensity': intensity}
        if fade_seconds:
            command['fade_seconds'] = fade_seconds
        self._send_command(command)

    def set_paused(self, paused: bool) -> None:
        self._send_command({'kind': 'set_paused', 'paused': paused})

    def set_default_crossfade_beats(self, beats: float) -> None:
        self._send_command({'kind': 'set_default_crossfade_beats', 'beats': beats})

    def set_strobe_limits(self, max_rate_hz: float, max_burst_seconds: float) -> None:
        self._send_command({'kind': 'set_strobe_max_rate_hz', 'hz': max_rate_hz})
        self._send_command({'kind': 'set_strobe_max_burst_seconds', 'seconds': max_burst_seconds})

    def set_speed(self, multiplier: float) -> None:
        self._send_command({'kind': 'set_speed', 'multiplier': multiplier})

    def set_blackout(self, enabled: bool) -> None:
        self._send_command({'kind': 'set_blackout', 'enabled': enabled})

    def discard_delayed_audio(self) -> None:
        self._send_command({'kind': 'discard_delayed_audio'})

    def stop(self) -> None:
        self._send_command({'kind': 'stop'})
        self.selected_autoloop = None
