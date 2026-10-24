from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from autolux.lib.clients.midi_client import MidiClient
from autolux.lib.clients.midi_message import MidiChannel
from autolux.lib.engine.effect_definitions import AUTOLOOP_BY_LIGHT_INTENT, LightIntent

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

STROBE_DURATION_SEC = 1


class EffectController:
    def __init__(self, midi_client: MidiClient, event_buffer: EventBuffer | None = None):
        self.midi_client = midi_client
        self.event_buffer = event_buffer

    async def change_effect(self, intent: LightIntent) -> None:
        autoloop = AUTOLOOP_BY_LIGHT_INTENT[intent]
        await self.midi_client.clear_color_overrides()
        await self.midi_client.set_autoloop(autoloop)
        if self.event_buffer:
            self.event_buffer.add_effect(autoloop.name, 'AUTOLOOP')
        logging.info('[lighting] intent=%s | effect=%s', intent.name, autoloop.name)

        # The drop loop must keep playing underneath the temporary strobe.
        if intent == LightIntent.DROP:
            strobe = MidiChannel.SPECIAL_EFFECT_STROBE
            await self.midi_client.set_special_effect(strobe, duration_sec=STROBE_DURATION_SEC)
            if self.event_buffer:
                self.event_buffer.add_effect(strobe.name, 'SPECIAL_EFFECT')
            logging.info('[lighting] strobe | duration=%ds', STROBE_DURATION_SEC)
