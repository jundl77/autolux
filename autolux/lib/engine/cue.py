from __future__ import annotations

from dataclasses import dataclass

from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lighting.palettes import VARIANT_BASES

# Mirrors Starfire's row checks; at these bounds a CueSection stays under the 4 KiB cap.
MAX_ROTATION = 8
MAX_OVERLAYS = 8
MAX_PREROLL_BEATS = 64.0
MAX_SPAN_BEATS = 1024.0
MAX_IDENTIFIER_BYTES = 128


@dataclass(frozen=True)
class Envelope:
    attack_beats: float = 0.0
    release_beats: float = 0.0

    def command(self) -> dict:
        return {'attack_beats': self.attack_beats, 'release_beats': self.release_beats}


@dataclass(frozen=True)
class Step:
    bank: str
    autoloop: str
    bars: int
    transition: dict | None = None

    def command(self) -> dict:
        step = {'bank': self.bank, 'autoloop': self.autoloop, 'bars': self.bars}
        if self.transition is not None:
            step['transition'] = dict(self.transition)
        return step


@dataclass(frozen=True)
class Overlay:
    bank: str
    autoloop: str
    priority: int
    begin_beats: float  # negative is pre-roll
    length_beats: float
    every_bars: int = 0
    envelope: Envelope = Envelope()
    blackout: bool = False

    def command(self) -> dict:
        return {
            'bank': self.bank,
            'autoloop': self.autoloop,
            'priority': self.priority,
            'begin_beats': self.begin_beats,
            'length_beats': self.length_beats,
            'every_bars': self.every_bars,
            'envelope': self.envelope.command(),
            'blackout': self.blackout,
        }


def recurring_rows(overlays: tuple[Overlay, ...]) -> tuple[Overlay, ...]:
    return tuple(row for row in overlays if row.every_bars > 0)


def _check_transition(transition: dict | None) -> None:
    if transition is None or transition == {'kind': 'cut'}:
        return
    beats = transition.get('duration_beats')
    if (
        set(transition) != {'kind', 'duration_beats'}
        or transition['kind'] != 'crossfade'
        or not isinstance(beats, (int, float))
        or not 0 < beats <= MAX_SPAN_BEATS
    ):
        raise ValueError(f'a transition is a cut or a crossfade of 0..{MAX_SPAN_BEATS} beats')


@dataclass(frozen=True)
class Cue:
    start_beat_index: int
    intent: LightIntent
    rotation: tuple[Step, ...]
    palette: str
    overlays: tuple[Overlay, ...] = ()
    drop_strength: float | None = None
    seed: int = 0  # uint32 for sf.random

    def __post_init__(self):
        if self.start_beat_index < 0:
            raise ValueError('a cue starts on a nonnegative beat index')
        if not 1 <= len(self.rotation) <= MAX_ROTATION:
            raise ValueError(f'a cue needs 1..{MAX_ROTATION} steps')
        if len(self.overlays) > MAX_OVERLAYS:
            raise ValueError(f'a cue carries at most {MAX_OVERLAYS} overlays')
        for row in (*self.rotation, *self.overlays):
            if len(f'{row.bank}/{row.autoloop}'.encode()) > MAX_IDENTIFIER_BYTES:
                raise ValueError('autoloop ID is too long for a cue')
        if any(step.bars < 1 for step in self.rotation):
            raise ValueError('a step lasts at least one bar')
        for step in self.rotation:
            _check_transition(step.transition)
        if self.palette not in VARIANT_BASES:
            raise ValueError(f'palette {self.palette!r} is not in the Login palette table')
        for overlay in self.overlays:
            if not 0 < overlay.length_beats <= MAX_SPAN_BEATS:
                raise ValueError('an overlay lasts a positive number of beats')
            envelope = overlay.envelope
            if not all(0 <= beats <= MAX_SPAN_BEATS for beats in envelope.command().values()):
                raise ValueError('overlay attack and release are within 0..1024 beats')
            if overlay.every_bars < 0:
                raise ValueError('an overlay repeats every nonnegative number of bars')
            if overlay.every_bars == 0 and not (
                -MAX_PREROLL_BEATS <= overlay.begin_beats <= MAX_SPAN_BEATS
            ):
                raise ValueError('a one-shot overlay begins within -64..1024 beats')
            if overlay.blackout and (overlay.every_bars or overlay.begin_beats >= 0):
                raise ValueError('a blackout plays once before its section')
        if self.drop_strength is not None and (
            self.intent is not LightIntent.DROP or not 0.0 <= self.drop_strength <= 1.0
        ):
            raise ValueError('only a drop carries a drop strength, within 0..1')
        if not 0 <= self.seed < 1 << 32:
            raise ValueError('a cue seed is a uint32')

    def command(self, beats_per_bar: int) -> dict:
        """beats_per_bar is the session's UpdateBeat bar, which Starfire counts repeats in."""
        for overlay in recurring_rows(self.overlays):
            period = overlay.every_bars * beats_per_bar
            if not 0 <= overlay.begin_beats < period:
                raise ValueError('a repeating overlay begins inside its period')
            if overlay.length_beats + overlay.envelope.release_beats > period:
                raise ValueError("a repeating overlay's length and release fit its period")
        value = {
            'start_beat_index': self.start_beat_index,
            'intent': self.intent.value,
            'rotation': [step.command() for step in self.rotation],
            'overlays': [overlay.command() for overlay in self.overlays],
            'palette': self.palette,
            'seed': self.seed,
        }
        if self.drop_strength is not None:
            value['drop_strength'] = self.drop_strength
        return {'kind': 'cue_section', 'value': value}
