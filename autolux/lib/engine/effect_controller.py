from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.engine.effect_definitions import AUTOLOOP_BY_LIGHT_INTENT, LightIntent

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

STROBE_DURATION_SEC = 1.0
DROP_STROBE_HZ = 8.0
DROP_ACCENT_PRIORITY = 100
DROP_ACCENT_ID = 'drop_arrival'


class EffectController:
    def __init__(self, lighting_client: StarfireClient, event_buffer: EventBuffer | None = None):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer

    async def change_effect(self, intent: LightIntent) -> None:
        bank, autoloop = AUTOLOOP_BY_LIGHT_INTENT[intent]
        if not self.lighting_client.select_autoloop(bank, autoloop):
            return
        if self.event_buffer:
            self.event_buffer.add_effect(f'{bank}/{autoloop}', 'AUTOLOOP')
        LOGGER.info('[lighting] intent=%s | autoloop=%s/%s', intent.name, bank, autoloop)

        if intent == LightIntent.DROP:
            self.lighting_client.set_override(
                {
                    'id': DROP_ACCENT_ID,
                    'target': 'all',
                    'values': {'strobe_hz': DROP_STROBE_HZ},
                    'priority': DROP_ACCENT_PRIORITY,
                    'lifetime': {'kind': 'seconds', 'duration_seconds': STROBE_DURATION_SEC},
                }
            )
            if self.event_buffer:
                self.event_buffer.add_effect(
                    DROP_ACCENT_ID, 'ACCENT', duration_seconds=STROBE_DURATION_SEC
                )
            LOGGER.info('[lighting] drop accent | duration=%.1fs', STROBE_DURATION_SEC)
