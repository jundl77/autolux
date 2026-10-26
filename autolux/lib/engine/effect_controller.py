from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.autoloop_policy import (
    DEFAULT_AUTOLOOP_META,
    AutoloopChoice,
    AutoloopMetaTable,
    AutoloopPolicy,
    SelectionContext,
)
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOPS,
    STROBE_BURST_BEATS,
    STROBE_MAX_BURST_SECONDS,
    STROBE_RATE_HZ,
    AutoloopTable,
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
        autoloops: AutoloopTable = DEFAULT_AUTOLOOPS,
        autoloop_meta: AutoloopMetaTable = DEFAULT_AUTOLOOP_META,
        seed: int = 0,
    ):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer
        self.clock = clock
        # Read at use so a dashboard edit applies to the next change.
        self._settings = settings
        self._last_strobe_started_at: float | None = None
        # One seed per session: a replay of the same run picks the same looks.
        self._policy = AutoloopPolicy(autoloops, autoloop_meta, random.Random(seed))
        self._entered: tuple[LightIntent, AutoloopChoice] | None = None

    def _entry_choice(self, context: SelectionContext) -> AutoloopChoice:
        # Re-sending the current intent (resume, restart after stop) keeps its
        # look; a change of intent or a new track chooses again.
        if (
            self._entered is not None
            and self._entered[0] == context.intent
            and not context.new_track
        ):
            return self._entered[1]
        choice = self._policy.choose(context)
        self._entered = context.intent, choice
        return choice

    async def change_effect(
        self,
        intent: LightIntent,
        bpm: float | None = None,
        context: SelectionContext | None = None,
    ) -> None:
        if context is None:
            context = SelectionContext(intent, self.clock.monotonic())
        choice = self._entry_choice(context)
        bank, autoloop, meta = choice.bank, choice.autoloop, choice.meta
        if not self.lighting_client.select_autoloop(
            bank, autoloop, transition=transition_for(intent, self._settings())
        ):
            return
        if self.event_buffer:
            self.event_buffer.add_effect(
                f'{bank}/{autoloop}',
                'AUTOLOOP',
                selection={
                    'energy': meta.energy,
                    'family': meta.family,
                    'movement': meta.movement,
                    'reason': choice.reason,
                },
            )
        LOGGER.info(
            '[lighting] intent=%s | autoloop=%s/%s | energy=%d family=%s movement=%s | reason=%s',
            intent.name,
            bank,
            autoloop,
            meta.energy,
            meta.family,
            meta.movement,
            choice.reason,
        )

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
