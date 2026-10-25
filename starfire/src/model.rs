//! Fixture-independent values shared by scripts, playback and output encoding.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

pub const DMX_CHANNELS: usize = 512;
pub const DEFAULT_FRAME_RATE_HZ: u32 = 40;
pub const MAX_FRAME_RATE_HZ: u32 = 100;
// Four output samples per cycle at the default render cadence.
pub const MAX_STROBE_HZ: f64 = DEFAULT_FRAME_RATE_HZ as f64 / 4.0;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rgb {
    pub red: f64,
    pub green: f64,
    pub blue: f64,
}

impl Default for Rgb {
    fn default() -> Self {
        Self {
            red: 1.0,
            green: 1.0,
            blue: 1.0,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Target {
    All,
    Group(String),
    Fixture(String),
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PropertyValues {
    pub intensity: Option<f64>,
    pub colour: Option<Rgb>,
    pub pan: Option<f64>,
    pub tilt: Option<f64>,
    pub strobe_hz: Option<f64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Effect {
    pub target: Target,
    pub values: PropertyValues,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FixtureState {
    pub intensity: f64,
    pub colour: Rgb,
    pub pan: f64,
    pub tilt: f64,
    pub strobe_hz: f64,
}

impl Default for FixtureState {
    fn default() -> Self {
        Self {
            intensity: 0.0,
            colour: Rgb::default(),
            pan: 0.5,
            tilt: 0.5,
            strobe_hz: 0.0,
        }
    }
}

pub type FixtureStates = BTreeMap<String, FixtureState>;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct RenderContext {
    pub time_seconds: f64,
    pub beat_position: f64,
    pub sequence_elapsed_beats: f64,
    pub sequence_speed_scaled_elapsed_beats: f64,
    /// Measured RMS mapped to 0..1, independent of operator brightness.
    pub normalized_rms_level: f64,
}

impl Default for RenderContext {
    fn default() -> Self {
        Self {
            time_seconds: 0.0,
            beat_position: 0.0,
            sequence_elapsed_beats: 0.0,
            sequence_speed_scaled_elapsed_beats: 0.0,
            normalized_rms_level: 1.0,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Transition {
    #[default]
    Cut,
    Crossfade {
        duration_beats: f64,
    },
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum OverrideLifetime {
    #[default]
    UntilCleared,
    Beats {
        duration_beats: f64,
    },
    Seconds {
        duration_seconds: f64,
    },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LightingOverride {
    pub id: String,
    pub target: Target,
    #[serde(default)]
    pub values: PropertyValues,
    pub intensity_multiplier: Option<f64>,
    #[serde(default)]
    pub lifetime: OverrideLifetime,
    #[serde(default)]
    pub priority: i32,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Beat {
        position_beats: f64,
        bpm: f64,
    },
    Select {
        bank: String,
        sequence: String,
        #[serde(default)]
        transition: Transition,
    },
    SetOverride {
        value: LightingOverride,
    },
    ClearOverride {
        id: String,
    },
    SetMaster {
        intensity: f64,
    },
    SetNormalizedRmsLevel {
        level: f64,
    },
    SetSpeed {
        multiplier: f64,
    },
    SetBlackout {
        enabled: bool,
    },
    Stop,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TimedCommand {
    pub at_seconds: f64,
    pub command: Command,
}
