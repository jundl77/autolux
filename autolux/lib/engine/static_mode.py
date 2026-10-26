from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import Clock
from autolux.lib.engine.effect_controller import DROP_ACCENT_PRIORITY, strobe_override
from autolux.lib.engine.effect_definitions import ShowSettings
from autolux.lib.engine.special_policy import Special, SpecialCapabilities
from autolux.lighting.special_catalogue import special_values

LOGGER = logging.getLogger(__name__)

STATIC_PRIORITY = DROP_ACCENT_PRIORITY + 1000
STATIC_FLOOR_PRIORITY = STATIC_PRIORITY - 1
STATIC_FLOOR_ID = 'static_floor'
STATIC_GROUP_PRIORITY = STATIC_PRIORITY + 1
STATIC_LOOK_PREFIX = 'static_'
STATIC_STROBE_ID = 'static_strobe'
STATIC_SPECIAL_ID = 'static_special'
STATIC_SPECIAL_RELEASE_BEATS = 4.0
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


@dataclass(frozen=True)
class StaticRig:
    groups: tuple[str, ...] = ()
    uv_groups: tuple[str, ...] = ()

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
        clock: Clock,
        settings: Callable[[], ShowSettings],
    ):
        self._lighting = lighting_client
        self.rig = rig
        self._clock = clock
        self._settings = settings
        self._looks: dict[str, dict] = {}
        self._strobe_until: float | None = None
        self._special: Special | None = None
        self._floor = False
        self.blackout = False

    def special(
        self,
        group: str,
        colour_slot: str,
        energy: float,
        speed: float,
        pulse_beats: int | None = None,
    ) -> None:
        values = {
            'colour_slot': colour_slot,
            'energy': energy,
            'speed': speed,
            'pulse_beats': pulse_beats,
            'attack_beats': 0.0,
            'release_beats': STATIC_SPECIAL_RELEASE_BEATS,
        }
        special = Special('static', (), group, **special_values(values))
        if not self.special_capabilities().allows(special):
            raise ValueError(f'{group!r} cannot render this special')
        self._special = special
        self._send_special()

    def clear_special(self) -> None:
        if self._special is not None:
            self._special = None
            self._lighting.clear_override(STATIC_SPECIAL_ID)

    def special_capabilities(self) -> SpecialCapabilities:
        return SpecialCapabilities.from_groups(self._lighting.groups())

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
        if target != ALL_FIXTURES and ALL_FIXTURES not in self._looks:
            # Without it the held automatic look keeps lighting every other group.
            self._set_floor()
        look = self._looks.setdefault(target, {})
        if colour is not None and uv is None:
            look.pop('uv', None)
        if colour is not None and dimmer is None:
            dimmer = look.get('dimmer', 1.0)
        for key, value in (('colour', colour), ('dimmer', dimmer), ('uv', uv)):
            if value is not None:
                look[key] = value
        self._send(target)

    def blacklight(self) -> None:
        if not self.rig.uv_groups:
            raise ValueError('the rig has no UV fixtures')
        for group in self.rig.uv_groups:
            self.look(group, colour=BLACK, dimmer=1.0, uv=True)

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
            self.look(ALL_FIXTURES, dimmer=0.0)
            self.blacklight()
        elif name == 'off':
            # Clearing alone would reveal the held automatic look (warm-white standby when idle).
            self._set_floor()

    def set_blackout(self, enabled: bool) -> None:
        if enabled == self.blackout:
            return
        self.blackout = enabled
        self._lighting.set_blackout(enabled)

    def strobe(self, enabled: bool | None = None) -> None:
        now = self._clock.monotonic()
        running = self._strobe_until is not None and now < self._strobe_until
        if enabled is None:
            enabled = not running
        if not enabled:
            if running:
                self._strobe_until = None
                self._lighting.clear_override(STATIC_STROBE_ID)
            return
        if running:
            return
        settings = self._settings()
        # Starfire caps the burst at this scaled length; matching it keeps the toggle in step.
        duration = settings.strobe_max_burst_seconds * settings.intensity
        self._strobe_until = now + duration
        self._lighting.set_override(
            strobe_override(STATIC_STROBE_ID, STATIC_PRIORITY, duration, settings)
        )

    def resend(self) -> None:
        self._strobe_until = None
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
        if self._strobe_until is not None:
            self._strobe_until = None
            self._lighting.clear_override(STATIC_STROBE_ID)
        self.set_blackout(False)

    def state(self) -> dict:
        return {
            'targets': list(self.rig.targets),
            'blacklight': bool(self.rig.uv_groups),
            'blackout': self.blackout,
            'special_fx': None
            if self._special is None
            else {
                'group': self._special.group,
                'colour_slot': self._special.colour_slot,
                'energy': self._special.energy,
                'speed': self._special.speed,
                'pulse_beats': self._special.pulse_beats,
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
