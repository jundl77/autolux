"""Follow the music: pick calm / balanced / wild from the measured track mood.

The level is chosen when the mood window closes. At each drop it is re-read from every beat
since the track start, one step at a time, because the opening window alone depends on where
in the track the show joined. A drop that arrives below the track's peak lowers wild to
balanced once per track, so a soft drop never plays wild.
"""

from autolux.lib.engine.autoloop_policy import MAXIMAL_ARRIVAL_OF_PEAK
from autolux.lib.engine.effect_definitions import (
    BALANCED,
    CALM,
    INTENSITY_BALANCED,
    INTENSITY_CALM,
    INTENSITY_WILD,
    WILD,
)

# Drive spans about -0.4..+0.65 on the calibration set (track_mood.py).
CALM_BELOW_DRIVE = -0.3
WILD_ABOVE_DRIVE = 0.4
# Staying at calm or wild needs the drive to come back past the threshold by this much.
DRIVE_HYSTERESIS = 0.1
SOFT_DROP_ARRIVAL = MAXIMAL_ARRIVAL_OF_PEAK
LEVEL_INTENSITIES = {CALM: INTENSITY_CALM, BALANCED: INTENSITY_BALANCED, WILD: INTENSITY_WILD}
LEVELS = (CALM, BALANCED, WILD)


def level_for_drive(drive: float, current: str) -> str:
    if current == CALM and drive < CALM_BELOW_DRIVE + DRIVE_HYSTERESIS:
        return CALM
    if current == WILD and drive > WILD_ABOVE_DRIVE - DRIVE_HYSTERESIS:
        return WILD
    if drive < CALM_BELOW_DRIVE:
        return CALM
    if drive > WILD_ABOVE_DRIVE:
        return WILD
    return BALANCED


def one_step_toward(target: str, current: str) -> str:
    here, there = LEVELS.index(current), LEVELS.index(target)
    return LEVELS[here + (there > here) - (there < here)]


class FollowMusic:
    def __init__(self):
        self.track_level: str | None = None
        self.capped = False

    def on_track_mood(self, drive: float, current: str) -> str:
        self.track_level = level_for_drive(drive, current)
        self.capped = False
        return self.track_level

    def on_drop(
        self, arrival_of_peak: float | None, current: str, drive: float | None = None
    ) -> str | None:
        """The level this drop should play at, or None to keep the current one.

        drive is the track's drive over every beat so far, None while the opening window is open.
        """
        if self.track_level is None or self.capped:
            return None
        if drive is not None:
            self.track_level = one_step_toward(level_for_drive(drive, current), current)
        soft = arrival_of_peak is not None and arrival_of_peak < SOFT_DROP_ARRIVAL
        level = BALANCED if self.track_level == WILD and soft else self.track_level
        if level == current:
            return None
        self.capped = level != self.track_level
        return level
