"""One section's lighting plan, decided at commit time and sent to Starfire ahead of the room."""

from __future__ import annotations

from dataclasses import dataclass

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.engine.section_decoder import BEATS_PER_BAR
from autolux.lighting.palettes import VARIANT_BASES

# Starfire's row checks, mirrored so a bad plan is refused before it reaches the stage; at these
# bounds a CueSection stays under the 4 KiB cap.
MAX_ROTATION = 8
MAX_OVERLAYS = 8
MAX_PREROLL_BEATS = 64.0
MAX_OVERLAY_BEATS = 1024.0
MAX_IDENTIFIER_BYTES = 128


@dataclass(frozen=True)
class BaseLook:
    """A rotation entry; entries change on bar lines counted from the section start."""

    bank: str
    autoloop: str
    bars: int
    # Into this entry: {'kind': 'cut'} or {'kind': 'crossfade', 'duration_beats': b}; None is
    # the renderer default. The first entry's transition enters the section.
    transition: dict | None = None

    def command(self) -> dict:
        entry = {'bank': self.bank, 'autoloop': self.autoloop, 'bars': self.bars}
        if self.transition is not None:
            entry['transition'] = dict(self.transition)
        return entry


@dataclass(frozen=True)
class Overlay:
    """A scheduled Lua layer; beats count from the section start, negative begins are pre-roll."""

    bank: str
    autoloop: str
    priority: int
    begin_beats: float
    length_beats: float
    every_bars: int = 0
    attack_beats: float = 0.0
    release_beats: float = 0.0

    def command(self) -> dict:
        return {
            'bank': self.bank,
            'autoloop': self.autoloop,
            'priority': self.priority,
            'begin_beats': self.begin_beats,
            'length_beats': self.length_beats,
            'every_bars': self.every_bars,
            'attack_beats': self.attack_beats,
            'release_beats': self.release_beats,
        }


def _check_transition(transition: dict | None) -> None:
    if transition is None or transition == {'kind': 'cut'}:
        return
    beats = transition.get('duration_beats')
    if (
        set(transition) != {'kind', 'duration_beats'}
        or transition['kind'] != 'crossfade'
        or not isinstance(beats, (int, float))
        or not 0 < beats <= MAX_OVERLAY_BEATS
    ):
        raise ValueError('a transition is a cut or a crossfade of 0..1024 beats')


@dataclass(frozen=True)
class Cue:
    start_beat_index: int  # An analysis-time beat index whose bar phase is 0.
    intent: LightIntent
    rotation: tuple[BaseLook, ...]
    palette: str
    overlays: tuple[Overlay, ...] = ()
    drop_strength: float | None = None  # 0..1, drops only.
    seed: int = 0  # uint32, for sf.random.

    def __post_init__(self):
        if self.start_beat_index < 0:
            raise ValueError('a cue starts on a nonnegative beat index')
        if not 1 <= len(self.rotation) <= MAX_ROTATION:
            raise ValueError(f'a cue needs 1..{MAX_ROTATION} rotation entries')
        if len(self.overlays) > MAX_OVERLAYS:
            raise ValueError(f'a cue carries at most {MAX_OVERLAYS} overlays')
        for look in (*self.rotation, *self.overlays):
            if len(f'{look.bank}/{look.autoloop}'.encode()) > MAX_IDENTIFIER_BYTES:
                raise ValueError('autoloop ID is too long for a cue')
        if any(look.bars < 1 for look in self.rotation):
            raise ValueError('a rotation entry lasts at least one bar')
        for look in self.rotation:
            _check_transition(look.transition)
        if self.palette not in VARIANT_BASES:
            raise ValueError(f'palette {self.palette!r} is not in the Login palette table')
        for overlay in self.overlays:
            if not 0 < overlay.length_beats <= MAX_OVERLAY_BEATS:
                raise ValueError('an overlay lasts a positive number of beats')
            for beats in (overlay.attack_beats, overlay.release_beats):
                if not 0 <= beats <= MAX_OVERLAY_BEATS:
                    raise ValueError('overlay attack and release are within 0..1024 beats')
            if overlay.every_bars < 0:
                raise ValueError('an overlay repeats every nonnegative number of bars')
            # The session's bars are 4/4 (BEATS_PER_BAR), as Starfire's repeat period is.
            period = overlay.every_bars * BEATS_PER_BAR
            if overlay.every_bars == 0:
                if not -MAX_PREROLL_BEATS <= overlay.begin_beats <= MAX_OVERLAY_BEATS:
                    raise ValueError('a one-shot overlay begins within -64..1024 beats')
            elif not 0 <= overlay.begin_beats < period:
                raise ValueError('a repeating overlay begins inside its period')
            elif overlay.length_beats + overlay.release_beats > period:
                raise ValueError("a repeating overlay's length and release fit its period")
        if self.drop_strength is not None and (
            self.intent is not LightIntent.DROP or not 0.0 <= self.drop_strength <= 1.0
        ):
            raise ValueError('only a drop carries a drop strength, within 0..1')
        if not 0 <= self.seed < 1 << 32:
            raise ValueError('a cue seed is a uint32')

    def command(self) -> dict:
        """The replay and adapter shape (Starfire's `cue_section` command)."""
        value = {
            'start_beat_index': self.start_beat_index,
            'intent': self.intent.value,
            'rotation': [look.command() for look in self.rotation],
            'overlays': [overlay.command() for overlay in self.overlays],
            'palette': self.palette,
            'seed': self.seed,
        }
        if self.drop_strength is not None:
            value['drop_strength'] = self.drop_strength
        return {'kind': 'cue_section', 'value': value}
