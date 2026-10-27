from __future__ import annotations

import dataclasses
import logging
import random
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient, special_target
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.autoloop_policy import (
    MAXIMAL_ARRIVAL_OF_PEAK,
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
    WILD,
    AutoloopMetaTable,
    AutoloopTable,
    LightIntent,
    ShowSettings,
    intensity_level,
    transition_for,
)
from autolux.lib.engine.palette_policy import PalettePolicy, palette_variant
from autolux.lib.engine.special_policy import (
    DEFAULT_SPECIALS,
    DROP_STROBE,
    Special,
    SpecialCapabilities,
    SpecialPolicy,
)
from autolux.lighting.palettes import PALETTE_NAMES, PALETTES, Mood, Palette

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

DROP_ACCENT_PRIORITY = 100
DROP_ACCENT_ID = 'drop_arrival'
SPECIAL_PRIORITY = DROP_ACCENT_PRIORITY
SPECIAL_ID = 'special'
OPERATOR_FADE_SECONDS = 0.1


def strobe_burst_seconds(bpm: float | None, settings: ShowSettings) -> float:
    duration = settings.strobe_max_burst_seconds
    if intensity_level(settings.intensity) != WILD:
        duration = min(settings.strobe_burst_seconds, duration)
    if bpm and bpm > 0:
        duration = min(duration, STROBE_BURST_BEATS * 60.0 / bpm)
    return duration * settings.intensity


def drop_strobe_cooldown_seconds(settings: ShowSettings) -> float:
    scale = DROP_STROBE_COOLDOWN_SCALE[intensity_level(settings.intensity)]
    return settings.drop_strobe_cooldown_seconds * scale


def operator_transition(bpm: float | None) -> dict:
    # Starfire fades autoloops in beats; without a tempo there is no beat to fade over.
    if bpm and bpm > 0:
        return {'kind': 'crossfade', 'duration_beats': bpm / 60.0 * OPERATOR_FADE_SECONDS}
    return {'kind': 'cut'}


def strobe_override(
    override_id: str,
    priority: int,
    duration_seconds: float,
    settings: ShowSettings,
    special: Special = DROP_STROBE,
) -> dict:
    return {
        'id': override_id,
        'target': special_target(special.group),
        'values': {
            'strobe_hz': min(special.strobe_hz, settings.strobe_max_rate_hz) * settings.intensity
        },
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
        specials: Sequence[Special] = DEFAULT_SPECIALS,
    ):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer
        self.clock = clock
        self._settings = settings
        self._last_intense_at: float | None = None
        self._policy = AutoloopPolicy(autoloops, autoloop_meta, random.Random(seed))
        self._entered: tuple[LightIntent, AutoloopChoice] | None = None
        self._palette_policy = PalettePolicy(palettes, random.Random(f'{seed}/palette'))
        self._pinned_palette: str | None = None
        self._special_policy = SpecialPolicy(specials, random.Random(f'{seed}/special'))
        self._special: Special | None = None
        self._special_on = False
        self._capabilities: SpecialCapabilities | None = None
        self._publish_palette()
        self.special_capabilities()
        if event_buffer is not None:
            event_buffer.set_cooldown_source(self.special_cooldowns)

    def special_capabilities(self) -> SpecialCapabilities:
        capabilities = SpecialCapabilities.from_groups(self.lighting_client.groups())
        if capabilities != self._capabilities:
            self._capabilities = capabilities
            if self.event_buffer is not None:
                self.event_buffer.set_special_groups(
                    {name: sorted(axes) for name, axes in capabilities.groups.items()},
                    dict(capabilities.roles),
                )
        return capabilities

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
            self._restore_special()
            return False
        self._publish_palette()
        if fresh:
            self._policy.record(context, choice)
            self._entered = intent, choice
        self._announce('[lighting]', intent, choice, palette)

        if fresh:
            self._enter_special(intent, bpm, context.arrival_of_peak)
        else:
            self._restore_special()
        return True

    def apply_intensity(self, context: SelectionContext, bpm: float | None) -> None:
        """Re-map the running look to the new level's bank, or swap it for a calmer loop.

        Entering or leaving calm moves a drop or build to its level's bank at once; within
        one bank a raised cap waits for the next entry.
        """
        if self._entered is None or self._entered[0] != context.intent:
            return
        current = self._entered[1]
        if self.lighting_client.selected_autoloop != (current.bank, current.autoloop):
            return  # Standby or another look is showing; the next entry obeys the level.
        intensity = self._settings().intensity
        remapped = look_intent(context.intent, intensity) != current.look
        if not remapped and current.meta.energy <= max_energy_for_intensity(intensity):
            return
        try:
            choice = self._policy.choose(
                context, exclude=(current.bank, current.autoloop), intensity=intensity
            )
        except ValueError:
            return
        if not remapped and choice.meta.energy >= current.meta.energy:
            return
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
        self._forget_special()
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

    def special_cooldowns(self) -> list[dict]:
        now = self.clock.monotonic()
        total = drop_strobe_cooldown_seconds(self._settings())
        last = self._last_intense_at
        remaining = 0.0 if last is None else max(0.0, total - (now - last))
        return self._special_policy.cooldowns(now, total, remaining, self._capabilities)

    def _intense_blocked(self, settings: ShowSettings, now: float) -> str | None:
        if settings.intensity <= STROBE_GATE_INTENSITY:
            return f'intensity {settings.intensity:.2f} at or below {STROBE_GATE_INTENSITY:.2f}'
        cooldown = drop_strobe_cooldown_seconds(settings)
        last = self._last_intense_at
        if last is not None and now - last < cooldown:
            return f'cooldown {cooldown - (now - last):.0f}s of {cooldown:.0f}s left'
        return None

    def _enter_special(
        self, intent: LightIntent, bpm: float | None, arrival_of_peak: float | None = None
    ) -> None:
        settings = self._settings()
        now = self.clock.monotonic()
        # An unknown arrival is not soft, matching how intensity follow grades a drop.
        soft = arrival_of_peak is not None and arrival_of_peak < MAXIMAL_ARRIVAL_OF_PEAK
        choice = self._special_policy.choose(
            intent,
            self.special_capabilities(),
            self._intense_blocked(settings, now),
            soft_drop=(
                f'drop arrival {arrival_of_peak:.2f} below {MAXIMAL_ARRIVAL_OF_PEAK:.2f} '
                'of the track peak'
                if soft
                else None
            ),
            now=now,
        )
        if choice.suppressed:
            LOGGER.info('[lighting] intense special suppressed | %s', choice.suppressed)
        if choice.intense:
            self._last_intense_at = now
            if self.event_buffer:
                self.event_buffer.mark_intense()
        if choice.special is None:
            self._forget_special()
        else:
            self._special_policy.record(choice.special, now)
            self._fire_special(choice.special)
        for strobe in choice.strobes:
            self._strobe(strobe, bpm, settings)

    def _fire_special(self, special: Special) -> None:
        self.lighting_client.set_special(
            SPECIAL_ID, special.group, special.wire(), priority=SPECIAL_PRIORITY
        )
        self._special = special
        self._special_on = True
        if self.event_buffer:
            self.event_buffer.set_special(special.name, special.group)
        LOGGER.info(
            '[lighting] special %s | group=%s colour=%s energy=%.2f speed=%.2f pulse=%s',
            special.name,
            special.group,
            special.colour_slot,
            special.energy,
            special.speed,
            special.pulse_beats,
        )

    def release_special(self) -> None:
        if not self._special_on:
            return
        self.lighting_client.clear_override(SPECIAL_ID)
        LOGGER.info(
            '[lighting] special %s released over %g beats',
            self._special.name,
            self._special.release_beats,
        )
        self._special_on = False
        if self.event_buffer:
            self.event_buffer.set_special(None)

    def _forget_special(self) -> None:
        self.release_special()
        self._special = None

    def _restore_special(self) -> None:
        if self._special is not None and not self._special_on:
            self._fire_special(self._special)

    def _strobe(self, special: Special, bpm: float | None, settings: ShowSettings) -> None:
        duration = strobe_burst_seconds(bpm, settings)
        self.lighting_client.set_override(
            strobe_override(DROP_ACCENT_ID, DROP_ACCENT_PRIORITY, duration, settings, special)
        )
        if self.event_buffer:
            self.event_buffer.add_effect(DROP_ACCENT_ID, 'ACCENT', duration_seconds=duration)
            if not self._special_on:
                self.event_buffer.set_special(
                    special.name, special.group, duration_seconds=duration
                )
        LOGGER.info('[lighting] drop accent | duration=%.1fs', duration)
