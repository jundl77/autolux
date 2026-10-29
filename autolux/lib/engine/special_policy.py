from __future__ import annotations

import dataclasses
import random
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from autolux.lib.clients.starfire_client import WHOLE_RIG
from autolux.lib.engine.cue import Envelope
from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lighting.mvr_schema import ACCENT_ROLE, LOOP_ROLE

INTENSE_SPEED = 0.6
ACCENT_WINDOW_SECONDS = 60.0
VENUE_GROUP = 'venue'
STROBE_COOLDOWN_LABEL = 'strobe'
AXES = ('colour', 'speed', 'pulse', 'pattern')


@dataclass(frozen=True)
class Special:
    name: str
    intents: tuple[LightIntent, ...]
    group: str = ''
    role: str | None = None
    zone: str | None = None
    depth: str | None = None
    colour_slot: str | None = None
    energy: float = 0.0
    speed: float = 0.0
    pulse_beats: int | None = None
    pattern: float | None = None
    envelope: Envelope = field(default_factory=Envelope)
    # Never sent to Starfire: the whole group rests, since the room tires of fixtures.
    cooldown_seconds: float = 0.0

    @property
    def intense(self) -> bool:
        return self.pulse_beats is not None or self.speed > INTENSE_SPEED

    @property
    def axes(self) -> frozenset[str]:
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
            'pattern': self.pattern,
            'envelope': self.envelope.command(),
        }


@dataclass(frozen=True)
class SpecialCapabilities:
    groups: Mapping[str, frozenset[str]] = field(default_factory=dict)
    roles: Mapping[str, str] = field(default_factory=dict)
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
    suppressed: str | None = None

    @property
    def intense(self) -> bool:
        return self.special is not None and self.special.intense


@dataclass(frozen=True)
class Venue:
    max_accent_events_per_minute: int = 2


def moments_near(
    times: Mapping[Hashable, float], at: float, window: float, *, excluding: Hashable
) -> dict[Hashable, float]:
    # Either side of `at`: cues are booked ahead of specials firing in the room's now.
    return {
        key: when for key, when in times.items() if key != excluding and abs(at - when) < window
    }


class SpecialPolicy:
    def __init__(self, specials: Sequence[Special], rng: random.Random, venue: Venue | None = None):
        self._specials = tuple(specials)
        self._rng = rng
        self._venue = venue or Venue()
        self._accents: dict[Hashable, float] = {}
        self._selections = 0
        self._last_used: dict[tuple[str, str], int] = {}
        self._group_fired: dict[str, tuple[Special, float]] = {}

    def choose(
        self,
        intent: LightIntent,
        capabilities: SpecialCapabilities,
        intense_blocked: str | None = None,
        soft_drop: str | None = None,
        *,
        now: float = 0.0,
        moment: Hashable = None,
    ) -> SpecialChoice:
        rows = [
            row
            for special in self._specials
            if intent in special.intents
            for row in capabilities.targets(special)
        ]
        recent = self._recent_accents(now, moment)
        crowded = len(recent) >= self._venue.max_accent_events_per_minute
        selectable = [
            special
            for special in rows
            if not self._cooling(special, now)
            and not (crowded and capabilities.role(special.group) == ACCENT_ROLE)
        ]
        calm = [special for special in selectable if not special.intense]
        restraint = intense_blocked or soft_drop
        if restraint:
            suppressed = restraint if len(calm) < len(selectable) else None
            chosen = self._pick(calm, capabilities) if calm else None
            return SpecialChoice(chosen, suppressed=suppressed)
        chosen = self._pick(selectable, capabilities) if selectable else None
        return SpecialChoice(chosen)

    def record(
        self,
        special: Special,
        now: float = 0.0,
        capabilities: SpecialCapabilities | None = None,
        moment: Hashable = None,
    ) -> None:
        self._selections += 1
        self._last_used[special.name, special.group] = self._selections
        self._group_fired[special.group] = (special, now)
        if capabilities is not None and capabilities.role(special.group) == ACCENT_ROLE:
            self.plan_accent(object() if moment is None else moment, now)

    def plan_accent(self, moment: Hashable, at: float) -> None:
        kept = moments_near(self._accents, at, ACCENT_WINDOW_SECONDS, excluding=moment)
        # Replaced, never mutated, so the dashboard thread reads a whole snapshot.
        self._accents = {**kept, moment: at}

    def forget_accent(self, moment: Hashable) -> None:
        self._accents = {key: at for key, at in self._accents.items() if key != moment}

    def forget_accents_after(self, now: float) -> None:
        self._accents = {key: at for key, at in self._accents.items() if at <= now}

    def accent_crowded(self, at: float, moment: Hashable = None) -> bool:
        near = moments_near(self._accents, at, ACCENT_WINDOW_SECONDS, excluding=moment)
        return len(near) >= self._venue.max_accent_events_per_minute

    def _recent_accents(self, now: float, moment: Hashable = None) -> list[float]:
        accents = dict(self._accents)
        return sorted(
            at
            for key, at in accents.items()
            if key != moment and 0.0 <= now - at < ACCENT_WINDOW_SECONDS
        )

    def cooldowns(
        self,
        now: float,
        intense_total: float,
        intense_remaining: float,
        capabilities: SpecialCapabilities | None = None,
    ) -> list[dict]:
        # Copied: the snapshot thread reads it while the engine writes it in place.
        fired = dict(self._group_fired)
        capabilities = capabilities or SpecialCapabilities()
        groups = dict.fromkeys(
            row.group
            for special in self._specials
            for row in (capabilities.targets(special) if special.role else [special])
        )
        entries = []
        for group in groups:
            last, at = fired.get(group, (None, now))
            total = last.cooldown_seconds if last else 0.0
            entries.append(_entry(group, last and last.name, total, total - (now - at)))
        intense = _entry(WHOLE_RIG, STROBE_COOLDOWN_LABEL, intense_total, intense_remaining)
        entries.append({**intense, 'strobe': True})
        if ACCENT_ROLE in capabilities.roles.values():
            entries.append(self._venue_entry(now))
        return entries

    def _venue_entry(self, now: float) -> dict:
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
        chosen = self._least_recent(pool)
        accents = {s.group for s in pool if capabilities.role(s.group) == ACCENT_ROLE}
        if chosen.group not in accents or len(accents) < 2:
            return chosen
        fired = {}
        for (_, group), at in self._last_used.items():
            fired[group] = max(at, fired.get(group, -1))
        fired_accents = [group for group in fired if capabilities.role(group) == ACCENT_ROLE]
        last = max(fired_accents, key=fired.get, default=None)

        def order(group: str):
            x = capabilities.x(group)
            reach = x if last is None else -abs(x - capabilities.x(last))
            return (reach, fired.get(group, -1), x, group)

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
