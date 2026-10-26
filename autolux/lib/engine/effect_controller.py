from __future__ import annotations

import dataclasses
import logging
import random
from collections.abc import Callable
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.autoloop_policy import (
    AutoloopChoice,
    AutoloopPolicy,
    SelectionContext,
    look_intent,
    max_energy_for_intensity,
)
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    DEFAULT_AUTOLOOPS,
    DROP_STROBE_COOLDOWN_SCALE,
    STANDBY_AUTOLOOP,
    STANDBY_CROSSFADE_SECONDS,
    STROBE_BURST_BEATS,
    STROBE_GATE_INTENSITY,
    STROBE_RATE_HZ,
    WILD,
    AutoloopMetaTable,
    AutoloopTable,
    LightIntent,
    ShowSettings,
    intensity_level,
    transition_for,
)
from autolux.lib.engine.palette_policy import PalettePolicy, palette_variant
from autolux.lighting.palettes import PALETTE_NAMES, PALETTES, Mood, Palette

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

DROP_ACCENT_PRIORITY = 100
DROP_ACCENT_ID = 'drop_arrival'
OPERATOR_FADE_SECONDS = 0.1


def strobe_burst_seconds(bpm: float | None, settings: ShowSettings) -> float:
    duration = settings.strobe_max_burst_seconds
    if intensity_level(settings.intensity) != WILD:
        duration = min(settings.strobe_burst_seconds, duration)
    if bpm and bpm > 0:
        duration = min(duration, STROBE_BURST_BEATS * 60.0 / bpm)
    return duration * settings.intensity


def strobe_rate_hz(settings: ShowSettings) -> float:
    return min(STROBE_RATE_HZ, settings.strobe_max_rate_hz) * settings.intensity


def drop_strobe_cooldown_seconds(settings: ShowSettings) -> float:
    scale = DROP_STROBE_COOLDOWN_SCALE[intensity_level(settings.intensity)]
    return settings.drop_strobe_cooldown_seconds * scale


def operator_transition(bpm: float | None) -> dict:
    # Starfire fades autoloops in beats; without a tempo there is no beat to fade over.
    if bpm and bpm > 0:
        return {'kind': 'crossfade', 'duration_beats': bpm / 60.0 * OPERATOR_FADE_SECONDS}
    return {'kind': 'cut'}


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
        intent = LightIntent.ATMOSPHERIC if self._entered is None else self._entered[1].look
        if self._pinned_palette is not None:
            return palette_variant(self._pinned_palette, intent)
        if self._entered is None:
            return None
        return self._palette_policy.variant_for(intent)

    def _palette_for(self, look: LightIntent) -> str:
        base = self._pinned_palette or self._palette_policy.track_palette
        return palette_variant(base, look)

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
        transition = {'kind': 'cut'} if cut else transition_for(choice.look)
        palette = self._palette_for(choice.look)
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

    def apply_intensity(
        self, context: SelectionContext, bpm: float | None
    ) -> AutoloopChoice | None:
        """Re-map the running look to the new level's bank, or swap it for a calmer loop.

        Entering or leaving calm moves a drop or build to its level's bank at once; within
        one bank a raised cap waits for the next entry.
        """
        if self._entered is None or self._entered[0] != context.intent:
            return None
        current = self._entered[1]
        if self.lighting_client.selected_autoloop != (current.bank, current.autoloop):
            return None  # Standby or another look is showing; the next entry obeys the level.
        intensity = self._settings().intensity
        remapped = look_intent(context.intent, intensity) != current.look
        if not remapped and current.meta.energy <= max_energy_for_intensity(intensity):
            return None
        try:
            choice = self._policy.choose(
                context, exclude=(current.bank, current.autoloop), intensity=intensity
            )
        except ValueError:
            return None
        if not remapped and choice.meta.energy >= current.meta.energy:
            return None
        cause = f'intensity {intensity_level(intensity)}' if remapped else 'intensity cap'
        choice = dataclasses.replace(choice, reason=f'{cause}; {choice.reason}')
        palette = self._palette_for(choice.look) if remapped else None
        self.lighting_client.select_autoloop(
            choice.bank, choice.autoloop, palette=palette, transition=operator_transition(bpm)
        )
        if palette is not None:
            self._publish_palette()
        # Not recorded: an operator swap must not spend the loop's turn in the rotation.
        self._entered = context.intent, choice
        self._announce(
            f'[lighting] {cause} |', context.intent, choice, self.lighting_client.palette
        )
        return choice

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
                    # A calm remap plays another bank's look; the section stays the intent.
                    'intent': intent.value,
                    'look': choice.look.value,
                    'level': intensity_level(self._settings().intensity),
                    'energy': meta.energy,
                    'texture': meta.texture,
                    'movement': meta.movement,
                    'palette': palette,
                    'reason': choice.reason,
                },
            )
        LOGGER.info(
            '%s intent=%s | autoloop=%s/%s | palette=%s | energy=%d movement=%s texture=%s '
            '| reason=%s',
            prefix,
            intent.name,
            bank,
            autoloop,
            palette,
            meta.energy,
            meta.movement,
            meta.texture,
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
        if settings.intensity <= STROBE_GATE_INTENSITY:
            LOGGER.info(
                '[lighting] drop strobe suppressed | intensity %.2f at or below %.2f',
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
