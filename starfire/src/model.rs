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

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SectionIntent {
    Atmospheric,
    Breakdown,
    Buildup,
    Drop,
}

/// Measured by Autolux over one beat; each 0..1.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BeatFeatures {
    pub onset: f64,
    pub bass: f64,
    pub mid: f64,
    pub high: f64,
}

/// Lua sees `None` as nil. Section fields describe the active cue and `next_*` the earliest
/// pending one; a pending cue never shows through `section_*`.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct RenderContext {
    pub time_seconds: f64,
    pub beat_position: f64,
    pub bar_phase: f64,
    pub bar_index: u64,
    pub beats_per_bar: u32,
    /// Beats since this layer began: the rotation entry for a base loop, the nominal begin for
    /// an overlay.
    pub autoloop_elapsed_beats: f64,
    pub autoloop_speed_scaled_elapsed_beats: f64,
    pub normalized_rms_level: Option<f64>,
    pub intensity: f64,
    /// The newest beat's tempo; 0 before the first beat.
    pub bpm: f64,
    pub section_intent: Option<SectionIntent>,
    pub section_elapsed_beats: Option<f64>,
    pub next_intent: Option<SectionIntent>,
    pub beats_until_next: Option<f64>,
    /// Beats until the next cue's dip pre-roll begins, the moment a finale aims at.
    pub beats_until_dip: Option<f64>,
    pub drop_strength: Option<f64>,
    pub next_drop_strength: Option<f64>,
    /// The active cue's seed, or 0.
    pub seed: u32,
    pub onset: Option<f64>,
    pub bass: Option<f64>,
    pub mid: Option<f64>,
    pub high: Option<f64>,
    /// Set only for overlay scripts.
    pub overlay_length_beats: Option<f64>,
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
            bpm: 0.0,
            section_intent: None,
            section_elapsed_beats: None,
            next_intent: None,
            beats_until_next: None,
            beats_until_dip: None,
            drop_strength: None,
            next_drop_strength: None,
            seed: 0,
            onset: None,
            bass: None,
            mid: None,
            high: None,
            overlay_length_beats: None,
        }
    }
}

/// One section's plan, addressed by an Autolux beat index (a downbeat).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SectionCue {
    pub start_beat_index: u64,
    pub intent: SectionIntent,
    pub rotation: Vec<RotationEntry>,
    #[serde(default)]
    pub overlays: Vec<OverlayCue>,
    pub palette: String,
    #[serde(default)]
    pub drop_strength: Option<f64>,
    #[serde(default)]
    pub seed: u32,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RotationEntry {
    pub bank: String,
    pub autoloop: String,
    pub bars: u32,
    #[serde(default)]
    pub transition: Transition,
}

/// Times in beats from the section start; whether intensity is HTP is the loop's metadata.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OverlayCue {
    pub bank: String,
    pub autoloop: String,
    #[serde(default)]
    pub priority: i32,
    pub begin_beats: f64,
    pub length_beats: f64,
    #[serde(default)]
    pub every_bars: u32,
    #[serde(default)]
    pub attack_beats: f64,
    #[serde(default)]
    pub release_beats: f64,
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
    pub special_fx: Option<Box<SpecialFx>>,
    /// Pan runs the whole logical range, low soft limit to high and back, and tilt reaches the
    /// focus point's elevation, in place of `pan` and `tilt`.
    #[serde(default)]
    pub pan_sweep: bool,
    /// Pan and tilt throw the beam through the fixture's focus point, in place of `pan` and
    /// `tilt`.
    #[serde(default)]
    pub aim_focus: bool,
    /// The target fixture encodes with this in place of its loaded placement.
    #[serde(default)]
    pub pose: Option<Box<FixturePose>>,
}

/// A rig fixture's placement and patch fields: MVR Matrix, soft limits, focus point position
/// (mm), Art-Net universe, DMX start address and profile (its GDTF mode).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixturePose {
    pub matrix: Vec<f64>,
    pub pan: MovementRange,
    pub tilt: MovementRange,
    pub focus: Vec<f64>,
    pub universe: u32,
    pub address: u32,
    pub profile_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MovementRange {
    pub min: f64,
    pub max: f64,
    pub inverted: bool,
}

impl Default for MovementRange {
    fn default() -> Self {
        Self {
            min: 0.0,
            max: 1.0,
            inverted: false,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Beat {
        beat_index: u64,
        bar_phase: Option<u32>,
        beats_per_bar: u32,
        bpm: f64,
        #[serde(default)]
        features: Option<BeatFeatures>,
    },
    CueSection {
        value: Box<SectionCue>,
    },
    /// Drops every pending cue and ends the active one; the base look plays on.
    ClearCues,
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
        instantaneous_release: bool,
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
    /// An inhibitive submaster: scales the group's rendered output, never raises it.
    SetGroupMaster {
        group: String,
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
