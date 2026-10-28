from __future__ import annotations

import dataclasses
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from autolux.lib.clients.starfire_client import WHOLE_RIG
from autolux.lib.engine.effect_definitions import STROBE_RATE_HZ, LightIntent
from autolux.lighting.mvr_schema import ACCENT_ROLE, LOOP_ROLE

INTENSE_SPEED = 0.6
ACCENT_WINDOW_SECONDS = 60.0
VENUE_GROUP = 'venue'
AXES = ('colour', 'speed', 'pulse', 'pattern')


@dataclass(frozen=True)
class Special:
    name: str
    intents: tuple[LightIntent, ...]
    # The group it plays on; a role row leaves it empty and plays on each capable group of
    # its role, so a new fixture group inherits the rows its layer and profiles allow.
    group: str = ''
    role: str | None = None
    # A role row narrowed to the groups whose members all share one stage area in this zone
    # (column) or depth (row).
    zone: str | None = None
    depth: str | None = None
    colour_slot: str | None = None
    energy: float = 0.0
    speed: float = 0.0
    pulse_beats: int | None = None
    attack_beats: float = 0.0
    release_beats: float = 0.0
    pattern: float | None = None
    strobe_hz: float | None = None
    # Never on the wire: how long the row's group rests after it fires; no row on that group
    # is selectable meanwhile, since the fixtures are what the room tires of, not the recipe.
    cooldown_seconds: float = 0.0

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
    roles: Mapping[str, str] = field(default_factory=dict)
    # group -> (zone, depth, centre X in metres) of the members' shared stage area; both None
    # when the members' areas differ.
    places: Mapping[str, tuple[str | None, str | None, float]] = field(default_factory=dict)

    @classmethod
    def from_groups(cls, groups: Iterable[Mapping]) -> SpecialCapabilities:
        populated = [
            group for group in groups if group.get('name') and (group.get('cell_count') or 0) > 0
        ]
        return cls(
            {
                group['name']: frozenset(axis for axis in AXES if group.get(axis))
                for group in populated
            },
            {group['name']: group.get('role') or LOOP_ROLE for group in populated},
            {
                group['name']: (group.get('zone'), group.get('depth'), group.get('x') or 0.0)
                for group in populated
            },
        )

    def role(self, group: str) -> str:
        return self.roles.get(group, LOOP_ROLE)

    def x(self, group: str) -> float:
        return self.places.get(group, (None, None, 0.0))[2]

    def _lies_in(self, group: str, special: Special) -> bool:
        zone, depth, _ = self.places.get(group, (None, None, 0.0))
        return special.zone in (None, zone) and special.depth in (None, depth)

    def allows(self, special: Special) -> bool:
        if special.group == WHOLE_RIG:
            return True
        axes = self.groups.get(special.group)
        return axes is not None and special.axes <= axes

    def targets(self, special: Special) -> list[Special]:
        """The row on each group it can play on: its own group, or every capable one of its role."""
        if special.role is None:
            return [special] if self.allows(special) else []
        placed = (
            dataclasses.replace(special, group=group)
            for group in sorted(self.groups)
            if self.role(group) == special.role and self._lies_in(group, special)
        )
        return [row for row in placed if self.allows(row)]


@dataclass(frozen=True)
class SpecialChoice:
    special: Special | None
    strobes: tuple[Special, ...] = ()
    suppressed: str | None = None

    @property
    def intense(self) -> bool:
        return bool(self.strobes) or (self.special is not None and self.special.intense)


@dataclass(frozen=True)
class Venue:
    """Room-wide limits on top of each group's own rest."""

    # Specials fired on accent groups in any 60 s, whichever groups; strobe rows do not count.
    # At least 1; remove the accent rows to keep the accents dark.
    max_accent_events_per_minute: int = 2


DROP_STROBE = Special('strobe_burst', (LightIntent.DROP,), WHOLE_RIG, strobe_hz=STROBE_RATE_HZ)
DEFAULT_SPECIALS = (DROP_STROBE,)


class SpecialPolicy:
    def __init__(self, specials: Sequence[Special], rng: random.Random, venue: Venue | None = None):
        self._specials = tuple(specials)
        self._rng = rng
        self._venue = venue or Venue()
        self._accent_fired: list[float] = []  # when each accent special fired, oldest first
        self._selections = 0
        self._last_used: dict[tuple[str, str], int] = {}
        # group -> the row that last fired there and when; the group rests that row's cooldown.
        self._group_fired: dict[str, tuple[Special, float]] = {}
        # The accent group that fired last and its X, and when each accent group last fired.
        self._last_accent: tuple[str, float] | None = None
        self._accent_order: dict[str, int] = {}

    def choose(
        self,
        intent: LightIntent,
        capabilities: SpecialCapabilities,
        intense_blocked: str | None = None,
        soft_drop: str | None = None,
        *,
        now: float = 0.0,
    ) -> SpecialChoice:
        rows = [
            row
            for special in self._specials
            if intent in special.intents
            for row in capabilities.targets(special)
        ]
        strobes = tuple(special for special in rows if special.strobe_hz is not None)
        crowded = self._accents_within(now) >= self._venue.max_accent_events_per_minute
        selectable = [
            special
            for special in rows
            if special.strobe_hz is None
            and not self._cooling(special, now)
            and not (crowded and capabilities.role(special.group) == ACCENT_ROLE)
        ]
        calm = [special for special in selectable if not special.intense]
        if intense_blocked:
            suppressed = intense_blocked if strobes or len(calm) < len(selectable) else None
            chosen = self._pick(calm, capabilities) if calm else None
            return SpecialChoice(chosen, suppressed=suppressed)
        if soft_drop:
            # A soft drop keeps its strobe burst but not an intense special on top of it.
            suppressed = soft_drop if len(calm) < len(selectable) else None
            chosen = self._pick(calm, capabilities) if calm else None
            return SpecialChoice(chosen, strobes, suppressed)
        chosen = self._pick(selectable, capabilities) if selectable else None
        return SpecialChoice(chosen, strobes)

    def record(
        self,
        special: Special,
        now: float = 0.0,
        capabilities: SpecialCapabilities | None = None,
    ) -> None:
        self._selections += 1
        self._last_used[special.name, special.group] = self._selections
        self._group_fired[special.group] = (special, now)
        if capabilities is not None and capabilities.role(special.group) == ACCENT_ROLE:
            self._accent_fired = [*self._recent_accents(now), now]
            self._last_accent = (special.group, capabilities.x(special.group))
            self._accent_order[special.group] = self._selections

    def _recent_accents(self, now: float) -> list[float]:
        fired = list(self._accent_fired)  # replaced, never mutated, so the snapshot reads safely
        return [at for at in fired if now - at < ACCENT_WINDOW_SECONDS]

    def _accents_within(self, now: float) -> int:
        return len(self._recent_accents(now))

    def cooldowns(
        self,
        now: float,
        intense_total: float,
        intense_remaining: float,
        capabilities: SpecialCapabilities | None = None,
    ) -> list[dict]:
        """One entry per group with special rows, plus each strobe row on the shared intense one."""
        fired = dict(self._group_fired)  # read from the snapshot thread while the engine records
        capabilities = capabilities or SpecialCapabilities()
        groups = dict.fromkeys(
            row.group
            for special in self._specials
            if special.strobe_hz is None
            for row in (capabilities.targets(special) if special.role else [special])
        )
        entries = []
        for group in groups:
            last, at = fired.get(group, (None, now))
            total = last.cooldown_seconds if last else 0.0
            entries.append(_entry(group, last and last.name, total, total - (now - at)))
        for strobe in (s for s in self._specials if s.strobe_hz is not None):
            entry = _entry(strobe.group, strobe.name, intense_total, intense_remaining)
            entries.append({**entry, 'strobe': True})
        if ACCENT_ROLE in capabilities.roles.values():
            entries.append(self._venue_entry(now))
        return entries

    def _venue_entry(self, now: float) -> dict:
        """The accent density cap as one more bar: full while under the cap, else draining
        until the oldest accent in the window leaves it."""
        recent, cap = self._recent_accents(now), self._venue.max_accent_events_per_minute
        remaining = ACCENT_WINDOW_SECONDS - (now - recent[-cap]) if len(recent) >= cap else 0.0
        return {
            **_entry(VENUE_GROUP, None, ACCENT_WINDOW_SECONDS, remaining),
            'label': f'{VENUE_GROUP} · accents {len(recent)}/{cap}',
        }

    def _cooling(self, special: Special, now: float) -> bool:
        last = self._group_fired.get(special.group)
        return last is not None and now - last[1] < last[0].cooldown_seconds

    def _pick(self, pool: list[Special], capabilities: SpecialCapabilities) -> Special:
        """The seed picks a row; when it lands on an accent and several accent groups are
        ready, space picks the group: the house-left-most first, then each time the ready
        group furthest in X from the last accent, so accents ping-pong across the room. The
        seeded row plays there if that group has it, else the group's least recently used row."""
        chosen = self._least_recent(pool)
        accents = {s.group for s in pool if capabilities.role(s.group) == ACCENT_ROLE}
        if chosen.group not in accents or len(accents) < 2:
            return chosen
        last = self._last_accent

        def order(group: str):
            x = capabilities.x(group)
            reach = x if last is None else -abs(x - last[1])
            # Equal reach: the group that has waited longest, then house left first.
            return (reach, self._accent_order.get(group, -1), x, group)

        group = min(accents, key=order)
        rows = [s for s in pool if s.group == group]
        same = [s for s in rows if s.name == chosen.name]
        return same[0] if same else self._least_recent(rows)

    def _least_recent(self, pool: list[Special]) -> Special:
        unused = [s for s in pool if (s.name, s.group) not in self._last_used]
        if unused:
            return self._rng.choice(unused)
        weights = [self._selections + 1 - self._last_used[s.name, s.group] for s in pool]
        return self._rng.choices(pool, weights)[0]


def _entry(group: str, last: str | None, total: float, remaining: float) -> dict:
    remaining = max(0.0, remaining)
    return {
        'group': group,
        'last': last,
        'cooldown_seconds': total,
        'remaining_seconds': remaining,
        'ready': remaining <= 0.0,
    }
