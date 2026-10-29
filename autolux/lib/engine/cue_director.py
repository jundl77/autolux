from __future__ import annotations

import dataclasses
import math
import random
import zlib
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from autolux.lib.engine.autoloop_policy import (
    AutoloopChoice,
    AutoloopPolicy,
    SelectionContext,
    look_intent,
    soft_drop,
)
from autolux.lib.engine.cue import (
    MAX_OVERLAYS,
    CueSection,
    Envelope,
    Overlay,
    Step,
    recurring_rows,
)
from autolux.lib.engine.effect_definitions import (
    CALM,
    CROSSFADE_SCALE,
    WILD,
    LightIntent,
    OverlayMetaTable,
    ShowSettings,
    intensity_level,
)
from autolux.lib.engine.palette_policy import section_palette

ROTATION = {
    LightIntent.DROP: (2, 16),
    LightIntent.ATMOSPHERIC: (2, 32),
    LightIntent.BREAKDOWN: (2, 32),
    LightIntent.BUILDUP: (1, 16),
}
STEP_CROSSFADE_BEATS = 4.0
BREAKDOWN_CROSSFADE_BEATS = 8.0
BUILDUP_CROSSFADE_BEATS = 1.0

PRIORITY = {'fill': 10, 'accent': 20, 'riser': 30, 'hit': 40, 'dip': 50}
MOVER_FADE_BEATS = 0.5
DROP_DIP = ('dip', 'last_beat')
BREAKDOWN_DIP = ('dip', 'fade')
COLOUR_HIT = ('hit', 'colour_bump')
CALM_RISER = ('riser', 'strip_fill')
BALANCED_RISER = ('riser', 'white_rise')
TURNAROUND_FILL = ('fill', 'turnaround')
MIXED_TEXTURE = 'mixed'
STILL_MOVEMENT = 'still'
MOVEMENT_PAIRED_BANKS = ('accent',)
PIXEL_TEXTURE = 'pixel'

# Low, since a high-passed roll also drops RMS and must not count as gap.
GAP_LEVEL_SHARE = 0.25
SILENT_GAP_SHARE = 0.1
GAP_REFERENCE_BEATS = 16
BREAKDOWN_DIP_BEATS = 2.0
# Half a beat, the no-gap default, reads as a flicker between the riser's white and the hit's.
DROP_DIP_BEATS = 1.0

HIT_BEATS = 2.0
RISER_BARS = 8
MIN_RISER_BEATS = 4
# A decoder re-decision must not snap a lit riser off; at the drop the dip covers it.
RISER_RELEASE_BEATS = 2.0
# A strobing riser strobes in its last 8 beats, which must fit the strobe burst cap.
RISER_STROBE_BEATS = 8
FILL_BEATS = 4.0
PHRASE_BARS = 16
ACCENT_OFFSET_BARS = 8
ACCENT_BARS = 16
ENTRY_ACCENT_BARS = 8


class VenueGate(Protocol):
    def intense_blocked(self, at: float, moment: Hashable) -> str | None: ...

    def accent_crowded(self, at: float, moment: Hashable) -> bool: ...


@dataclass(frozen=True)
class SectionRequest:
    start_beat_index: int
    intent: LightIntent
    context: SelectionContext
    room_sec: float
    bpm: float | None = None
    levels_before: tuple[float, ...] = ()
    # No row may begin before Starfire has the cue.
    lead_beats: float = 0.0
    beats_per_bar: int = field(kw_only=True)


@dataclass(frozen=True)
class OverlayPicks:
    hits: tuple[tuple[str, str], ...] = ()
    strobe_hit: tuple[str, str] | None = None
    riser: tuple[str, str] | None = None
    strobe_riser: tuple[str, str] | None = None
    calm_riser: tuple[str, str] | None = None
    fill: tuple[str, str] | None = None
    accents: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EntryEvents:
    intense: bool = False
    accent: bool = False


@dataclass(frozen=True)
class SectionPlan:
    request: SectionRequest
    track: int
    section: int
    previous: LightIntent | None
    previous_beats: int
    drop_number: int
    continued: bool
    after_first_drop: bool
    choices: tuple[AutoloopChoice, ...]
    picks: OverlayPicks
    dip_beats: float
    cue: CueSection
    events: EntryEvents

    @property
    def look(self) -> LightIntent:
        return self.choices[0].look


def dip_beats(levels_before: Sequence[float], beats_per_bar: int) -> float:
    dips = (0.5, 1.0, float(beats_per_bar), 2.0 * beats_per_bar)
    window = list(levels_before)[-GAP_REFERENCE_BEATS:]
    if not window:
        return dips[0]
    reference = max(window)
    if reference <= 0:
        return dips[-1]
    gap = []
    for level in reversed(window):
        if level >= GAP_LEVEL_SHARE * reference:
            break
        gap.append(level)
    silent = all(level < SILENT_GAP_SHARE * reference for level in gap)
    choices = dips if silent else dips[:-1]
    return max((beats for beats in choices if beats <= len(gap)), default=dips[0])


def drop_strength(request: SectionRequest) -> float | None:
    arrival = request.context.arrival_of_peak
    return None if arrival is None else min(1.0, max(0.0, arrival))


def strobe_hit_beats(bpm: float | None, settings: ShowSettings, beats_per_bar: int) -> float:
    if not bpm or bpm <= 0:
        return 1.0
    seconds = min(settings.strobe_burst_seconds, settings.strobe_max_burst_seconds)
    return float(min(beats_per_bar, max(1, math.floor(seconds * bpm / 60.0))))


def cue_seed(seed: int, track: int, start_beat_index: int) -> int:
    return zlib.crc32(f'{seed}/{track}/{start_beat_index}'.encode())


def _row(
    overlay: tuple[str, str],
    begin: float,
    duration: float,
    every_bars: int = 0,
    attack: float = 0.0,
    release: float = 0.0,
    *,
    dip: bool = False,
) -> Overlay:
    bank, autoloop = overlay
    return Overlay(
        bank, autoloop, PRIORITY[bank], begin, duration, every_bars, Envelope(attack, release), dip
    )


class CueDirector:
    def __init__(self, policy: AutoloopPolicy, overlays: OverlayMetaTable, seed: int = 0):
        self._policy = policy
        self._overlays = overlays
        self._seed = seed
        self._rng = random.Random(f'{seed}/cue')
        self._plans: dict[Hashable, SectionPlan] = {}
        self._track = 0
        self._sections = 0
        self._new_track = True
        self._picks = 0
        self._picked: dict[tuple[str, str], int] = {}

    def plan_for(self, moment: Hashable) -> SectionPlan | None:
        return self._plans.get(moment)

    def reset(self) -> None:
        self._plans.clear()
        self._new_track = True

    def drop(self, moment: Hashable) -> SectionPlan | None:
        return self._plans.pop(moment, None)

    def plan(
        self,
        moment: Hashable,
        request: SectionRequest,
        settings: ShowSettings,
        palette: str,
        gate: VenueGate,
    ) -> SectionPlan:
        if request.context.new_track or self._new_track:
            self._new_track = False
            self._track += 1
            self._sections = 0
            self._plans = {
                key: plan for key, plan in self._plans.items() if plan.track == self._track - 1
            }
        self._sections += 1
        earlier = [
            plan
            for plan in self._plans.values()
            if plan.track == self._track and plan.cue.start_beat_index < request.start_beat_index
        ]
        previous = max(earlier, key=lambda plan: plan.cue.start_beat_index, default=None)
        drops = [plan for plan in earlier if plan.request.intent is LightIntent.DROP]
        first_drop = min(drops, key=lambda plan: plan.cue.start_beat_index, default=None)
        is_drop = request.intent is LightIntent.DROP
        continued = is_drop and previous is not None and previous.request.intent is LightIntent.DROP
        choices = self._rotation(
            request, settings, first_drop if is_drop else None, previous if continued else None
        )
        picks = self._picks_for(request.intent, first_drop, choices, previous)
        seed = cue_seed(self._seed, self._track, request.start_beat_index)
        draft = SectionPlan(
            request=request,
            track=self._track,
            section=self._sections,
            previous=previous and previous.request.intent,
            previous_beats=0
            if previous is None
            else request.start_beat_index - previous.cue.start_beat_index,
            drop_number=len(drops) + 1 if is_drop else 0,
            continued=continued,
            after_first_drop=bool(drops),
            choices=choices,
            picks=picks,
            dip_beats=dip_beats(request.levels_before, request.beats_per_bar) if is_drop else 0.0,
            cue=CueSection(
                request.start_beat_index,
                request.intent,
                self._steps(request, choices, settings, previous, continued),
                section_palette(
                    palette, choices[0].look, request.intent, after_first_drop=bool(drops)
                ),
                drop_strength=drop_strength(request) if is_drop else None,
                seed=seed,
                beats_per_bar=request.beats_per_bar,
            ),
            events=EntryEvents(),
        )
        plan = self._with_overlays(draft, settings, gate, moment)
        self._plans[moment] = plan
        return plan

    def revise(
        self,
        moment: Hashable,
        settings: ShowSettings,
        gate: VenueGate,
        *,
        started: bool,
        palette: str | None = None,
    ) -> SectionPlan | None:
        plan = self._plans.get(moment)
        if plan is None:
            return None
        if palette is not None:
            name = section_palette(
                palette, plan.look, plan.request.intent, after_first_drop=plan.after_first_drop
            )
            plan = dataclasses.replace(plan, cue=dataclasses.replace(plan.cue, palette=name))
        revised = self._with_overlays(plan, settings, gate, moment)
        if started:
            # Its one-shot rows and their bookings stand; only recurring rows go out again.
            once = tuple(row for row in plan.cue.overlays if row.every_bars == 0)
            again = recurring_rows(revised.cue.overlays)
            revised = dataclasses.replace(
                plan, cue=dataclasses.replace(plan.cue, overlays=once + again)
            )
        self._plans[moment] = revised
        return revised

    def _rotation(
        self,
        request: SectionRequest,
        settings: ShowSettings,
        first_drop: SectionPlan | None,
        split_from: SectionPlan | None,
    ) -> tuple[AutoloopChoice, ...]:
        context, intensity = request.context, settings.intensity
        look = look_intent(request.intent, intensity)
        if split_from is not None and split_from.look == look:
            return split_from.choices[1:] + split_from.choices[:1]
        if first_drop is not None and first_drop.look == look:
            first = dataclasses.replace(
                first_drop.choices[0], reason="the track's first drop again, one layer more"
            )
        else:
            first = self._policy.choose(context, intensity=intensity)
        self._policy.record(context, first)
        choices = [first]
        entries, _ = ROTATION[request.intent]
        while len(choices) < entries:
            last = choices[-1]
            try:
                following = self._policy.choose(
                    context,
                    exclude=(last.bank, last.autoloop),
                    intensity=intensity,
                    contrast=last.meta,
                )
            except ValueError:
                break
            self._policy.record(context, following)
            choices.append(following)
        return tuple(choices)

    def _steps(
        self,
        request: SectionRequest,
        choices: tuple[AutoloopChoice, ...],
        settings: ShowSettings,
        previous: SectionPlan | None,
        continued: bool,
    ) -> tuple[Step, ...]:
        scale = CROSSFADE_SCALE[intensity_level(settings.intensity)]
        _, bars = ROTATION[request.intent]
        if len(choices) == 1:
            bars = 0  # a lone step holds: wrapping would only re-enter the loop playing
        between = STEP_CROSSFADE_BEATS * scale
        entry = between if continued else self._entry_crossfade(request.intent, previous, scale)
        return tuple(
            Step(choice.bank, choice.autoloop, bars, entry if index == 0 else between)
            for index, choice in enumerate(choices)
        )

    @staticmethod
    def _entry_crossfade(
        intent: LightIntent, previous: SectionPlan | None, scale: float
    ) -> float | None:
        if intent is LightIntent.DROP:
            return 0.0
        if intent is LightIntent.BREAKDOWN:
            return BREAKDOWN_CROSSFADE_BEATS * scale
        if intent is LightIntent.BUILDUP:
            if previous is not None and previous.request.intent is LightIntent.DROP:
                return 0.0
            return BUILDUP_CROSSFADE_BEATS
        return None

    def _bank(
        self,
        bank: str,
        *,
        strobe: bool | None = None,
        under: Sequence[AutoloopChoice] = (),
    ) -> list[tuple[str, str]]:
        return sorted(
            loop
            for loop, meta in self._overlays.items()
            if loop[0] == bank
            and (strobe is None or meta.strobe == strobe)
            and not any(self._fights(loop, choice) for choice in under)
        )

    def _fights(self, overlay: tuple[str, str], base: AutoloopChoice) -> bool:
        meta = self._overlays[overlay]
        if overlay[0] == 'riser':
            return meta.texture == base.meta.texture == PIXEL_TEXTURE
        if meta.texture != MIXED_TEXTURE and meta.texture == base.meta.texture:
            return True
        return (
            overlay[0] in MOVEMENT_PAIRED_BANKS
            and meta.movement != STILL_MOVEMENT
            and meta.movement == base.meta.movement
        )

    def _shipped(self, overlay: tuple[str, str]) -> tuple[str, str] | None:
        return overlay if overlay in self._overlays else None

    def _pick(self, pool: Sequence[tuple[str, str]]) -> tuple[str, str] | None:
        if not pool:
            return None
        unused = [overlay for overlay in pool if overlay not in self._picked]
        chosen = self._rng.choice(unused) if unused else min(pool, key=self._picked.__getitem__)
        self._picks += 1
        self._picked[chosen] = self._picks
        return chosen

    def _picks_for(
        self,
        intent: LightIntent,
        first_drop: SectionPlan | None,
        choices: tuple[AutoloopChoice, ...],
        previous: SectionPlan | None,
    ) -> OverlayPicks:
        if intent is LightIntent.BUILDUP:
            return OverlayPicks()
        accents: list[tuple[str, str]] = []
        while intent is LightIntent.DROP and len(accents) < 2:
            pool = [loop for loop in self._bank('accent', under=choices) if loop not in accents]
            accent = self._pick(pool)
            if accent is None:
                break
            accents.append(accent)
        taken = {self._overlays[accent].texture for accent in accents} - {MIXED_TEXTURE}
        fills = [
            loop
            for loop in self._bank('fill', under=choices)
            if loop != TURNAROUND_FILL and self._overlays[loop].texture not in taken
        ]
        if intent is not LightIntent.DROP:
            return OverlayPicks(fill=self._pick(fills))
        white_hits = tuple(loop for loop in self._bank('hit', strobe=False) if loop != COLOUR_HIT)
        build = previous.choices if previous is not None else ()
        risers = self._bank('riser', strobe=False, under=build)
        return OverlayPicks(
            hits=first_drop.picks.hits if first_drop is not None else white_hits,
            strobe_hit=next(iter(self._bank('hit', strobe=True)), None),
            riser=BALANCED_RISER
            if BALANCED_RISER in risers
            else self._pick([loop for loop in risers if loop != CALM_RISER]),
            strobe_riser=next(iter(self._bank('riser', strobe=True, under=build)), None),
            calm_riser=CALM_RISER if CALM_RISER in risers else None,
            fill=self._pick(fills),
            accents=tuple(accents),
        )

    def _with_overlays(
        self, plan: SectionPlan, settings: ShowSettings, gate: VenueGate, moment: Hashable
    ) -> SectionPlan:
        level = intensity_level(settings.intensity)
        intent = plan.request.intent
        bar = plan.request.beats_per_bar
        events = EntryEvents()
        if intent is LightIntent.DROP:
            rows, events = self._drop_rows(plan, level, settings, gate, moment)
        elif intent is LightIntent.BREAKDOWN:
            rows = []
            if self._shipped(BREAKDOWN_DIP):
                rows.append(
                    _row(
                        BREAKDOWN_DIP, -BREAKDOWN_DIP_BEATS, BREAKDOWN_DIP_BEATS, 0, 1, 4, dip=True
                    )
                )
            rows += self._fills(plan.picks.fill, level, bar, attack=1.0, release=2.0, calm_every=32)
        elif intent is LightIntent.ATMOSPHERIC:
            rows = self._fills(
                plan.picks.fill, level, bar, attack=2.0, release=2.0, calm_every=None
            )
        else:
            rows = []
        cue = dataclasses.replace(plan.cue, overlays=tuple(rows))
        return dataclasses.replace(plan, cue=cue, events=events)

    @staticmethod
    def _fills(
        fill: tuple[str, str] | None,
        level: str,
        bar: int,
        *,
        attack: float,
        release: float,
        calm_every: int | None,
    ) -> list[Overlay]:
        every = calm_every if level == CALM else PHRASE_BARS
        if fill is None or every is None:
            return []
        return [_row(fill, (every - 1) * bar, FILL_BEATS, every, attack, release)]

    def _drop_rows(
        self,
        plan: SectionPlan,
        level: str,
        settings: ShowSettings,
        gate: VenueGate,
        moment: Hashable,
    ) -> tuple[list[Overlay], EntryEvents]:
        rows: list[Overlay] = []
        events = EntryEvents()
        accents = plan.picks.accents[:1] if level == CALM else plan.picks.accents
        entry_accent = None
        if not plan.continued:
            rows, events, entry_accent = self._drop_entry(
                plan, level, settings, gate, moment, accents
            )
        bar_8 = entry_accent is None or len(accents) > 1
        fills = self._drop_fills(plan, level, bar_8=bar_8)
        accent_rows = [
            _row(
                accent,
                (ACCENT_OFFSET_BARS + index * ACCENT_BARS) * plan.request.beats_per_bar,
                ACCENT_BARS * plan.request.beats_per_bar,
                2 * ACCENT_BARS,
                4.0,
                4.0,
            )
            for index, accent in enumerate(accents)
        ]
        rows += fills + accent_rows
        shed_over_row_cap = [*fills[:-1], *accent_rows[1:], *fills[-1:], *accent_rows[:1]]
        while len(rows) > MAX_OVERLAYS and shed_over_row_cap:
            rows.remove(shed_over_row_cap.pop(0))
        return rows, events

    def _drop_entry(
        self,
        plan: SectionPlan,
        level: str,
        settings: ShowSettings,
        gate: VenueGate,
        moment: Hashable,
        accents: tuple[tuple[str, str], ...],
    ) -> tuple[list[Overlay], EntryEvents, tuple[str, str] | None]:
        at, bpm = plan.request.room_sec, plan.request.bpm
        blocked = gate.intense_blocked(at, moment)
        crowded = gate.accent_crowded(at, moment)
        white = level != CALM and not crowded
        strobing = (
            level == WILD
            and white
            and blocked is None
            and not soft_drop(plan.request.context.arrival_of_peak)
        )
        rows: list[Overlay] = []
        dip = plan.dip_beats if level == CALM else max(plan.dip_beats, DROP_DIP_BEATS)
        if self._shipped(DROP_DIP):
            rows.append(_row(DROP_DIP, -dip, dip, dip=True))
        intense = False

        riser_beats = self._riser_beats(plan, dip)
        if plan.previous is LightIntent.BUILDUP and riser_beats is not None:
            riser = plan.picks.calm_riser if level == CALM else plan.picks.riser
            fits = (
                bool(bpm) and RISER_STROBE_BEATS * 60.0 / bpm <= settings.strobe_max_burst_seconds
            )
            if strobing and plan.picks.strobe_riser is not None and fits:
                riser, intense = plan.picks.strobe_riser, True
            if riser is not None:
                rows.append(
                    _row(riser, -(dip + riser_beats), riser_beats, 0, 4.0, RISER_RELEASE_BEATS)
                )

        hits = [self._shipped(COLOUR_HIT)]
        if white and plan.picks.hits:
            hits = list(plan.picks.hits)
        rows += [_row(hit, 0.0, HIT_BEATS, release=HIT_BEATS) for hit in hits if hit is not None]
        if strobing and plan.picks.strobe_hit is not None:
            intense = True
            beats = strobe_hit_beats(bpm, settings, plan.request.beats_per_bar)
            rows.append(_row(plan.picks.strobe_hit, 0.0, beats))
        lit = [row for row in rows if row.bank == 'hit' and (row.bank, row.autoloop) != COLOUR_HIT]
        events = EntryEvents(intense=intense, accent=bool(lit))

        accent = accents[-1] if plan.drop_number > 1 and accents else None
        if accent is not None:
            meta = self._overlays[accent]
            leaves_heads_alone = meta.intensity_htp and meta.movement == STILL_MOVEMENT
            begin = 0.0 if leaves_heads_alone else HIT_BEATS
            duration = ENTRY_ACCENT_BARS * plan.request.beats_per_bar - begin
            rows.append(_row(accent, begin, duration, 0, MOVER_FADE_BEATS, 4.0))
        return rows, events, accent

    @staticmethod
    def _riser_beats(plan: SectionPlan, dip: float) -> float | None:
        longest = RISER_BARS * plan.request.beats_per_bar
        beats = min(longest, plan.previous_beats - dip, math.floor(plan.request.lead_beats - dip))
        return beats if beats >= MIN_RISER_BEATS else None

    def _drop_fills(self, plan: SectionPlan, level: str, *, bar_8: bool) -> list[Overlay]:
        fill = plan.picks.fill
        if fill is None:
            return []

        def on_bar(loop: tuple[str, str], bar: int) -> Overlay:
            begin = (bar - 1) * plan.request.beats_per_bar
            return _row(loop, begin, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0)

        if level == CALM:
            return [on_bar(fill, PHRASE_BARS)]
        turnaround = self._shipped(TURNAROUND_FILL)
        if turnaround is None or any(self._fights(turnaround, base) for base in plan.choices):
            turnaround = fill
        rows = [on_bar(turnaround, PHRASE_BARS)]
        if bar_8:
            rows.insert(0, on_bar(fill, PHRASE_BARS // 2))
        return rows
