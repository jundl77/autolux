from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.effect_definitions import (
    AUTOLOOP_BY_LIGHT_INTENT,
    STROBE_BURST_BEATS,
    STROBE_MAX_BURST_SECONDS,
    STROBE_RATE_HZ,
    LightIntent,
    ShowSettings,
    transition_for,
)

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

DROP_STROBE_HZ = STROBE_RATE_HZ
DROP_ACCENT_PRIORITY = 100
DROP_ACCENT_ID = 'drop_arrival'


def strobe_burst_seconds(bpm: float | None, burst_seconds: float) -> float:
    duration = min(burst_seconds, STROBE_MAX_BURST_SECONDS)
    if bpm and bpm > 0:
        duration = min(duration, STROBE_BURST_BEATS * 60.0 / bpm)
    return duration


class EffectController:
    def __init__(
        self,
        lighting_client: StarfireClient,
        event_buffer: EventBuffer | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: Callable[[], ShowSettings] = ShowSettings,
    ):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer
        self.clock = clock
        # Read at use so a dashboard edit applies to the next change.
        self._settings = settings
        self._last_strobe_started_at: float | None = None

    async def change_effect(self, intent: LightIntent, bpm: float | None = None) -> None:
        bank, autoloop = AUTOLOOP_BY_LIGHT_INTENT[intent]
        if not self.lighting_client.select_autoloop(
            bank, autoloop, transition=transition_for(intent, self._settings())
        ):
            return
        if self.event_buffer:
            self.event_buffer.add_effect(f'{bank}/{autoloop}', 'AUTOLOOP')
        LOGGER.info('[lighting] intent=%s | autoloop=%s/%s', intent.name, bank, autoloop)

        if intent == LightIntent.DROP:
            self._strobe_unless_cooling_down(bpm)

    def _strobe_unless_cooling_down(self, bpm: float | None) -> None:
        settings = self._settings()
        cooldown = settings.drop_strobe_cooldown_seconds
        now = self.clock.monotonic()
        last = self._last_strobe_started_at
        if last is not None and now - last < cooldown:
            LOGGER.info(
                '[lighting] drop strobe suppressed | cooldown %.0fs of %.0fs left',
                cooldown - (now - last),
                cooldown,
            )
            return
        self._last_strobe_started_at = now
        duration = strobe_burst_seconds(bpm, settings.strobe_burst_seconds)
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
