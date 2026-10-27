"""Follow the music: pick calm / balanced / wild from what the room has heard of the track.

Drive alone misreads tracks: a loud pop ballad has the level of a peak-time drop. So each level
needs evidence from the track's sections since its start:

- wild: the drive is above WILD_ABOVE_DRIVE and at least one DROP arrived loud, at
  LOUD_DROP_ARRIVAL of the track peak or more (the autoloop policy's maximal-arrival grade);
- calm: the first CALM_OPENING_BARS are heard without a DROP, and the committed intents average
  CALM_MAX_MEAN_ENERGY or less on INTENT_ENERGY;
- balanced otherwise.

The level is first chosen when the mood window closes, and that choice is never wild: no drop
has been graded yet. Calm can follow once the opening is heard. At each drop the level is
re-read from every beat since the track start, one step at a time, because the opening window
alone depends on where in the track the show joined. A drop that arrives below the track's
peak lowers wild to balanced once per track, so a soft drop never plays wild.

INTENT_ENERGY is the section's own energy, not the autoloop tier it played at: the tier
depends on the level, and evidence that follows the level would hold any level it chose.
"""

from statistics import fmean

from autolux.lib.engine.autoloop_policy import MAXIMAL_ARRIVAL_OF_PEAK
from autolux.lib.engine.effect_definitions import (
    BALANCED,
    CALM,
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    WILD,
    LightIntent,
)
from autolux.lib.engine.section_decoder import BEATS_PER_BAR

# Drive spans about -0.6..+0.8 on the calibration set (track_mood.py).
WILD_ABOVE_DRIVE = 0.4
# Staying wild needs the drive to come back below the threshold by this much.
DRIVE_HYSTERESIS = 0.1
LOUD_DROP_ARRIVAL = MAXIMAL_ARRIVAL_OF_PEAK
CALM_OPENING_BARS = 32
CALM_OPENING_BEATS = CALM_OPENING_BARS * BEATS_PER_BAR
CALM_MAX_MEAN_ENERGY = 1.5
INTENT_ENERGY = {
    LightIntent.ATMOSPHERIC: 1,
    LightIntent.BREAKDOWN: 1,
    LightIntent.BUILDUP: 2,
    LightIntent.DROP: 3,
}
LEVEL_INTENSITIES = {CALM: INTENSITY_CALM, BALANCED: INTENSITY_BALANCED, WILD: INTENSITY_WILD}
LEVELS = (CALM, BALANCED, WILD)


def drive_is_wild(drive: float, current: str) -> bool:
    threshold = WILD_ABOVE_DRIVE - DRIVE_HYSTERESIS if current == WILD else WILD_ABOVE_DRIVE
    return drive > threshold


def one_step_toward(target: str, current: str) -> str:
    here, there = LEVELS.index(current), LEVELS.index(target)
    return LEVELS[here + (there > here) - (there < here)]


class FollowMusic:
    def __init__(self):
        self.start_track()

    def start_track(self, intent: LightIntent | None = None) -> None:
        """Forget the previous track; intent is the section the new track starts with."""
        self.track_level: str | None = None
        self.capped = False
        self.energies: list[int] = []
        self.opening_heard = False
        self.drop_in_opening = False
        self.loud_drop = False
        if intent is not None:
            self.on_intent(intent)

    def on_intent(self, intent: LightIntent) -> None:
        self.energies.append(INTENT_ENERGY[intent])
        if intent == LightIntent.DROP and not self.opening_heard:
            self.drop_in_opening = True

    @property
    def mean_energy(self) -> float | None:
        return fmean(self.energies) if self.energies else None

    def heard_calm(self) -> bool:
        return (
            self.opening_heard
            and not self.drop_in_opening
            and self.mean_energy is not None
            and self.mean_energy <= CALM_MAX_MEAN_ENERGY
        )

    def level_for(self, drive: float, current: str) -> str:
        if self.heard_calm():
            return CALM
        if self.loud_drop and drive_is_wild(drive, current):
            return WILD
        return BALANCED

    def on_track_mood(self, drive: float, current: str) -> str:
        level = self.level_for(drive, current)
        self.track_level = BALANCED if level == WILD else level
        return self.track_level

    def on_opening_heard(self) -> str | None:
        """CALM once CALM_OPENING_BEATS of the track are heard and they read calm, else None."""
        self.opening_heard = True
        if self.track_level is None or not self.heard_calm():
            return None
        self.track_level = CALM
        return CALM

    def on_drop(
        self, arrival_of_peak: float | None, current: str, drive: float | None = None
    ) -> str | None:
        """The level this drop should play at, or None to keep the current one.

        drive is the track's drive over every beat so far, None while the opening window is open.
        """
        soft = arrival_of_peak is not None and arrival_of_peak < LOUD_DROP_ARRIVAL
        if arrival_of_peak is not None and not soft:
            self.loud_drop = True
        if self.track_level is None or self.capped:
            return None
        if drive is not None:
            self.track_level = one_step_toward(self.level_for(drive, current), current)
        level = BALANCED if self.track_level == WILD and soft else self.track_level
        if level == current:
            return None
        self.capped = level != self.track_level
        return level
