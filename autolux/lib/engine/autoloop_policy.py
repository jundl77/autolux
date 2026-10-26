"""Choose the autoloop for an intent from the metadata each loop declares about itself."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from autolux.lib.engine.effect_definitions import AutoloopTable, LightIntent

# The vocabulary of the header block in `autoloops/README.md`.
ENERGIES = (1, 2, 3)
FAMILIES = ('cold', 'warm', 'accent')
MOVEMENTS = ('still', 'sweep', 'snap')

RISING, FLAT, FALLING = 'rising', 'flat', 'falling'
LOUDNESS_TREND_BEATS = 32  # eight bars
# On the 0..1 normalized level (36 dB wide), so about 2 dB between the halves.
LOUDNESS_TREND_STEP = 0.05

LONG_BUILD_BARS = 32
LONG_SINCE_DROP_SECONDS = 90.0
FOLLOW_FAMILY_CHANCE = 0.5
FAMILY_WEIGHT = 3.0
# Loops picked at the same instant still need a positive weight.
MIN_AGE_SECONDS = 1e-3


@dataclass(frozen=True)
class AutoloopMeta:
    energy: int
    family: str
    movement: str

    def __post_init__(self):
        for name, allowed in (
            ('energy', ENERGIES),
            ('family', FAMILIES),
            ('movement', MOVEMENTS),
        ):
            if getattr(self, name) not in allowed:
                raise ValueError(
                    f'{name} {getattr(self, name)!r} is not one of {", ".join(map(str, allowed))}'
                )


AutoloopMetaTable = Mapping[tuple[str, str], AutoloopMeta]

# Headers of the four originals in DEFAULT_AUTOLOOPS, for tests and offline tools
# that run without reading the library; a test keeps them equal to the files.
DEFAULT_AUTOLOOP_META: AutoloopMetaTable = {
    ('atmospheric', 'cold_breathe'): AutoloopMeta(1, 'cold', 'sweep'),
    ('breakdown', 'wide_space'): AutoloopMeta(1, 'accent', 'sweep'),
    ('buildup', 'ramp'): AutoloopMeta(2, 'warm', 'sweep'),
    ('drop', 'kick'): AutoloopMeta(3, 'warm', 'snap'),
}


@dataclass(frozen=True)
class SelectionContext:
    """What the engine knows about the music when an intent enters the room."""

    intent: LightIntent
    now: float  # monotonic seconds
    seconds_since_drop: float | None = None  # None before the first drop
    # Bars of breakdown and buildup played back to back up to this entry.
    build_bars: float = 0.0
    loudness: str = FLAT
    new_track: bool = False


@dataclass(frozen=True)
class AutoloopChoice:
    bank: str
    autoloop: str
    meta: AutoloopMeta
    reason: str


# Tier rules, first match wins. Energy is about contrast over a set, not the
# level of one section: a maximal drop only lands after a long build (or a long
# wait since the last drop) and never twice running, so the next one still has
# headroom; a buildup is lit for the drop it promises; a breakdown after a
# maximal drop goes calm so the room hears the contrast; an intro or outro
# stays calm unless the music is already getting louder.
TIER_RULES: tuple[tuple[LightIntent, str | None, tuple[int, ...], str], ...] = (
    (LightIntent.DROP, 'maximal_drop_last', (2,), 'no maximal drop twice running'),
    (LightIntent.DROP, 'long_build', (3,), 'long build or long since the last drop'),
    (LightIntent.DROP, None, (2,), 'short build'),
    (LightIntent.BUILDUP, 'maximal_drop_last', (2,), 'next drop cannot be maximal'),
    (LightIntent.BUILDUP, 'long_build', (3,), 'leads into a maximal drop'),
    (LightIntent.BUILDUP, None, (2,), 'leads into a driving drop'),
    (LightIntent.BREAKDOWN, 'maximal_drop_last', (1,), 'calm after a maximal drop'),
    (LightIntent.BREAKDOWN, None, (1, 2), 'breakdown'),
    (LightIntent.ATMOSPHERIC, 'loudness_rising', (1, 2), 'loudness rising'),
    (LightIntent.ATMOSPHERIC, None, (1,), 'calm'),
)


def loudness_trend(levels: Sequence[float]) -> str:
    """Compare the newer half of the last eight bars of beat levels with the older half."""
    levels = list(levels)[-LOUDNESS_TREND_BEATS:]
    if len(levels) < LOUDNESS_TREND_BEATS:
        return FLAT
    half = len(levels) // 2
    change = sum(levels[half:]) / (len(levels) - half) - sum(levels[:half]) / half
    if change > LOUDNESS_TREND_STEP:
        return RISING
    if change < -LOUDNESS_TREND_STEP:
        return FALLING
    return FLAT


class AutoloopPolicy:
    """Remembers what played, so it is pure given its seeded random source."""

    def __init__(self, autoloops: AutoloopTable, meta: AutoloopMetaTable, rng: random.Random):
        missing = sorted(
            f'{bank}/{name}'
            for loops in autoloops.values()
            for bank, name in loops
            if (bank, name) not in meta
        )
        if missing:
            raise ValueError(f'autoloops without metadata: {", ".join(missing)}')
        self._autoloops = autoloops
        self._meta = meta
        self._rng = rng
        self._last_used: dict[tuple[str, str], float] = {}
        self._last_by_intent: dict[LightIntent, tuple[str, str]] = {}
        self._last_drop_energy: int | None = None
        self._track_family: str | None = None
        self._track_movement: str | None = None

    @property
    def track_family(self) -> str | None:
        return self._track_family

    def choose(self, context: SelectionContext) -> AutoloopChoice:
        if context.new_track or self._track_family is None:
            self._start_track()
        tiers, reason = self._tiers(context)
        loops = self._autoloops[context.intent]
        candidates, exact = self._nearest_tier(loops, tiers)
        if not exact:
            energies = sorted({self._meta[loop].energy for loop in candidates})
            reason = (
                f'{reason}; no energy {"/".join(map(str, tiers))} loop, '
                f'nearest {"/".join(map(str, energies))}'
            )
        candidates = self._narrowed(candidates, context.intent)
        loop = self._weighted_pick(candidates, context.now)

        meta = self._meta[loop]
        self._last_used[loop] = context.now
        self._last_by_intent[context.intent] = loop
        self._track_movement = meta.movement
        if context.intent == LightIntent.DROP:
            self._last_drop_energy = meta.energy
        return AutoloopChoice(loop[0], loop[1], meta, reason)

    def _start_track(self) -> None:
        # A set often stays in one colour world across a mix; the coin keeps it
        # from locking there for the whole night.
        previous = self._track_family
        if previous is None or self._rng.random() >= FOLLOW_FAMILY_CHANCE:
            self._track_family = self._rng.choice(FAMILIES)
        self._track_movement = None

    def _tiers(self, context: SelectionContext) -> tuple[tuple[int, ...], str]:
        since_drop = context.seconds_since_drop
        facts = {
            None: True,
            'maximal_drop_last': self._last_drop_energy == 3,
            'long_build': context.build_bars >= LONG_BUILD_BARS
            or since_drop is None
            or since_drop >= LONG_SINCE_DROP_SECONDS,
            'loudness_rising': context.loudness == RISING,
        }
        for intent, fact, tiers, reason in TIER_RULES:
            if intent == context.intent and facts[fact]:
                return tiers, reason
        raise AssertionError(f'no tier rule for {context.intent}')

    def _nearest_tier(self, loops, tiers) -> tuple[list[tuple[str, str]], bool]:
        def distance(loop):
            return min(abs(self._meta[loop].energy - tier) for tier in tiers)

        nearest = min(distance(loop) for loop in loops)
        return [loop for loop in loops if distance(loop) == nearest], nearest == 0

    def _narrowed(self, candidates, intent: LightIntent) -> list[tuple[str, str]]:
        # Each filter only applies while something is left to choose from.
        last = self._last_by_intent.get(intent)
        filters = [lambda loop: loop != last]
        if intent != LightIntent.DROP and self._track_movement == 'still':
            # A parked rig that suddenly jumps reads as a glitch, except on a drop.
            filters.append(lambda loop: self._meta[loop].movement != 'snap')
        for keep in filters:
            kept = [loop for loop in candidates if keep(loop)]
            candidates = kept or candidates
        return candidates

    def _weighted_pick(self, candidates, now: float) -> tuple[str, str]:
        # Unplayed loops go first, so a tier is exhausted before anything repeats;
        # after that the longer a loop has rested, the likelier it returns.
        unused = [loop for loop in candidates if loop not in self._last_used]
        pool = unused or candidates
        weights = [
            (1.0 if loop in unused else max(now - self._last_used[loop], MIN_AGE_SECONDS))
            * (FAMILY_WEIGHT if self._meta[loop].family == self._track_family else 1.0)
            for loop in pool
        ]
        return self._rng.choices(pool, weights)[0]
