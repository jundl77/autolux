from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.engine.effect_controller import DROP_ACCENT_PRIORITY
from autolux.lib.engine.special_policy import Special, SpecialCapabilities

LOGGER = logging.getLogger(__name__)

STATIC_PRIORITY = DROP_ACCENT_PRIORITY + 1000
STATIC_FLOOR_PRIORITY = STATIC_PRIORITY - 1
STATIC_FLOOR_ID = 'static_floor'
STATIC_GROUP_PRIORITY = STATIC_PRIORITY + 1
STATIC_LOOK_PREFIX = 'static_'
STATIC_SPECIAL_ID = 'static_special'
STATIC_SPECIAL_RELEASE_BEATS = 4.0
HIGHLIGHT_ID = 'highlight'
# Dead-man expiry: a Locate left on by a dashboard that went away goes out by itself.
HIGHLIGHT_SECONDS = 60.0
ALL_FIXTURES = 'all'
UV_ATTRIBUTE = 'ColorAdd_UV'
UV_FULL = 255.0
BLACK = (0.0, 0.0, 0.0)
PRESET_COLOURS = {
    'white': (1.0, 1.0, 1.0),
    'warm': (1.0, 0.55, 0.2),
    'red': (1.0, 0.0, 0.0),
    'blue': (0.0, 0.0, 1.0),
}
PRESETS = (*PRESET_COLOURS, 'uv', 'blackout', 'off')
HIGHLIGHT_COLOURS = {**PRESET_COLOURS, 'green': (0.0, 1.0, 0.0)}


@dataclass(frozen=True)
class StaticRig:
    groups: tuple[str, ...] = ()
    uv_groups: tuple[str, ...] = ()
    # (fixture name, its Starfire cell ids); a single-cell fixture's cell is the fixture.
    fixtures: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Fixture names and cell ids Starfire accepts a UV attribute on.
    uv_targets: tuple[str, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return (ALL_FIXTURES, *self.groups)


def colour_hex(colour: tuple[float, float, float]) -> str:
    return '#' + ''.join(f'{round(channel * 255):02x}' for channel in colour)


class StaticLooks:
    def __init__(
        self,
        lighting_client: StarfireClient,
        rig: StaticRig,
        specials: Sequence[Special] = (),
        capabilities: Callable[[], SpecialCapabilities] = SpecialCapabilities,
    ):
        self._lighting = lighting_client
        self.rig = rig
        self._capabilities = capabilities
        # Strobe rows stay the show's own drop accent; the operator never holds one.
        self._rows = {row.name: row for row in specials if row.strobe_hz is None}
        self._looks: dict[str, dict] = {}
        self._special: Special | None = None
        self._floor = False
        self.blackout = False
        self.highlighted: dict | None = None

    @property
    def active(self) -> bool:
        return bool(self._looks) or self._floor or self.blackout or self._special is not None

    def special_rows(
        self, target: str, capabilities: SpecialCapabilities | None = None
    ) -> list[str]:
        """Catalogue rows the operator may hold on target: the rows the automatic show could
        place on that group (its own group, or its role where the fixtures render the row's
        axes), and on the whole rig any row it could place somewhere."""
        capabilities = capabilities or self._capabilities()
        return [
            name
            for name, row in self._rows.items()
            if any(target in (ALL_FIXTURES, placed.group) for placed in capabilities.targets(row))
        ]

    def special(self, row: str, target: str) -> None:
        """Hold a catalogue row on target until Clear FX or Clear all; cooldowns and the FX auto
        toggle belong to the automatic show and never touch it."""
        if target not in self.rig.targets:
            raise ValueError(f'unknown static target {target!r}')
        if row not in self._rows:
            raise ValueError(f'unknown FX {row!r}')
        if row not in self.special_rows(target):
            raise ValueError(f'{target!r} cannot render {row!r}')
        # Cut in at once and release short: the operator's hand, not the row's section pacing.
        self._special = dataclasses.replace(
            self._rows[row],
            group=target,
            attack_beats=0.0,
            release_beats=STATIC_SPECIAL_RELEASE_BEATS,
            cooldown_seconds=0.0,
        )
        self._send_special()

    def clear_special(self) -> None:
        if self._special is not None:
            self._special = None
            self._lighting.clear_override(STATIC_SPECIAL_ID)

    def _send_special(self) -> None:
        special = self._special
        self._lighting.set_special(
            STATIC_SPECIAL_ID, special.group, special.wire(), priority=STATIC_PRIORITY
        )

    def look(self, target: str, colour=None, dimmer=None, uv=None) -> None:
        if target not in self.rig.targets:
            raise ValueError(f'unknown static target {target!r}')
        if colour is None and dimmer is None and uv is None:
            raise ValueError('a static look needs a colour, dimmer or UV level')
        if dimmer is not None and not 0.0 <= dimmer <= 1.0:
            raise ValueError('a static dimmer must be within 0..1')
        if uv is not None and target not in self.rig.uv_groups:
            raise ValueError(f'{target!r} has no UV channel')
        if colour is not None and target == ALL_FIXTURES:
            for name, other in self._looks.items():
                if name != ALL_FIXTURES and ('colour' in other or 'uv' in other):
                    other.pop('colour', None)
                    other.pop('uv', None)
                    self._send(name)
        look = self._looks.setdefault(target, {})
        if colour is not None and uv is None:
            look.pop('uv', None)
        if colour is not None and dimmer is None:
            dimmer = look.get('dimmer', 1.0)
        for key, value in (('colour', colour), ('dimmer', dimmer), ('uv', uv)):
            if value is not None:
                look[key] = value
        self._send(target)

    def preset(self, name: str) -> None:
        if name not in PRESETS:
            raise ValueError(f'unknown static preset {name!r}')
        if name == 'blackout':
            self.set_blackout(True)
            return
        self.clear()
        if name in PRESET_COLOURS:
            self.look(ALL_FIXTURES, colour=PRESET_COLOURS[name], dimmer=1.0)
        elif name == 'uv':
            if not self.rig.uv_groups:
                raise ValueError('the rig has no UV fixtures')
            self.look(ALL_FIXTURES, dimmer=0.0)
            for group in self.rig.uv_groups:
                self.look(group, colour=BLACK, dimmer=1.0, uv=True)
        elif name == 'off':
            # Clearing alone would reveal the held automatic look (warm-white standby when idle).
            self._set_floor()

    def highlight(self, target: dict, colour: str = 'white', *, paused: bool = False) -> None:
        """Locate {'fixture': name} or {'cell': id}: open white (or a swatch), beam home, no
        strobe. One highlight at a time, so testing another fixture moves it."""
        if not any(
            target in ({'fixture': name}, *({'cell': cell} for cell in cells))
            for name, cells in self.rig.fixtures
        ):
            raise ValueError(f'unknown fixture or cell {target!r}')
        if colour not in HIGHLIGHT_COLOURS:
            raise ValueError(f'unknown highlight colour {colour!r}')
        # Paused or blacked out, the rig is dark and the Locate would light nothing.
        if paused:
            raise ValueError('the show is paused')
        if self.blackout:
            raise ValueError('blackout is on')
        red, green, blue = HIGHLIGHT_COLOURS[colour]
        values = {
            'intensity': 1.0,
            'colour': {'red': red, 'green': green, 'blue': blue},
            'pan': 0.5,
            'tilt': 0.5,
            'strobe_hz': 0.0,
        }
        (name,) = target.values()
        if name in self.rig.uv_targets:
            values['attributes'] = {UV_ATTRIBUTE: 0.0}
        self._lighting.set_override(
            {
                'id': HIGHLIGHT_ID,
                # Starfire resolves a cell id through its fixture target too.
                'target': {'fixture': name},
                'values': values,
                'priority': STATIC_GROUP_PRIORITY + 1,
                'lifetime': {'kind': 'seconds', 'duration_seconds': HIGHLIGHT_SECONDS},
            }
        )
        self.highlighted = {'target': dict(target), 'colour': colour}

    def clear_highlight(self) -> None:
        if self.highlighted is not None:
            self.highlighted = None
            self._lighting.clear_override(HIGHLIGHT_ID)

    def set_blackout(self, enabled: bool) -> None:
        if enabled == self.blackout:
            return
        self.blackout = enabled
        if enabled:
            self.clear_highlight()
        self._lighting.set_blackout(enabled)

    def resend(self) -> None:
        if self._floor:
            self._send_floor()
        for target in self._looks:
            self._send(target)
        if self._special is not None:
            self._send_special()

    def clear(self) -> None:
        for target in self._looks:
            self._lighting.clear_override(STATIC_LOOK_PREFIX + target)
        self._looks.clear()
        self.clear_special()
        if self._floor:
            self._floor = False
            self._lighting.clear_override(STATIC_FLOOR_ID)
        self.set_blackout(False)

    def state(self) -> dict:
        capabilities = self._capabilities()
        return {
            'targets': list(self.rig.targets),
            'blacklight': bool(self.rig.uv_groups),
            'blackout': self.blackout,
            'special_fx': None
            if self._special is None
            else {'name': self._special.name, 'group': self._special.group},
            'fx_rows': {
                target: self.special_rows(target, capabilities) for target in self.rig.targets
            },
            'looks': {
                target: {
                    'colour': None if 'colour' not in look else colour_hex(look['colour']),
                    'dimmer': look.get('dimmer'),
                    'uv': look.get('uv', False),
                }
                for target, look in self._looks.items()
            },
        }

    def _set_floor(self) -> None:
        if not self._floor:
            self._floor = True
            self._send_floor()

    def _send_floor(self) -> None:
        self._lighting.set_override(
            {
                'id': STATIC_FLOOR_ID,
                'target': ALL_FIXTURES,
                'values': {'intensity': 0.0},
                'priority': STATIC_FLOOR_PRIORITY,
            }
        )

    def _send(self, target: str) -> None:
        look = self._looks[target]
        values = {}
        if 'colour' in look:
            red, green, blue = look['colour']
            values['colour'] = {'red': red, 'green': green, 'blue': blue}
        if 'dimmer' in look:
            values['intensity'] = look['dimmer']
        if 'uv' in look:
            values['attributes'] = {UV_ATTRIBUTE: UV_FULL if look['uv'] else 0.0}
        self._lighting.set_override(
            {
                'id': STATIC_LOOK_PREFIX + target,
                'target': ALL_FIXTURES if target == ALL_FIXTURES else {'group': target},
                'values': values,
                'priority': STATIC_PRIORITY if target == ALL_FIXTURES else STATIC_GROUP_PRIORITY,
            }
        )
