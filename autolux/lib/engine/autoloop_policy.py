from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from autolux.lib.engine.effect_definitions import (
    CALM,
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    WILD,
    AutoloopMeta,
    AutoloopMetaTable,
    AutoloopTable,
    LightIntent,
    intensity_level,
)

RISING, FLAT, FALLING = 'rising', 'flat', 'falling'
LOUDNESS_TREND_BEATS = 32
LOUDNESS_TREND_STEP = 0.05

LONG_BUILD_BARS = 32
LONG_SINCE_DROP_SECONDS = 90.0
ARRIVAL_BEATS = 4
MAXIMAL_ARRIVAL_OF_PEAK = 0.9
MAX_ENERGY = 3
CALM_INTENT_BANKS: dict[LightIntent, LightIntent] = {
    LightIntent.DROP: LightIntent.BREAKDOWN,
}
CALM_BUILD_ENERGY = 2
WILD_MAX_ENERGY_WEIGHT = 3


@dataclass(frozen=True)
class SelectionContext:
    intent: LightIntent
    seconds_since_drop: float | None = None
    build_bars: float = 0.0
    loudness: str = FLAT
    new_track: bool = False
    arrival_of_peak: float | None = None


@dataclass(frozen=True)
class AutoloopChoice:
    bank: str
    autoloop: str
    meta: AutoloopMeta
    reason: str
    look: LightIntent


TIER_RULES: tuple[tuple[LightIntent, str | None, tuple[int, ...], str], ...] = (
    (LightIntent.DROP, 'arrival_below_peak', (2,), 'arrival below 0.9 of the track peak'),
    (
        LightIntent.DROP,
        'maximal_again',
        (3,),
        'maximal again: 32-bar build, 90 s since the last drop, arrival at the track peak',
    ),
    (LightIntent.DROP, 'wild_maximal_again', (3,), 'wild: maximal twice running allowed'),
    (LightIntent.DROP, 'no_maximal_twice', (2,), 'no maximal drop twice running'),
    (LightIntent.DROP, 'long_build', (3,), 'long build or long since the last drop'),
    (LightIntent.DROP, None, (2,), 'short build'),
    (LightIntent.BUILDUP, 'no_maximal_twice', (2,), 'next drop cannot be maximal'),
    (LightIntent.BUILDUP, 'long_build', (3,), 'leads into a maximal drop'),
    (LightIntent.BUILDUP, None, (2,), 'leads into a driving drop'),
    (LightIntent.BREAKDOWN, 'maximal_drop_last', (1,), 'calm after a maximal drop'),
    (LightIntent.BREAKDOWN, None, (1, 2), 'breakdown'),
    (LightIntent.ATMOSPHERIC, 'loudness_rising', (1, 2), 'loudness rising'),
    (LightIntent.ATMOSPHERIC, None, (1,), 'calm'),
)


def max_energy_for_intensity(intensity: float, look: LightIntent | None = None) -> int:
    if intensity <= INTENSITY_CALM:
        return CALM_BUILD_ENERGY if look is LightIntent.BUILDUP else 1
    if intensity < INTENSITY_BALANCED:
        return 2
    return MAX_ENERGY


def look_intent(intent: LightIntent, intensity: float) -> LightIntent:
    if intensity_level(intensity) == CALM:
        return CALM_INTENT_BANKS.get(intent, intent)
    return intent


def arrival_of_peak(
    beat_levels: Sequence[tuple[float, float]], arrival_sec: float, track_started_sec: float
) -> float | None:
    arriving = [level for at, level in beat_levels if at >= arrival_sec][:ARRIVAL_BEATS]
    if not arriving:
        return None
    arrival = sum(arriving) / len(arriving)
    before = [level for at, level in beat_levels if track_started_sec <= at < arrival_sec]
    peak = max([arrival, *before])
    return arrival / peak if peak > 0 else None


def soft_drop(arrival: float | None) -> bool:
    return arrival is not None and arrival < MAXIMAL_ARRIVAL_OF_PEAK


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

    def choose(
        self,
        context: SelectionContext,
        exclude: tuple[str, str] | None = None,
        intensity: float = INTENSITY_BALANCED,
        contrast: AutoloopMeta | None = None,
    ) -> AutoloopChoice:
        look = look_intent(context.intent, intensity)
        wild = intensity_level(intensity) == WILD
        loops = [loop for loop in self._autoloops[look] if loop != exclude]
        if not loops:
            raise ValueError(f'{look.name} has no other autoloop')
        tiers, reason = self._tiers(context, wild)
        max_energy = max_energy_for_intensity(intensity, look)
        if max(tiers) > max_energy:
            tiers = tuple(sorted({min(tier, max_energy) for tier in tiers}))
            reason = f'{reason}; intensity caps energy at {max_energy}'
        if look != context.intent:
            reason = f'{reason}; calm plays it from the {look.value} bank'
        if wild and MAX_ENERGY not in tiers:
            tiers = (*tiers, MAX_ENERGY)
            reason = f'{reason}; wild adds energy {MAX_ENERGY}'
        candidates, exact = self._nearest_tier(loops, tiers)
        if not exact:
            energies = sorted({self._meta[loop].energy for loop in candidates})
            reason = (
                f'{reason}; no energy {"/".join(map(str, tiers))} loop, '
                f'nearest {"/".join(map(str, energies))}'
            )
        if contrast is not None:
            candidates, reason = self._contrasting(loops, candidates, tiers, contrast, reason)
        candidates = self._narrowed(candidates, look)
        bank, name = self._weighted_pick(candidates, look, wild)
        return AutoloopChoice(bank, name, self._meta[(bank, name)], reason, look)

    def record(self, context: SelectionContext, choice: AutoloopChoice) -> None:
        loop = choice.bank, choice.autoloop
        self._selections[choice.look] += 1
        self._last_used[loop] = self._selections[choice.look]
        self._last_by_intent[choice.look] = loop
        if context.intent == LightIntent.DROP:
            self._last_drop_energy = choice.meta.energy

    def _tiers(self, context: SelectionContext, wild: bool) -> tuple[tuple[int, ...], str]:
        since_drop = context.seconds_since_drop
        long_since_drop = since_drop is None or since_drop >= LONG_SINCE_DROP_SECONDS
        long_build = context.build_bars >= LONG_BUILD_BARS
        arrival = context.arrival_of_peak
        maximal_arrival = arrival is not None and not soft_drop(arrival)
        maximal_drop_last = self._last_drop_energy == 3
        facts = {
            None: True,
            'arrival_below_peak': soft_drop(arrival),
            'maximal_again': maximal_drop_last
            and long_build
            and long_since_drop
            and maximal_arrival,
            'maximal_drop_last': maximal_drop_last,
            'wild_maximal_again': wild and maximal_drop_last,
            'no_maximal_twice': maximal_drop_last and not wild,
            'long_build': long_build or long_since_drop,
            'loudness_rising': context.loudness == RISING,
        }
        for intent, fact, tiers, reason in TIER_RULES:
            if intent == context.intent and facts[fact]:
                if intent == LightIntent.DROP and arrival is not None:
                    reason = f'{reason}; arrival {arrival:.2f} of the track peak'
                return tiers, reason
        raise AssertionError(f'no tier rule for {context.intent}')

    def _nearest_tier(self, loops, tiers) -> tuple[list[tuple[str, str]], bool]:
        def distance(loop):
            return min(abs(self._meta[loop].energy - tier) for tier in tiers)

        nearest = min(distance(loop) for loop in loops)
        return [loop for loop in loops if distance(loop) == nearest], nearest == 0

    def _contrasting(
        self, loops, candidates, tiers, contrast: AutoloopMeta, reason: str
    ) -> tuple[list[tuple[str, str]], str]:

        def differs_in(loop) -> int:
            meta = self._meta[loop]
            return (meta.movement != contrast.movement) + (meta.texture != contrast.texture)

        def strongest(pool):
            best = max((differs_in(loop) for loop in pool), default=0)
            return [loop for loop in pool if differs_in(loop) == best] if best else []

        against = f'{contrast.movement}/{contrast.texture}'
        within = strongest(candidates)
        if within:
            return within, f'{reason}; contrasts {against}'
        others = [loop for loop in loops if differs_in(loop)]
        if not others:
            return candidates, f'{reason}; nothing contrasts {against}'
        neighbours, _ = self._nearest_tier(others, tiers)
        neighbours = strongest(neighbours)
        energies = '/'.join(
            str(e) for e in sorted({self._meta[loop].energy for loop in neighbours})
        )
        return neighbours, f'{reason}; contrasts {against} from energy {energies}'

    def _narrowed(self, candidates, intent: LightIntent) -> list[tuple[str, str]]:
        last = self._last_by_intent.get(intent)
        return [loop for loop in candidates if loop != last] or candidates

    def _weighted_pick(self, candidates, intent: LightIntent, wild: bool) -> tuple[str, str]:
        unused = [loop for loop in candidates if loop not in self._last_used]
        pool = unused or candidates
        count = self._selections[intent]
        weights = [1 if loop in unused else count + 1 - self._last_used[loop] for loop in pool]
        if wild:
            weights = [
                weight * WILD_MAX_ENERGY_WEIGHT if self._meta[loop].energy == MAX_ENERGY else weight
                for weight, loop in zip(weights, pool)
            ]
        return self._rng.choices(pool, weights)[0]
