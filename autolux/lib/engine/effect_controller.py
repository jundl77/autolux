from __future__ import annotations

import dataclasses
import logging
import math
import random
from collections.abc import Callable, Hashable, Sequence
from typing import TYPE_CHECKING

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.autoloop_policy import (
    MAXIMAL_ARRIVAL_OF_PEAK,
    AutoloopChoice,
    AutoloopPolicy,
    SelectionContext,
    look_intent,
    max_energy_for_intensity,
    soft_drop,
)
from autolux.lib.engine.cue import BaseLook, Cue, Overlay
from autolux.lib.engine.cue_director import CueDirector, SectionPlan, SectionRequest
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    DEFAULT_AUTOLOOPS,
    DROP_STROBE_COOLDOWN_SCALE,
    MAX_STROBE_COOLDOWN_SECONDS,
    STANDBY_AUTOLOOP,
    STANDBY_CROSSFADE_SECONDS,
    STROBE_GATE_INTENSITY,
    AutoloopMetaTable,
    AutoloopTable,
    LightIntent,
    OverlayMetaTable,
    ShowSettings,
    intensity_level,
    transition_for,
)
from autolux.lib.engine.palette_policy import PalettePolicy, section_palette
from autolux.lib.engine.special_policy import (
    Special,
    SpecialCapabilities,
    SpecialPolicy,
    Venue,
    moments_near,
)
from autolux.lighting.palettes import PALETTE_NAMES, PALETTES, Mood, Palette

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

SPECIAL_PRIORITY = 100
SPECIAL_ID = 'special'
OPERATOR_FADE_SECONDS = 0.1


def drop_strobe_cooldown_seconds(settings: ShowSettings) -> float:
    scale = DROP_STROBE_COOLDOWN_SCALE[intensity_level(settings.intensity)]
    return settings.drop_strobe_cooldown_seconds * scale


def operator_transition(bpm: float | None) -> dict:
    if bpm and bpm > 0:
        return {'kind': 'crossfade', 'duration_beats': bpm / 60.0 * OPERATOR_FADE_SECONDS}
    return {'kind': 'cut'}


def _loop_ids(looks: Sequence[BaseLook | Overlay]) -> str:
    return ','.join(f'{look.bank}/{look.autoloop}' for look in looks)


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
        specials: Sequence[Special] = (),
        venue: Venue | None = None,
        overlay_meta: OverlayMetaTable | None = None,
    ):
        self.lighting_client = lighting_client
        self.event_buffer = event_buffer
        self.clock = clock
        self._settings = settings
        self._intense_at: dict[Hashable, float] = {}
        self._policy = AutoloopPolicy(autoloops, autoloop_meta, random.Random(seed))
        self._director = CueDirector(self._policy, overlay_meta or {}, seed)
        self._entered: tuple[LightIntent, AutoloopChoice] | None = None
        self._entered_moment: Hashable = None
        self._track_dropped = False
        self.palette_listener: Callable[[], None] | None = None
        self._palette_policy = PalettePolicy(palettes, random.Random(f'{seed}/palette'))
        self._pinned_palette: str | None = None
        self.specials = tuple(specials)
        self._special_policy = SpecialPolicy(self.specials, random.Random(f'{seed}/special'), venue)
        self._special: Special | None = None
        self._special_on = False
        self._capabilities: SpecialCapabilities | None = None
        self._publish_palette()
        self.special_capabilities()
        if event_buffer is not None:
            event_buffer.set_cooldown_source(self.special_cooldowns)
            event_buffer.set_planning(seed, dataclasses.asdict(venue or Venue()))
            event_buffer.record_settings(settings().to_dict())

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
        self._palette_changed()

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
        self._palette_changed()
        return chosen

    def _palette_changed(self) -> None:
        if self.palette_listener is not None:
            self.palette_listener()

    def _palette_for_current_look(self) -> str | None:
        plan = self._entered_plan()
        if plan is not None:
            return section_palette(
                self._palette_base(),
                plan.look,
                plan.request.intent,
                after_first_drop=plan.after_first_drop,
            )
        if self._entered is None:
            if self._pinned_palette is None:
                return None
            return self._palette_for(LightIntent.ATMOSPHERIC, LightIntent.ATMOSPHERIC)
        intent, choice = self._entered
        return self._palette_for(choice.look, intent)

    def _palette_base(self) -> str:
        return self._pinned_palette or self._palette_policy.track_palette

    def _palette_for(self, look: LightIntent, intent: LightIntent) -> str:
        return section_palette(
            self._palette_base(), look, intent, after_first_drop=self._track_dropped
        )

    def _entered_plan(self) -> SectionPlan | None:
        if self._entered_moment is None:
            return None
        return self._director.plan_for(self._entered_moment)

    def plan_cue(self, moment: Hashable, request: SectionRequest) -> Cue:
        plan = self._director.plan(moment, request, self._settings(), self._palette_base(), self)
        self._book(moment, plan)
        LOGGER.info(
            '[lighting] cue | intent=%s start=%d | looks=%s | palette=%s | overlays=%s',
            request.intent.name,
            request.start_beat_index,
            _loop_ids(plan.cue.rotation),
            plan.cue.palette,
            _loop_ids(plan.cue.overlays) or '-',
        )
        return plan.cue

    def revise_cue(self, moment: Hashable, *, started: bool, palette: bool = False) -> Cue | None:
        plan = self._director.revise(
            moment,
            self._settings(),
            self,
            started=started,
            palette=self._palette_base() if palette else None,
        )
        if plan is None:
            return None
        if not started:
            self._book(moment, plan)
        return plan.cue

    def cue_for(self, moment: Hashable) -> Cue | None:
        plan = self._director.plan_for(moment)
        return None if plan is None else plan.cue

    def drop_cue(self, moment: Hashable) -> None:
        if self._director.drop(moment) is not None:
            self._release_booking(moment)

    def reset_cues(self) -> None:
        now = self.clock.monotonic()
        self._director.reset()
        for moment, at in list(self._intense_at.items()):
            if at > now:
                self._release_booking(moment)
        self._special_policy.forget_accents_after(now)

    def _book(self, moment: Hashable, plan: SectionPlan) -> None:
        at = plan.request.room_sec
        kept = moments_near(self._intense_at, at, MAX_STROBE_COOLDOWN_SECONDS, excluding=moment)
        # Replaced, never mutated, so the dashboard thread reads a whole snapshot.
        self._intense_at = {**kept, moment: at} if plan.events.intense else kept
        if plan.events.accent:
            self._special_policy.plan_accent(moment, at)
        else:
            self._special_policy.forget_accent(moment)

    def _release_booking(self, moment: Hashable) -> None:
        self._intense_at = {key: at for key, at in self._intense_at.items() if key != moment}
        self._special_policy.forget_accent(moment)

    def intense_blocked(self, at: float, moment: Hashable) -> str | None:
        return self._intense_blocked(self._settings(), at, moment)

    def accent_crowded(self, at: float, moment: Hashable) -> bool:
        return self._special_policy.accent_crowded(at, moment)

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
        moment: Hashable = None,
    ) -> bool:
        context = context or SelectionContext(intent)
        if context.new_track:
            self._track_dropped = False
        plan = None if moment is None else self._director.plan_for(moment)
        if plan is not None:
            return self._enter_cue(intent, bpm, context, moment, plan)
        self._entered_moment = None
        choice, fresh = self._entry_choice(context)
        bank, autoloop = choice.bank, choice.autoloop
        transition = transition_for(choice.look)
        palette = self._palette_for(choice.look, intent)
        if intent is LightIntent.DROP:
            self._track_dropped = True
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

    def _enter_cue(
        self,
        intent: LightIntent,
        bpm: float | None,
        context: SelectionContext,
        moment: Hashable,
        plan: SectionPlan,
    ) -> bool:
        fresh = moment is not self._entered_moment
        self._track_dropped = self._track_dropped or intent is LightIntent.DROP
        choice = plan.choices[0]
        self._entered = intent, choice
        self._entered_moment = moment
        # Starfire's clock changed the look, so the client's dedup record is stale.
        self.lighting_client.selected_autoloop = None
        self.lighting_client.palette = plan.cue.palette
        self._publish_palette()
        self._announce('[lighting] cue |', intent, choice, plan.cue.palette, plan.cue)
        if fresh:
            self._enter_special(intent, bpm, context.arrival_of_peak, moment)
        else:
            self._restore_special()
        return True

    def apply_intensity(self, context: SelectionContext, bpm: float | None) -> None:
        if self._entered is None or self._entered[0] != context.intent:
            return
        current = self._entered[1]
        if self.lighting_client.selected_autoloop != (current.bank, current.autoloop):
            return
        intensity = self._settings().intensity
        remapped = look_intent(context.intent, intensity) != current.look
        if not remapped and current.meta.energy <= max_energy_for_intensity(
            intensity, current.look
        ):
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
        palette = self._palette_for(choice.look, context.intent) if remapped else None
        self.lighting_client.select_autoloop(
            choice.bank, choice.autoloop, palette=palette, transition=operator_transition(bpm)
        )
        if palette is not None:
            self._publish_palette()
        self._entered = context.intent, choice
        self._announce(
            f'[lighting] {cause} |', context.intent, choice, self.lighting_client.palette
        )

    def skip_autoloop(self, context: SelectionContext) -> AutoloopChoice:
        playing = self.lighting_client.selected_autoloop
        if playing is None and self._entered is not None:
            playing = self._entered[1].bank, self._entered[1].autoloop
        choice = self._policy.choose(context, exclude=playing, intensity=self._settings().intensity)
        self.lighting_client.select_autoloop(choice.bank, choice.autoloop)
        self._policy.record(context, choice)
        self._entered_moment = None
        self._entered = context.intent, choice
        self._announce(
            '[lighting] next autoloop |', context.intent, choice, self.lighting_client.palette
        )
        return choice

    def _announce(
        self,
        prefix: str,
        intent: LightIntent,
        choice: AutoloopChoice,
        palette: str,
        cue: Cue | None = None,
    ) -> None:
        bank, autoloop, meta = choice.bank, choice.autoloop, choice.meta
        if self.event_buffer:
            self.event_buffer.add_effect(
                f'{bank}/{autoloop}',
                'AUTOLOOP',
                cue_start_beat_index=None if cue is None else cue.start_beat_index,
                selection={
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

    def enter_standby(self, bpm: float | None) -> None:
        self.forget_special(instantaneous=True)
        bank, autoloop = STANDBY_AUTOLOOP
        transition = (
            {'kind': 'crossfade', 'duration_beats': bpm / 60.0 * STANDBY_CROSSFADE_SECONDS}
            if bpm and bpm > 0
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
        last = max((at for at in dict(self._intense_at).values() if at <= now), default=None)
        remaining = 0.0 if last is None else max(0.0, total - (now - last))
        return self._special_policy.cooldowns(now, total, remaining, self._capabilities)

    def _intense_blocked(
        self, settings: ShowSettings, at: float, moment: Hashable = None
    ) -> str | None:
        if settings.intensity <= STROBE_GATE_INTENSITY:
            return f'intensity {settings.intensity:.2f} at or below {STROBE_GATE_INTENSITY:.2f}'
        cooldown = drop_strobe_cooldown_seconds(settings)
        nearest = min(
            (abs(at - when) for key, when in self._intense_at.items() if key != moment),
            default=math.inf,
        )
        if nearest < cooldown:
            return f'cooldown {cooldown - nearest:.0f}s of {cooldown:.0f}s left'
        return None

    def _enter_special(
        self,
        intent: LightIntent,
        bpm: float | None,
        arrival_of_peak: float | None = None,
        moment: Hashable = None,
    ) -> None:
        settings = self._settings()
        now = self.clock.monotonic()
        choice = self._special_policy.choose(
            intent,
            self.special_capabilities(),
            self._intense_blocked(settings, now, moment),
            soft_drop=(
                f'drop arrival {arrival_of_peak:.2f} below {MAXIMAL_ARRIVAL_OF_PEAK:.2f} '
                'of the track peak'
                if soft_drop(arrival_of_peak)
                else None
            ),
            now=now,
            moment=moment,
        )
        if not settings.fx_auto:
            choice = dataclasses.replace(choice, special=None)
        if choice.suppressed:
            LOGGER.info('[lighting] intense special suppressed | %s', choice.suppressed)
        if choice.intense:
            key = object() if moment is None else moment
            self._intense_at = {**self._intense_at, key: now}
        if choice.special is None:
            self.forget_special()
        else:
            self._special_policy.record(choice.special, now, self._capabilities, moment)
            self._fire_special(choice.special)

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

    def release_special(self, *, instantaneous: bool = False) -> None:
        if not self._special_on:
            return
        self.lighting_client.clear_override(SPECIAL_ID, instantaneous_release=instantaneous)
        LOGGER.info(
            '[lighting] special %s %s',
            self._special.name,
            'instantaneous release'
            if instantaneous
            else f'released over {self._special.release_beats:g} beats',
        )
        self._special_on = False
        if self.event_buffer:
            self.event_buffer.set_special(None)

    def forget_special(self, *, instantaneous: bool = False) -> None:
        self.release_special(instantaneous=instantaneous)
        self._special = None

    def _restore_special(self) -> None:
        if not self._settings().fx_auto:
            self._special = None
        if self._special is not None and not self._special_on:
            self._fire_special(self._special)
