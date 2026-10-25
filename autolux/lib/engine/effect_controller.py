from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.effect_definitions import (
    AUTOLOOP_BY_LIGHT_INTENT,
    STROBE_BURST_BEATS,
    STROBE_COOLDOWN_SECONDS,
    STROBE_MAX_BURST_SECONDS,
    STROBE_RATE_HZ,
    TRANSITION_BY_LIGHT_INTENT,
    LightIntent,
)

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

STROBE_DURATION_SEC = 1.0
DROP_STROBE_HZ = STROBE_RATE_HZ
DROP_ACCENT_PRIORITY = 100
DROP_ACCENT_ID = 'drop_arrival'


def strobe_burst_seconds(bpm: float | None) -> float:
    duration = min(STROBE_DURATION_SEC, STROBE_MAX_BURST_SECONDS)
    if bpm and bpm > 0:
        duration = min(duration, STROBE_BURST_BEATS * 60.0 / bpm)
    return duration


class EffectController:
    def __init__(
        self,
        lighting_client: StarfireClient,
        event_buffer: EventBuffer | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer
        self.clock = clock
        self._last_strobe_started_at: float | None = None

    async def change_effect(self, intent: LightIntent, bpm: float | None = None) -> None:
        bank, autoloop = AUTOLOOP_BY_LIGHT_INTENT[intent]
        if not self.lighting_client.select_autoloop(
            bank, autoloop, transition=dict(TRANSITION_BY_LIGHT_INTENT[intent])
        ):
            return
        if self.event_buffer:
            self.event_buffer.add_effect(f'{bank}/{autoloop}', 'AUTOLOOP')
        LOGGER.info('[lighting] intent=%s | autoloop=%s/%s', intent.name, bank, autoloop)

        if intent == LightIntent.DROP:
            self._strobe_unless_cooling_down(bpm)

    def _strobe_unless_cooling_down(self, bpm: float | None) -> None:
        now = self.clock.monotonic()
        last = self._last_strobe_started_at
        if last is not None and now - last < STROBE_COOLDOWN_SECONDS:
            LOGGER.info(
                '[lighting] drop strobe suppressed | cooldown %.0fs of %.0fs left',
                STROBE_COOLDOWN_SECONDS - (now - last),
                STROBE_COOLDOWN_SECONDS,
            )
            return
        self._last_strobe_started_at = now
        duration = strobe_burst_seconds(bpm)
        self.lighting_client.set_override(
            {
                'id': DROP_ACCENT_ID,
                'target': 'all',
                'values': {'strobe_hz': DROP_STROBE_HZ},
                'priority': DROP_ACCENT_PRIORITY,
                'lifetime': {'kind': 'seconds', 'duration_seconds': duration},
            }
        )
        if self.event_buffer:
            self.event_buffer.add_effect(DROP_ACCENT_ID, 'ACCENT', duration_seconds=duration)
        LOGGER.info('[lighting] drop accent | duration=%.1fs', duration)
