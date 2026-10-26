from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.autoloop_policy import AutoloopChoice, AutoloopPolicy, SelectionContext
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    DEFAULT_AUTOLOOPS,
    STANDBY_AUTOLOOP,
    STANDBY_CROSSFADE_SECONDS,
    STROBE_BURST_BEATS,
    STROBE_GATE_INTENSITY,
    STROBE_RATE_HZ,
    AutoloopMetaTable,
    AutoloopTable,
    LightIntent,
    ShowSettings,
    transition_for,
)
from autolux.lib.engine.palette_policy import PalettePolicy, palette_variant
from autolux.lighting.palettes import PALETTE_NAMES, PALETTES, Mood, Palette

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

DROP_ACCENT_PRIORITY = 100
DROP_ACCENT_ID = 'drop_arrival'


def strobe_burst_seconds(bpm: float | None, settings: ShowSettings) -> float:
    duration = min(settings.strobe_burst_seconds, settings.strobe_max_burst_seconds)
    if bpm and bpm > 0:
        duration = min(duration, STROBE_BURST_BEATS * 60.0 / bpm)
    return duration * settings.intensity


def strobe_rate_hz(settings: ShowSettings) -> float:
    return min(STROBE_RATE_HZ, settings.strobe_max_rate_hz) * settings.intensity


def drop_strobe_cooldown_seconds(settings: ShowSettings) -> float:
    return settings.drop_strobe_cooldown_seconds / settings.intensity


def strobe_override(
    override_id: str, priority: int, duration_seconds: float, settings: ShowSettings
) -> dict:
    return {
        'id': override_id,
        'target': 'all',
        'values': {'strobe_hz': strobe_rate_hz(settings)},
        'priority': priority,
        'lifetime': {'kind': 'seconds', 'duration_seconds': duration_seconds},
    }


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
        palettes: tuple[Palette, ...] = PALETTES,
    ):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer
        self.clock = clock
        self._settings = settings
        self._last_strobe_started_at: float | None = None
        self._policy = AutoloopPolicy(autoloops, autoloop_meta, random.Random(seed))
        self._entered: tuple[LightIntent, AutoloopChoice] | None = None
        self._palette_policy = PalettePolicy(palettes, random.Random(f'{seed}/palette'))
        self._pinned_palette: str | None = None
        self._publish_palette()

    @property
    def pinned_palette(self) -> str | None:
        return self._pinned_palette

    def pin_palette(self, name: str | None) -> None:
        if name is not None and name not in PALETTE_NAMES:
            raise ValueError(f'unknown palette {name!r}; choose one of {", ".join(PALETTE_NAMES)}')
        self._pinned_palette = name
        palette = self._palette_for_current_look()
        if palette is not None and palette != self.lighting_client.palette:
            self.lighting_client.set_palette(palette)
        self._publish_palette()
        LOGGER.info('[lighting] palette %s', f'pinned to {name}' if name else 'follows the policy')

    def choose_track_palette(self, mood: Mood, *, send_now: bool = True) -> str:
        chosen = self._palette_policy.choose_for_track(mood)
        LOGGER.info(
            '[lighting] track mood temperature=%+.2f weight=%+.2f drive=%+.2f | palette %s',
            *mood.axes(),
            chosen,
        )
        palette = self._palette_for_current_look()
        if send_now and palette is not None and palette != self.lighting_client.palette:
            self.lighting_client.set_palette(palette)
            self._publish_palette()
        return chosen

    def _palette_for_current_look(self) -> str | None:
        intent = LightIntent.ATMOSPHERIC if self._entered is None else self._entered[0]
        if self._pinned_palette is not None:
            return palette_variant(self._pinned_palette, intent)
        if self._entered is None:
            return None
        return self._palette_policy.variant_for(intent)

    def _palette_for(self, context: SelectionContext) -> str:
        base = self._pinned_palette or self._palette_policy.track_palette
        return palette_variant(base, context.intent)

    def _publish_palette(self) -> None:
        if self.event_buffer is not None:
            self.event_buffer.set_palette(
                self.lighting_client.palette, pinned=self._pinned_palette is not None
            )

    def _entry_choice(self, context: SelectionContext) -> tuple[AutoloopChoice, bool]:
        if (
            self._entered is not None
            and self._entered[0] == context.intent
            and not context.new_track
        ):
            return self._entered[1], False
        return self._policy.choose(context, intensity=self._settings().intensity), True

    async def change_effect(
        self,
        intent: LightIntent,
        bpm: float | None = None,
        context: SelectionContext | None = None,
        *,
        cut: bool = False,
    ) -> bool:
        context = context or SelectionContext(intent)
        choice, fresh = self._entry_choice(context)
        bank, autoloop = choice.bank, choice.autoloop
        transition = {'kind': 'cut'} if cut else transition_for(intent)
        palette = self._palette_for(context)
        if not self.lighting_client.select_autoloop(
            bank, autoloop, palette=palette, transition=transition
        ):
            return False
        self._publish_palette()
        if fresh:
            self._policy.record(context, choice)
            self._entered = intent, choice
        self._announce('[lighting]', intent, choice, palette)

        if intent == LightIntent.DROP:
            self._strobe_unless_cooling_down(bpm)
        return True

    def skip_autoloop(self, context: SelectionContext) -> AutoloopChoice:
        choice = self._policy.choose(
            context,
            exclude=self.lighting_client.selected_autoloop,
            intensity=self._settings().intensity,
        )
        self.lighting_client.select_autoloop(choice.bank, choice.autoloop)
        self._policy.record(context, choice)
        self._entered = context.intent, choice
        self._announce(
            '[lighting] next autoloop |', context.intent, choice, self.lighting_client.palette
        )
        return choice

    def _announce(
        self, prefix: str, intent: LightIntent, choice: AutoloopChoice, palette: str
    ) -> None:
        bank, autoloop, meta = choice.bank, choice.autoloop, choice.meta
        if self.event_buffer:
            self.event_buffer.add_effect(
                f'{bank}/{autoloop}',
                'AUTOLOOP',
                selection={
                    'energy': meta.energy,
                    'family': meta.family,
                    'movement': meta.movement,
                    'palette': palette,
                    'reason': choice.reason,
                },
            )
        LOGGER.info(
            '%s intent=%s | autoloop=%s/%s | palette=%s | energy=%d family=%s movement=%s '
            '| reason=%s',
            prefix,
            intent.name,
            bank,
            autoloop,
            palette,
            meta.energy,
            meta.family,
            meta.movement,
            choice.reason,
        )

    def enter_standby(self, bpm: float | None, *, renderer_paused: bool = False) -> None:
        self.lighting_client.clear_override(DROP_ACCENT_ID)
        bank, autoloop = STANDBY_AUTOLOOP
        transition = (
            {'kind': 'crossfade', 'duration_beats': bpm / 60.0 * STANDBY_CROSSFADE_SECONDS}
            if bpm and bpm > 0 and not renderer_paused
            else {'kind': 'cut'}
        )
        if not self.lighting_client.select_autoloop(bank, autoloop, transition=transition):
            return
        if self.event_buffer:
            self.event_buffer.add_effect(f'{bank}/{autoloop}', 'AUTOLOOP')
        LOGGER.info('[lighting] standby | autoloop=%s/%s', bank, autoloop)

    def _strobe_unless_cooling_down(self, bpm: float | None) -> None:
        settings = self._settings()
        if settings.intensity < STROBE_GATE_INTENSITY:
            LOGGER.info(
                '[lighting] drop strobe suppressed | intensity %.2f below %.2f',
                settings.intensity,
                STROBE_GATE_INTENSITY,
            )
            return
        cooldown = drop_strobe_cooldown_seconds(settings)
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
        duration = strobe_burst_seconds(bpm, settings)
        self.lighting_client.set_override(
            strobe_override(DROP_ACCENT_ID, DROP_ACCENT_PRIORITY, duration, settings)
        )
        if self.event_buffer:
            self.event_buffer.add_effect(DROP_ACCENT_ID, 'ACCENT', duration_seconds=duration)
        LOGGER.info('[lighting] drop accent | duration=%.1fs', duration)
