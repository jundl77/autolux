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
# Pairs the shared texture does not catch: the overlay fights these loops' lead layer.
CLASHES: dict[tuple[str, str], frozenset[tuple[str, str]]] = {
    ('accent', 'mover_shapes'): frozenset({('drop', 'ballyhoo'), ('drop', 'wide_fan')}),
    ('riser', 'strip_fill'): frozenset({('buildup', 'countdown')}),
}
# A texture that leads no single layer, so it fights none.
MIXED_TEXTURE = 'mixed'

# The dip before a drop, by the quiet beats before the one: half a beat, a beat, a bar, two bars.
DIP_BEATS = (0.5, 1.0, float(BEATS_PER_BAR), 2.0 * BEATS_PER_BAR)
# A beat this far below the build's loudest is part of the gap.
GAP_LEVEL_SHARE = 0.5
GAP_REFERENCE_BEATS = 16
BREAKDOWN_DIP_BEATS = 2.0

HIT_BEATS = 2.0
RISER_BEATS = 16
MIN_RISER_BEATS = 4
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
    reference = max(window, default=0.0)
    gap = 0
    for level in reversed(window):
        if level >= GAP_LEVEL_SHARE * reference:
            break
        gap += 1
    return max((beats for beats in DIP_BEATS if beats <= gap), default=DIP_BEATS[0])


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


def cue_seed(seed: int, track: int, section: int) -> int:
    return zlib.crc32(f'{seed}/{track}/{section}'.encode())


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
        choices = self._rotation(request, settings, first_drop if is_drop else None)
        picks = self._picks_for(request.intent, first_drop, choices, previous)
        seed = cue_seed(self._seed, self._track, self._sections)
        draft = SectionPlan(
            request=request,
            track=self._track,
            section=self._sections,
            previous=previous and previous.request.intent,
            previous_beats=0
            if previous is None
            else request.start_beat_index - previous.cue.start_beat_index,
            drop_number=len(drops) + 1 if is_drop else 0,
            after_first_drop=bool(drops),
            choices=choices,
            picks=picks,
            dip_beats=dip_beats(request.levels_before) if is_drop else 0.0,
            cue=Cue(
                request.start_beat_index,
                request.intent,
                self._looks(request, choices, settings, previous),
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
        section keeps its one-shot rows and their events, since Starfire never re-fires them."""
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
        self, request: SectionRequest, settings: ShowSettings, first_drop: SectionPlan | None
    ) -> tuple[AutoloopChoice, ...]:
        context, intensity = request.context, settings.intensity
        if first_drop is not None and first_drop.look == look_intent(request.intent, intensity):
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
    ) -> tuple[BaseLook, ...]:
        scale = CROSSFADE_SCALE[intensity_level(settings.intensity)]
        _, bars = ROTATION[request.intent]
        entry = self._entry_transition(request.intent, previous, scale)
        step = {'kind': 'crossfade', 'duration_beats': STEP_CROSSFADE_BEATS * scale}
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
        """An accent or riser on the layer the base loop leads with would cover its idea."""
        if (base.bank, base.autoloop) in CLASHES.get(overlay, frozenset()):
            return True
        texture = self._overlays[overlay].texture
        return texture != MIXED_TEXTURE and texture == base.meta.texture

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
        fills = [loop for loop in self._bank('fill') if loop != TURNAROUND_FILL]
        if intent is not LightIntent.DROP:
            fill = self._pick(fills) if intent is not LightIntent.BUILDUP else None
            return OverlayPicks(fill=fill)
        white_hits = [loop for loop in self._bank('hit', strobe=False) if loop != COLOUR_HIT]
        # The riser plays over the build before the drop, so it pairs with the build's look.
        build = previous.choices if previous is not None else ()
        accents: list[tuple[str, str]] = []
        for _ in range(2):
            pool = [loop for loop in self._bank('accent', under=choices) if loop not in accents]
            accent = self._pick(pool)
            if accent is not None:
                accents.append(accent)
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
        at, bpm = plan.request.room_sec, plan.request.bpm
        blocked = gate.intense_blocked(at, moment)
        crowded = gate.accent_crowded(at, moment)
        white = level != CALM and not crowded
        strobing = level == WILD and white and blocked is None
        rows: list[Overlay] = []
        dip = plan.dip_beats
        if self._shipped(DROP_DIP):
            rows.append(_row(DROP_DIP, -dip, dip))
        intense = False

        riser_beats = min(RISER_BEATS, plan.previous_beats)
        if (
            plan.previous is LightIntent.BUILDUP
            and level != CALM
            and riser_beats >= MIN_RISER_BEATS
        ):
            riser = plan.picks.riser
            fits = (
                bool(bpm) and RISER_STROBE_BEATS * 60.0 / bpm <= settings.strobe_max_burst_seconds
            )
            if strobing and plan.picks.strobe_riser is not None and fits:
                riser, intense = plan.picks.strobe_riser, True
            if riser is not None:
                # It closes where the dip opens, so the dark before the one stays dark.
                rows.append(_row(riser, -(dip + riser_beats), riser_beats, attack=4.0))

        hit = self._shipped(COLOUR_HIT)
        hit_beats, hit_release = HIT_BEATS, HIT_BEATS
        if white and plan.picks.hit is not None:
            hit = plan.picks.hit
        if strobing and plan.picks.strobe_hit is not None:
            hit, hit_release, intense = plan.picks.strobe_hit, 0.0, True
            hit_beats = strobe_hit_beats(bpm, settings)
        if hit is not None:
            rows.append(_row(hit, 0.0, hit_beats, release=hit_release))
        accent_event = hit is not None and hit != COLOUR_HIT

        accents = plan.picks.accents
        if plan.drop_number > 1 and accents:
            # The added layer: the texture arrives with the one instead of on bar 8.
            rows.append(
                _row(accents[-1], 0.0, ENTRY_ACCENT_BARS * BEATS_PER_BAR, 0, MOVER_FADE_BEATS, 4.0)
            )
        rows += self._drop_fills(plan.picks.fill, level)
        for index, accent in enumerate(accents):
            begin = (ACCENT_OFFSET_BARS + index * ACCENT_BARS) * BEATS_PER_BAR
            rows.append(_row(accent, begin, ACCENT_BARS * BEATS_PER_BAR, 2 * ACCENT_BARS, 4.0, 4.0))
        return rows, EntryEvents(intense=intense, accent=accent_event)

    def _drop_fills(self, fill: tuple[str, str] | None, level: str) -> list[Overlay]:
        if fill is None:
            return []
        last_bar = (PHRASE_BARS - 1) * BEATS_PER_BAR
        if level == CALM:
            return [_row(fill, last_bar, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0)]
        middle = (PHRASE_BARS // 2 - 1) * BEATS_PER_BAR
        turnaround = self._shipped(TURNAROUND_FILL) or fill
        return [
            _row(fill, middle, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0),
            _row(turnaround, last_bar, FILL_BEATS, PHRASE_BARS, MOVER_FADE_BEATS, 1.0),
        ]
