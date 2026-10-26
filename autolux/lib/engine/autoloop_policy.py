from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from autolux.lib.engine.effect_definitions import (
    FAMILIES,
    AutoloopMeta,
    AutoloopMetaTable,
    AutoloopTable,
    LightIntent,
)

RISING, FLAT, FALLING = 'rising', 'flat', 'falling'
LOUDNESS_TREND_BEATS = 32
LOUDNESS_TREND_STEP = 0.05

LONG_BUILD_BARS = 32
LONG_SINCE_DROP_SECONDS = 90.0
FOLLOW_FAMILY_CHANCE = 0.5
FAMILY_WEIGHT = 3.0


@dataclass(frozen=True)
class SelectionContext:
    intent: LightIntent
    seconds_since_drop: float | None = None
    build_bars: float = 0.0
    loudness: str = FLAT
    new_track: bool = False


@dataclass(frozen=True)
class AutoloopChoice:
    bank: str
    autoloop: str
    meta: AutoloopMeta
    reason: str
    track_family: str


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
        self._selections: Counter[LightIntent] = Counter()
        self._last_used: dict[tuple[str, str], int] = {}
        self._last_by_intent: dict[LightIntent, tuple[str, str]] = {}
        self._last_drop_energy: int | None = None
        self._track_family: str | None = None
        self._track_movement: str | None = None

    @property
    def track_family(self) -> str | None:
        return self._track_family

    def choose(self, context: SelectionContext) -> AutoloopChoice:
        new_track = context.new_track or self._track_family is None
        family = self._next_track_family() if new_track else self._track_family
        movement = None if new_track else self._track_movement
        tiers, reason = self._tiers(context)
        candidates, exact = self._nearest_tier(self._autoloops[context.intent], tiers)
        if not exact:
            energies = sorted({self._meta[loop].energy for loop in candidates})
            reason = (
                f'{reason}; no energy {"/".join(map(str, tiers))} loop, '
                f'nearest {"/".join(map(str, energies))}'
            )
        candidates = self._narrowed(candidates, context.intent, movement)
        bank, name = self._weighted_pick(candidates, context.intent, family)
        return AutoloopChoice(bank, name, self._meta[(bank, name)], reason, family)

    def record(self, context: SelectionContext, choice: AutoloopChoice) -> None:
        loop = choice.bank, choice.autoloop
        self._selections[context.intent] += 1
        self._last_used[loop] = self._selections[context.intent]
        self._last_by_intent[context.intent] = loop
        self._track_family = choice.track_family
        self._track_movement = choice.meta.movement
        if context.intent == LightIntent.DROP:
            self._last_drop_energy = choice.meta.energy

    def _next_track_family(self) -> str:
        previous = self._track_family
        if previous is not None and self._rng.random() < FOLLOW_FAMILY_CHANCE:
            return previous
        return self._rng.choice([family for family in FAMILIES if family != previous])

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

    def _narrowed(self, candidates, intent: LightIntent, movement) -> list[tuple[str, str]]:
        last = self._last_by_intent.get(intent)
        filters = [lambda loop: loop != last]
        if intent != LightIntent.DROP and movement == 'still':
            filters.append(lambda loop: self._meta[loop].movement != 'snap')
        for keep in filters:
            kept = [loop for loop in candidates if keep(loop)]
            candidates = kept or candidates
        return candidates

    def _weighted_pick(self, candidates, intent: LightIntent, family) -> tuple[str, str]:
        unused = [loop for loop in candidates if loop not in self._last_used]
        pool = unused or candidates
        count = self._selections[intent]
        weights = [
            (1 if loop in unused else count + 1 - self._last_used[loop])
            * (FAMILY_WEIGHT if self._meta[loop].family == family else 1.0)
            for loop in pool
        ]
        return self._rng.choices(pool, weights)[0]
