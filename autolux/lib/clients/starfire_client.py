"""Musical lighting commands; process and protobuf ownership stays in the CLI."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from autolux.lighting.palettes import DEFAULT_PALETTE

if TYPE_CHECKING:
    from autolux.lib.engine.cue import Cue

RMS_LEVEL_FLOOR_DBFS = -48.0
RMS_LEVEL_CEILING_DBFS = -12.0
# A band's share of the mix sits well below full-scale RMS, so its range starts lower.
BAND_LEVEL_FLOOR_DBFS = -60.0
BAND_LEVEL_CEILING_DBFS = -12.0
BEAT_FEATURES = ('onset', 'bass', 'mid', 'high')
WHOLE_RIG = 'all'


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


def band_to_normalized_level(dbfs: float) -> float:
    """Map a band's energy in dBFS linearly onto 0..1, as the RMS level is mapped."""
    if math.isnan(dbfs):
        raise ValueError('band level must be a number')
    return min(
        1.0,
        max(
            0.0,
            (dbfs - BAND_LEVEL_FLOOR_DBFS) / (BAND_LEVEL_CEILING_DBFS - BAND_LEVEL_FLOOR_DBFS),
        ),
    )


def special_target(group: str) -> str | dict:
    return WHOLE_RIG if group == WHOLE_RIG else {'group': group}


class StarfireClient:
    """Submit commands to a bounded nonblocking transport or simulation recorder."""

    def __init__(
        self,
        send_command: Callable[[dict], None],
        groups: Callable[[], Sequence[dict]] = tuple,
    ):
        self._send_command = send_command
        self.groups = groups
        self.selected_autoloop: tuple[str, str] | None = None
        self.palette: str = DEFAULT_PALETTE

    def beat(
        self,
        beat_index: int,
        bar_phase: int | None,
        beats_per_bar: int,
        bpm: float,
        features: dict[str, float] | None = None,
    ) -> None:
        command = {
            'kind': 'beat',
            'beat_index': beat_index,
            'bar_phase': bar_phase,
            'beats_per_bar': beats_per_bar,
            'bpm': bpm,
        }
        if features is not None:
            if set(features) != set(BEAT_FEATURES):
                raise ValueError(f'beat features are exactly {BEAT_FEATURES}')
            command['features'] = dict(features)
        self._send_command(command)

    def cue_section(self, cue: Cue) -> None:
        """Starfire starts the cue on its own clock; PlayAutoloop and stop clear it."""
        self._send_command(cue.command())

    def clear_cues(self) -> None:
        self._send_command({'kind': 'clear_cues'})

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

    def set_special(self, override_id: str, group: str, special: dict, *, priority: int) -> None:
        self.set_override(
            {
                'id': override_id,
                'target': special_target(group),
                'special_fx': dict(special),
                'priority': priority,
            }
        )

    def clear_override(self, override_id: str, instantaneous_release: bool = False) -> None:
        command = {'kind': 'clear_override', 'id': override_id}
        if instantaneous_release:
            command['instantaneous_release'] = True
        self._send_command(command)

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

    def set_intensity(self, level: float) -> None:
        self._send_command({'kind': 'set_intensity', 'level': level})

    def set_group_master(self, group: str, level: float) -> None:
        self._send_command({'kind': 'set_group_master', 'group': group, 'level': level})

    def set_blackout(self, enabled: bool) -> None:
        self._send_command({'kind': 'set_blackout', 'enabled': enabled})

    def discard_delayed_audio(self) -> None:
        self._send_command({'kind': 'discard_delayed_audio'})

    def stop(self) -> None:
        self._send_command({'kind': 'stop'})
        self.selected_autoloop = None
