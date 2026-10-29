"""The director: plans each committed section as one Cue, ahead of the room (design 2.6).

Autolux decides names, beats and bars; Starfire places them on its own clock and renders them.
"""

from __future__ import annotations

import dataclasses
import math
import random
import zlib
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Protocol

from autolux.lib.engine.autoloop_policy import (
    AutoloopChoice,
    AutoloopPolicy,
    SelectionContext,
    look_intent,
    soft_drop,
)
from autolux.lib.engine.cue import BaseLook, Cue, Overlay
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
from autolux.lib.engine.section_decoder import BEATS_PER_BAR

# (entries, bars per entry): base looks change on these phrase lines. A buildup keeps one look,
# since a change would reset its tension.
ROTATION = {
    LightIntent.DROP: (2, 16),
    LightIntent.ATMOSPHERIC: (2, 32),
    LightIntent.BREAKDOWN: (2, 32),
    LightIntent.BUILDUP: (1, 16),
}
STEP_CROSSFADE_BEATS = 4.0
BREAKDOWN_CROSSFADE_BEATS = 8.0
BUILDUP_CROSSFADE_BEATS = 1.0

# Higher composes later: the dip darkens everything, a hit lands over the riser it ends.
PRIORITY = {'fill': 10, 'accent': 20, 'riser': 30, 'hit': 40, 'dip': 50}
# Fills and accents move heads LTP, so they never snap a head in or out.
MOVER_FADE_BEATS = 0.5
DROP_DIP = ('dip', 'last_beat')
BREAKDOWN_DIP = ('dip', 'fade')
# Calm's entry: the palette's own punch, never white.
COLOUR_HIT = ('hit', 'colour_bump')
# The bar-16 fill, bigger than the bar-8 one.
TURNAROUND_FILL = ('fill', 'turnaround')
# A texture that leads no single layer and a movement that moves nothing fight nothing.
MIXED_TEXTURE = 'mixed'
STILL_MOVEMENT = 'still'
# Banks that also yield the heads to a base loop moving them the same way.
MOVEMENT_PAIRED_BANKS = ('accent', 'riser')

# The dip before a drop, by the quiet beats before the one: half a beat, a beat, a bar, two bars.
DIP_BEATS = (0.5, 1.0, float(BEATS_PER_BAR), 2.0 * BEATS_PER_BAR)
# A beat this far below the build's loudest is part of the gap; RMS falls on a high-passed
# build, so a filtered roll must stay out of it.
GAP_LEVEL_SHARE = 0.25
# Two bars of dark only for a gap that is close to silence.
SILENT_GAP_SHARE = 0.1
GAP_REFERENCE_BEATS = 16
BREAKDOWN_DIP_BEATS = 2.0

HIT_BEATS = 2.0
RISER_BEATS = 16
# A long build may carry a longer riser, still inside the cue's lead.
LONG_BUILD_BEATS = 32 * BEATS_PER_BAR
LONG_RISER_BEATS = 32
MIN_RISER_BEATS = 4
# A decoder re-decision must not snap a lit riser off; at the drop the dip covers it.
RISER_RELEASE_BEATS = 2.0
# A strobing riser strobes in its last 8 beats; it plays only when those fit the burst cap.
RISER_STROBE_BEATS = 8
FILL_BEATS = 4.0
PHRASE_BARS = 16
# Accents change 8 bars off the base lines: two rows, each 16 bars on in every 32.
ACCENT_OFFSET_BARS = 8
ACCENT_BARS = 16
ENTRY_ACCENT_BARS = 8


class VenueGate(Protocol):
    """The venue's limits at a room time; `moment` is the section's own key, never its own
    obstacle."""

    def intense_blocked(self, at: float, moment: Hashable) -> str | None: ...

    def accent_crowded(self, at: float, moment: Hashable) -> bool: ...


@dataclass(frozen=True)
class SectionRequest:
    """What Autolux knows about a committed section when it plans it."""

    start_beat_index: int
    intent: LightIntent
    context: SelectionContext
    # Monotonic seconds at which the room hears the start.
    room_sec: float
    bpm: float | None = None
    # Analysed beat levels before the start, oldest first.
    levels_before: tuple[float, ...] = ()
    # Beats from planning to the start: no row may begin before Starfire has the cue.
    lead_beats: float = 0.0


@dataclass(frozen=True)
class OverlayPicks:
    """Chosen once per plan, so a level change re-filters rows without a new draw."""

    hit: tuple[str, str] | None = None
    strobe_hit: tuple[str, str] | None = None
    riser: tuple[str, str] | None = None
    strobe_riser: tuple[str, str] | None = None
    fill: tuple[str, str] | None = None
    accents: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EntryEvents:
    """What the section's entry spends of the venue: one moment for the strobing riser, the
    strobing hit and the special entering with them."""

    intense: bool = False
    accent: bool = False


@dataclass(frozen=True)
class SectionPlan:
    request: SectionRequest
    track: int
    section: int
    previous: LightIntent | None
    previous_beats: int
    # 1 for a track's first drop, 2 for its second; 0 for any other section.
    drop_number: int
    # A drop right after a drop is one drop the decoder split: no entry of its own.
    continued: bool
    after_first_drop: bool
    choices: tuple[AutoloopChoice, ...]
    picks: OverlayPicks
    dip_beats: float
    cue: Cue
    events: EntryEvents

    @property
    def look(self) -> LightIntent:
        return self.choices[0].look


def dip_beats(levels_before: Sequence[float]) -> float:
    """The dip a drop earns from the energy gap before it: the quiet beats right before the one,
    against the loudest beat of the build."""
    window = list(levels_before)[-GAP_REFERENCE_BEATS:]
    if not window:
        return DIP_BEATS[0]
    reference = max(window)
    if reference <= 0:
        return DIP_BEATS[-1]  # the build stopped: the room goes dark with it
    gap = []
    for level in reversed(window):
        if level >= GAP_LEVEL_SHARE * reference:
            break
        gap.append(level)
    silent = all(level < SILENT_GAP_SHARE * reference for level in gap)
    choices = DIP_BEATS if silent else DIP_BEATS[:-1]
    return max((beats for beats in choices if beats <= len(gap)), default=DIP_BEATS[0])


def drop_strength(request: SectionRequest) -> float | None:
    """The arrival against the track's peak so far, known before the room hears it."""
    arrival = request.context.arrival_of_peak
    return None if arrival is None else min(1.0, max(0.0, arrival))


def strobe_hit_beats(bpm: float | None, settings: ShowSettings) -> float:
    """The strobe burst setting in whole beats, a beat to a bar."""
    if not bpm or bpm <= 0:
        return 1.0
    seconds = min(settings.strobe_burst_seconds, settings.strobe_max_burst_seconds)
    return float(min(BEATS_PER_BAR, max(1, math.floor(seconds * bpm / 60.0))))


def cue_seed(seed: int, track: int, start_beat_index: int) -> int:
    """Keyed on the start, so a superseded re-plan never shifts a later section's seed."""
    return zlib.crc32(f'{seed}/{track}/{start_beat_index}'.encode())


def _row(
    overlay: tuple[str, str],
    begin: float,
    length: float,
    every_bars: int = 0,
    attack: float = 0.0,
    release: float = 0.0,
) -> Overlay:
    bank, autoloop = overlay
    return Overlay(bank, autoloop, PRIORITY[bank], begin, length, every_bars, attack, release)


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
        """A cleared command queue takes its sections along; the next plan starts a track."""
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
            # The previous track's plans may still be ahead of the room; older ones are done.
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
            dip_beats=dip_beats(request.levels_before) if is_drop else 0.0,
            cue=Cue(
                request.start_beat_index,
                request.intent,
                self._looks(request, choices, settings, previous, continued),
                section_palette(
                    palette, choices[0].look, request.intent, after_first_drop=bool(drops)
                ),
                drop_strength=drop_strength(request) if is_drop else None,
                seed=seed,
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
        """The stored plan at a new level or palette: same start, seed and rotation. A started
        section keeps its one-shot rows and their events as planned; only its recurring rows
        are ever sent again."""
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
            once = tuple(row for row in plan.cue.overlays if row.every_bars == 0)
            again = tuple(row for row in revised.cue.overlays if row.every_bars > 0)
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
            # The drop goes on: its next look shows, not its first again.
            return split_from.choices[1:] + split_from.choices[:1]
        if first_drop is not None and first_drop.look == look:
            # A track's second drop opens on the first drop's look; its added layer is new.
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
                break  # a one-loop bank keeps its one look
            self._policy.record(context, following)
            choices.append(following)
        return tuple(choices)

    def _looks(
        self,
        request: SectionRequest,
        choices: tuple[AutoloopChoice, ...],
        settings: ShowSettings,
        previous: SectionPlan | None,
        continued: bool,
    ) -> tuple[BaseLook, ...]:
        scale = CROSSFADE_SCALE[intensity_level(settings.intensity)]
        _, bars = ROTATION[request.intent]
        step = {'kind': 'crossfade', 'duration_beats': STEP_CROSSFADE_BEATS * scale}
        entry = step if continued else self._entry_transition(request.intent, previous, scale)
        return tuple(
            BaseLook(choice.bank, choice.autoloop, bars, entry if index == 0 else step)
            for index, choice in enumerate(choices)
        )

    @staticmethod
    def _entry_transition(
        intent: LightIntent, previous: SectionPlan | None, scale: float
    ) -> dict | None:
        if intent is LightIntent.DROP:
            return {'kind': 'cut'}  # after the dip, the drop lands on the one
        if intent is LightIntent.BREAKDOWN:
            return {'kind': 'crossfade', 'duration_beats': BREAKDOWN_CROSSFADE_BEATS * scale}
        if intent is LightIntent.BUILDUP:
            if previous is not None and previous.request.intent is LightIntent.DROP:
                return {'kind': 'cut'}
            return {'kind': 'crossfade', 'duration_beats': BUILDUP_CROSSFADE_BEATS}
        return None

    def _bank(
        self,
        bank: str,
        *,
        strobe: bool | None = None,
        under: Sequence[AutoloopChoice] = (),
    ) -> list[tuple[str, str]]:
        """The bank's overlays that play over every base loop `under` without fighting it."""
        return sorted(
            loop
            for loop, meta in self._overlays.items()
            if loop[0] == bank
            and (strobe is None or meta.strobe == strobe)
            and not any(self._fights(loop, choice) for choice in under)
        )

    def _fights(self, overlay: tuple[str, str], base: AutoloopChoice) -> bool:
        """An overlay on the layer the base loop leads with would cover its idea; an accent or
        riser moving the heads the way the base does would take them over."""
        meta = self._overlays[overlay]
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
        """Each overlay once, then the least recently used, as the base rotation does."""
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
        # A fill on an accent's own layer would cut across it.
        taken = {self._overlays[accent].texture for accent in accents} - {MIXED_TEXTURE}
        fills = [
            loop
            for loop in self._bank('fill', under=choices)
            if loop != TURNAROUND_FILL and self._overlays[loop].texture not in taken
        ]
        if intent is not LightIntent.DROP:
            return OverlayPicks(fill=self._pick(fills))
        white_hits = [loop for loop in self._bank('hit', strobe=False) if loop != COLOUR_HIT]
        # The riser plays over the build before the drop, so it pairs with the build's look.
        build = previous.choices if previous is not None else ()
        return OverlayPicks(
            # The entry hit is the track's: a second drop reuses the first drop's.
            hit=first_drop.picks.hit if first_drop is not None else self._pick(white_hits),
            strobe_hit=next(iter(self._bank('hit', strobe=True)), None),
            riser=self._pick(self._bank('riser', strobe=False, under=build)),
            strobe_riser=next(iter(self._bank('riser', strobe=True, under=build)), None),
            fill=self._pick(fills),
            accents=tuple(accents),
        )

    def _with_overlays(
        self, plan: SectionPlan, settings: ShowSettings, gate: VenueGate, moment: Hashable
    ) -> SectionPlan:
        level = intensity_level(settings.intensity)
        intent = plan.request.intent
        events = EntryEvents()
        if intent is LightIntent.DROP:
            rows, events = self._drop_rows(plan, level, settings, gate, moment)
        elif intent is LightIntent.BREAKDOWN:
            rows = []
            if self._shipped(BREAKDOWN_DIP):
                rows.append(_row(BREAKDOWN_DIP, -BREAKDOWN_DIP_BEATS, BREAKDOWN_DIP_BEATS, 0, 1, 4))
            rows += self._fills(plan.picks.fill, level, attack=1.0, release=2.0, calm_every=32)
        elif intent is LightIntent.ATMOSPHERIC:
            rows = self._fills(plan.picks.fill, level, attack=2.0, release=2.0, calm_every=None)
        else:
            rows = []
        cue = dataclasses.replace(plan.cue, overlays=tuple(rows))
        return dataclasses.replace(plan, cue=cue, events=events)

    @staticmethod
    def _fills(
        fill: tuple[str, str] | None,
        level: str,
        *,
        attack: float,
        release: float,
        calm_every: int | None,
    ) -> list[Overlay]:
        """One fill on the last bar of each phrase; calm_every is Calm's slower cadence, None
        for none at Calm."""
        every = calm_every if level == CALM else PHRASE_BARS
        if fill is None or every is None:
            return []
        return [_row(fill, (every - 1) * BEATS_PER_BAR, FILL_BEATS, every, attack, release)]

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
        # Calm keeps half the accents: restraint is smaller, never absent.
        accents = plan.picks.accents[:1] if level == CALM else plan.picks.accents
        entry_accent = None
        if not plan.continued:
            rows, events, entry_accent = self._drop_entry(
                plan, level, settings, gate, moment, accents
            )
        # A single accent arrives with the one already, so no bar-8 fill cuts across it.
        bar_8 = entry_accent is None or len(accents) > 1
        rows += self._drop_fills(plan, level, bar_8=bar_8)
        for index, accent in enumerate(accents):
            begin = (ACCENT_OFFSET_BARS + index * ACCENT_BARS) * BEATS_PER_BAR
            rows.append(_row(accent, begin, ACCENT_BARS * BEATS_PER_BAR, 2 * ACCENT_BARS, 4.0, 4.0))
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
        # A soft drop keeps its white but never strobes, as no intense special plays on it.
        strobing = (
            level == WILD
            and white
            and blocked is None
            and not soft_drop(plan.request.context.arrival_of_peak)
        )
        rows: list[Overlay] = []
        dip = plan.dip_beats
        if self._shipped(DROP_DIP):
            rows.append(_row(DROP_DIP, -dip, dip))
        intense = False

        riser_beats = self._riser_beats(plan)
        if plan.previous is LightIntent.BUILDUP and level != CALM and riser_beats is not None:
            riser = plan.picks.riser
            fits = (
                bool(bpm) and RISER_STROBE_BEATS * 60.0 / bpm <= settings.strobe_max_burst_seconds
            )
            if strobing and plan.picks.strobe_riser is not None and fits:
                riser, intense = plan.picks.strobe_riser, True
            if riser is not None:
                # It closes where the dip opens, so the dark before the one stays dark.
                rows.append(
                    _row(riser, -(dip + riser_beats), riser_beats, 0, 4.0, RISER_RELEASE_BEATS)
                )

        hit = self._shipped(COLOUR_HIT)
        hit_beats, hit_release = HIT_BEATS, HIT_BEATS
        if white and plan.picks.hit is not None:
            hit = plan.picks.hit
        if strobing and plan.picks.strobe_hit is not None:
            hit, hit_release, intense = plan.picks.strobe_hit, 0.0, True
            hit_beats = strobe_hit_beats(bpm, settings)
        if hit is not None:
            rows.append(_row(hit, 0.0, hit_beats, release=hit_release))
        events = EntryEvents(intense=intense, accent=hit is not None and hit != COLOUR_HIT)

        accent = accents[-1] if plan.drop_number > 1 and accents else None
        if accent is not None:
            # The added layer: the texture arrives with the one instead of on bar 8. One that
            # takes the heads (LTP or moving) waits out the hit, so it never moves them lit.
            meta = self._overlays[accent]
            lifts = meta.intensity_htp and meta.movement == STILL_MOVEMENT
            begin = 0.0 if lifts else HIT_BEATS
            length = ENTRY_ACCENT_BARS * BEATS_PER_BAR - begin
            rows.append(_row(accent, begin, length, 0, MOVER_FADE_BEATS, 4.0))
        return rows, events, accent

    @staticmethod
    def _riser_beats(plan: SectionPlan) -> float | None:
        """Within the build and after Starfire has the cue, never inside the dip."""
        dip = plan.dip_beats
        longest = LONG_RISER_BEATS if plan.previous_beats >= LONG_BUILD_BEATS else RISER_BEATS
        beats = min(longest, plan.previous_beats - dip, math.floor(plan.request.lead_beats - dip))
        return beats if beats >= MIN_RISER_BEATS else None

    def _drop_fills(self, plan: SectionPlan, level: str, *, bar_8: bool) -> list[Overlay]:
        fill = plan.picks.fill
        if fill is None:
            return []
        last_bar = (PHRASE_BARS - 1) * BEATS_PER_BAR
        if level == CALM:
            return [_row(fill, last_bar, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0)]
        turnaround = self._shipped(TURNAROUND_FILL)
        if turnaround is None or any(self._fights(turnaround, base) for base in plan.choices):
            turnaround = fill
        rows = [_row(turnaround, last_bar, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0)]
        if bar_8:
            middle = (PHRASE_BARS // 2 - 1) * BEATS_PER_BAR
            rows.insert(0, _row(fill, middle, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0))
        return rows
