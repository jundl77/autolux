//! How the dashboard's intensity level shapes rendering. Autolux decides which loop plays;
//! these curves decide how hard it plays.
//!
//! Pulse depth and movement speed follow a three-point piecewise-linear curve keyed on the
//! dashboard levels (autolux `INTENSITY_CALM` / `INTENSITY_BALANCED` / `INTENSITY_WILD`), so
//! each level gets the value chosen for it and a continuous level in between interpolates.
//! Below calm the calm value holds: calm is already the floor of the show.

use crate::model::lerp;

pub const INTENSITY_CALM: f64 = 0.3;
pub const INTENSITY_BALANCED: f64 = 0.7;
pub const INTENSITY_WILD: f64 = 1.0;

/// Peak of the shared beat envelope (`sf.on_beat`), as a fraction of full depth.
pub const PULSE_DEPTH_CALM: f64 = 0.25;
pub const PULSE_DEPTH_BALANCED: f64 = 0.8;
pub const PULSE_DEPTH_WILD: f64 = 1.0;

/// Pan/tilt slew limit, as a fraction of `FULL_MOVEMENT_RANGE_PER_SECOND`; wild is unlimited.
pub const MOVEMENT_SPEED_CALM: f64 = 0.3;
pub const MOVEMENT_SPEED_BALANCED: f64 = 0.7;
pub const MOVEMENT_SPEED_WILD: f64 = 1.0;
/// Full range in half a second: the fastest a limited sweep moves before wild lifts the limit.
pub const FULL_MOVEMENT_RANGE_PER_SECOND: f64 = 2.0;

/// Strobes are off at or below calm.
pub const STROBE_GATE_INTENSITY: f64 = INTENSITY_CALM;

pub fn pulse_depth(intensity: f64) -> f64 {
    by_level(
        intensity,
        PULSE_DEPTH_CALM,
        PULSE_DEPTH_BALANCED,
        PULSE_DEPTH_WILD,
    )
}

pub fn movement_speed(intensity: f64) -> f64 {
    by_level(
        intensity,
        MOVEMENT_SPEED_CALM,
        MOVEMENT_SPEED_BALANCED,
        MOVEMENT_SPEED_WILD,
    )
}

/// `None` means unlimited: wild moves exactly as the loop authored it.
pub fn movement_range_per_second(intensity: f64) -> Option<f64> {
    (intensity < INTENSITY_WILD).then(|| movement_speed(intensity) * FULL_MOVEMENT_RANGE_PER_SECOND)
}

pub fn strobe_scale(intensity: f64) -> f64 {
    if intensity <= STROBE_GATE_INTENSITY {
        0.0
    } else {
        intensity
    }
}

fn by_level(intensity: f64, calm: f64, balanced: f64, wild: f64) -> f64 {
    if intensity <= INTENSITY_CALM {
        calm
    } else if intensity <= INTENSITY_BALANCED {
        lerp(
            calm,
            balanced,
            (intensity - INTENSITY_CALM) / (INTENSITY_BALANCED - INTENSITY_CALM),
        )
    } else if intensity < INTENSITY_WILD {
        lerp(
            balanced,
            wild,
            (intensity - INTENSITY_BALANCED) / (INTENSITY_WILD - INTENSITY_BALANCED),
        )
    } else {
        wild
    }
}
