from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from autolux.lib.clients.starfire_client import WHOLE_RIG
from autolux.lib.engine.effect_definitions import STROBE_RATE_HZ, LightIntent

INTENSE_SPEED = 0.6
AXES = ('colour', 'speed', 'pulse', 'pattern')


@dataclass(frozen=True)
class Special:
    name: str
    intents: tuple[LightIntent, ...]
    group: str
    colour_slot: str | None = None
    energy: float = 0.0
    speed: float = 0.0
    pulse_beats: int | None = None
    attack_beats: float = 0.0
    release_beats: float = 0.0
    pattern: float | None = None
    strobe_hz: float | None = None

    @property
    def intense(self) -> bool:
        return (
            self.strobe_hz is not None or self.pulse_beats is not None or self.speed > INTENSE_SPEED
        )

    @property
    def axes(self) -> frozenset[str]:
        if self.strobe_hz is not None:
            return frozenset()
        present = {
            'colour': True,
            'speed': self.speed > 0,
            'pulse': self.pulse_beats is not None,
            'pattern': self.pattern is not None,
        }
        return frozenset(axis for axis, used in present.items() if used)

    def wire(self) -> dict:
        return {
            'colour_slot': self.colour_slot,
            'energy': self.energy,
            'speed': self.speed,
            'pulse_beats': self.pulse_beats,
            'attack_beats': self.attack_beats,
            'release_beats': self.release_beats,
            'pattern': self.pattern,
        }


@dataclass(frozen=True)
class SpecialCapabilities:
    groups: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_groups(cls, groups: Iterable[Mapping]) -> SpecialCapabilities:
        return cls(
            {
                group['name']: frozenset(axis for axis in AXES if group.get(axis))
                for group in groups
                if group.get('name') and (group.get('fixture_count') or 0) > 0
            }
        )

    def allows(self, special: Special) -> bool:
        if special.group == WHOLE_RIG:
            return True
        axes = self.groups.get(special.group)
        return axes is not None and special.axes <= axes


@dataclass(frozen=True)
class SpecialChoice:
    special: Special | None
    strobes: tuple[Special, ...] = ()
    suppressed: str | None = None

    @property
    def intense(self) -> bool:
        return bool(self.strobes) or (self.special is not None and self.special.intense)


DROP_STROBE = Special('strobe_burst', (LightIntent.DROP,), WHOLE_RIG, strobe_hz=STROBE_RATE_HZ)
DEFAULT_SPECIALS = (DROP_STROBE,)


class SpecialPolicy:
    def __init__(self, specials: Sequence[Special], rng: random.Random):
        self._specials = tuple(specials)
        self._rng = rng
        self._selections = 0
        self._last_used: dict[str, int] = {}

    def choose(
        self,
        intent: LightIntent,
        capabilities: SpecialCapabilities,
        intense_blocked: str | None = None,
    ) -> SpecialChoice:
        rows = [
            special
            for special in self._specials
            if intent in special.intents and capabilities.allows(special)
        ]
        strobes = tuple(special for special in rows if special.strobe_hz is not None)
        selectable = [special for special in rows if special.strobe_hz is None]
        calm = [special for special in selectable if not special.intense]
        if intense_blocked:
            suppressed = intense_blocked if strobes or len(calm) < len(selectable) else None
            chosen = self._least_recent(calm) if calm else None
            return SpecialChoice(chosen, suppressed=suppressed)
        chosen = self._least_recent(selectable) if selectable else None
        return SpecialChoice(chosen, strobes)

    def record(self, special: Special) -> None:
        self._selections += 1
        self._last_used[special.name] = self._selections

    def _least_recent(self, pool: list[Special]) -> Special:
        unused = [special for special in pool if special.name not in self._last_used]
        if unused:
            return self._rng.choice(unused)
        weights = [self._selections + 1 - self._last_used[special.name] for special in pool]
        return self._rng.choices(pool, weights)[0]
