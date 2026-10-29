from __future__ import annotations

from dataclasses import dataclass, field

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
    bars: int  # 0 holds the last step until the next cue
    crossfade_beats: float | None = None  # None takes the renderer default, 0 cuts

    def command(self) -> dict:
        step = {'bank': self.bank, 'autoloop': self.autoloop, 'bars': self.bars}
        if self.crossfade_beats is not None:
            step['transition'] = {'crossfade_beats': self.crossfade_beats}
        return step


@dataclass(frozen=True)
class Overlay:
    bank: str
    autoloop: str
    priority: int
    begin_beats: float  # negative is pre-roll
    duration_beats: float
    every_bars: int = 0
    envelope: Envelope = Envelope()
    dip: bool = False

    def command(self) -> dict:
        return {
            'bank': self.bank,
            'autoloop': self.autoloop,
            'priority': self.priority,
            'begin_beats': self.begin_beats,
            'duration_beats': self.duration_beats,
            'every_bars': self.every_bars,
            'envelope': self.envelope.command(),
            'dip': self.dip,
        }


def recurring_rows(overlays: tuple[Overlay, ...]) -> tuple[Overlay, ...]:
    return tuple(row for row in overlays if row.every_bars > 0)


@dataclass(frozen=True)
class CueSection:
    start_beat_index: int
    intent: LightIntent
    rotation: tuple[Step, ...]
    palette: str
    overlays: tuple[Overlay, ...] = ()
    drop_strength: float | None = None
    seed: int = 0  # uint32 for sf.random
    # The bar the session's beat updates carry, which Starfire counts repeat periods in.
    beats_per_bar: int = field(kw_only=True)

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
        if any(step.bars < 1 for step in self.rotation[:-1]) or self.rotation[-1].bars < 0:
            raise ValueError('a step lasts at least one bar; only the last may hold with 0')
        for step in self.rotation:
            if step.crossfade_beats is not None and not 0 <= step.crossfade_beats <= MAX_SPAN_BEATS:
                raise ValueError(f'a crossfade lasts 0..{MAX_SPAN_BEATS:g} beats, 0 cutting')
        if self.palette not in VARIANT_BASES:
            raise ValueError(f'palette {self.palette!r} is not in the Login palette table')
        for overlay in self.overlays:
            self._check_overlay(overlay)
        if self.drop_strength is not None and (
            self.intent is not LightIntent.DROP or not 0.0 <= self.drop_strength <= 1.0
        ):
            raise ValueError('only a drop carries a drop strength, within 0..1')
        if not 0 <= self.seed < 1 << 32:
            raise ValueError('a cue seed is a uint32')

    def _check_overlay(self, overlay: Overlay) -> None:
        if not 0 < overlay.duration_beats <= MAX_SPAN_BEATS:
            raise ValueError('an overlay lasts a positive number of beats')
        if not all(0 <= beats <= MAX_SPAN_BEATS for beats in overlay.envelope.command().values()):
            raise ValueError('overlay attack and release are within 0..1024 beats')
        if overlay.every_bars < 0:
            raise ValueError('an overlay repeats every nonnegative number of bars')
        if overlay.every_bars == 0:
            if not -MAX_PREROLL_BEATS <= overlay.begin_beats <= MAX_SPAN_BEATS:
                raise ValueError('a one-shot overlay begins within -64..1024 beats')
        else:
            period = overlay.every_bars * self.beats_per_bar
            if not 0 <= overlay.begin_beats < period:
                raise ValueError('a repeating overlay begins inside its period')
            if overlay.duration_beats + overlay.envelope.release_beats > period:
                raise ValueError("a repeating overlay's duration and release fit its period")
        if overlay.dip and (overlay.every_bars or overlay.begin_beats >= 0):
            raise ValueError('a dip plays once before its section')

    def command(self) -> dict:
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
