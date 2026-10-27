use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

pub const DMX_CHANNELS: usize = 512;
pub const DEFAULT_FRAME_RATE_HZ: u32 = 40;
pub const MAX_FRAME_RATE_HZ: u32 = 100;
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

impl Rgb {
    pub fn lerp(self, to: Self, amount: f64) -> Self {
        Self {
            red: lerp(self.red, to.red, amount),
            green: lerp(self.green, to.green, amount),
            blue: lerp(self.blue, to.blue, amount),
        }
    }
}

pub fn lerp(from: f64, to: f64, amount: f64) -> f64 {
    from + (to - from) * amount
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
    pub attributes: BTreeMap<String, AttributeValue>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum AttributeValue {
    Number(f64),
    Preset(String),
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
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub attributes: BTreeMap<String, AttributeValue>,
    #[serde(skip)]
    pub output_blackout: bool,
    #[serde(skip, default = "unit_brightness")]
    pub attribute_brightness: f64,
}

impl Default for FixtureState {
    fn default() -> Self {
        Self {
            intensity: 0.0,
            colour: Rgb::default(),
            pan: 0.5,
            tilt: 0.5,
            strobe_hz: 0.0,
            attributes: BTreeMap::new(),
            output_blackout: false,
            attribute_brightness: 1.0,
        }
    }
}

fn unit_brightness() -> f64 {
    1.0
}

pub type FixtureStates = BTreeMap<String, FixtureState>;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct RenderContext {
    pub time_seconds: f64,
    pub beat_position: f64,
    pub bar_phase: f64,
    pub bar_index: u64,
    pub beats_per_bar: u32,
    pub autoloop_elapsed_beats: f64,
    pub autoloop_speed_scaled_elapsed_beats: f64,
    pub normalized_rms_level: Option<f64>,
    pub intensity: f64,
}

impl Default for RenderContext {
    fn default() -> Self {
        Self {
            time_seconds: 0.0,
            beat_position: 0.0,
            bar_phase: 0.0,
            bar_index: 0,
            beats_per_bar: 4,
            autoloop_elapsed_beats: 0.0,
            autoloop_speed_scaled_elapsed_beats: 0.0,
            normalized_rms_level: None,
            intensity: 1.0,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Transition {
    #[default]
    RendererDefault,
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

/// Fixture-agnostic accent vocabulary; the renderer maps each axis onto GDTF attribute families.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SpecialFx {
    pub colour_slot: String,
    pub energy: f64,
    pub speed: f64,
    pub pulse_beats: Option<u32>,
    pub attack_beats: f64,
    pub release_beats: f64,
    pub pattern: Option<f64>,
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
    #[serde(default)]
    pub special_fx: Option<Box<SpecialFx>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Beat {
        beat_index: u64,
        #[serde(default)]
        bar_phase: Option<u32>,
        beats_per_bar: u32,
        bpm: f64,
    },
    Select {
        bank: String,
        autoloop: String,
        #[serde(default)]
        transition: Transition,
        palette: String,
    },
    SetOverride {
        value: LightingOverride,
    },
    ClearOverride {
        id: String,
        #[serde(default)]
        release_beats: Option<f64>,
        #[serde(default)]
        release_seconds: Option<f64>,
    },
    SetMaster {
        intensity: f64,
        #[serde(default)]
        fade_seconds: f64,
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
    SetPaused {
        paused: bool,
    },
    SetDefaultCrossfadeBeats {
        beats: f64,
    },
    SetStrobeMaxRateHz {
        hz: f64,
    },
    SetStrobeMaxBurstSeconds {
        seconds: f64,
    },
    SetPalette {
        name: String,
    },
    SetIntensity {
        level: f64,
    },
    Stop,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TimedCommand {
    pub at_seconds: f64,
    pub command: Command,
}
