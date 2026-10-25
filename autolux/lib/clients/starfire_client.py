"""Musical lighting commands; process and protobuf ownership stays in the CLI."""

from __future__ import annotations

import math
from collections.abc import Callable

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
        self.selected_sequence: tuple[str, str] | None = None

    def beat(self, position_beats: float, bpm: float) -> None:
        self._send_command({'kind': 'beat', 'position_beats': position_beats, 'bpm': bpm})

    def set_normalized_rms_level(self, level: float) -> None:
        self._send_command({'kind': 'set_normalized_rms_level', 'level': level})

    def select_sequence(self, bank: str, sequence: str, *, transition: dict | None = None) -> bool:
        identity = bank, sequence
        if identity == self.selected_sequence:
            return False
        self._send_command(
            {
                'kind': 'select',
                'bank': bank,
                'sequence': sequence,
                'transition': {'kind': 'cut'} if transition is None else transition,
            }
        )
        self.selected_sequence = identity
        return True

    def set_override(self, override: dict) -> None:
        self._send_command({'kind': 'set_override', 'value': override})

    def clear_override(self, override_id: str) -> None:
        self._send_command({'kind': 'clear_override', 'id': override_id})

    def set_master(self, intensity: float) -> None:
        self._send_command({'kind': 'set_master', 'intensity': intensity})

    def set_speed(self, multiplier: float) -> None:
        self._send_command({'kind': 'set_speed', 'multiplier': multiplier})

    def set_blackout(self, enabled: bool) -> None:
        self._send_command({'kind': 'set_blackout', 'enabled': enabled})

    def stop(self) -> None:
        self._send_command({'kind': 'stop'})
        self.selected_sequence = None
