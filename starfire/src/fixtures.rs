//! Validated fixture patches and semantic values encoded into safe DMX frames.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;

use crate::artnet::MAX_ARTNET_UNIVERSE;
use crate::model::{DMX_CHANNELS, FixtureState, FixtureStates, Rgb, Target};

const RIG_SCHEMA_VERSION: u32 = 1;
const PROFILE_SCHEMA_VERSION: u32 = 1;
const PROFILE_CATALOGUE: &str = include_str!("../rigs/profiles.json");
const DMX_MAX: f64 = u8::MAX as f64;
const FINE_POSITION_MAX: f64 = u16::MAX as f64;
pub const MAX_RIG_INPUT_BYTES: usize = 1024 * 1024;
pub const MAX_FIXTURES: usize = 512;
const MAX_FIXTURE_GROUPS: usize = 512;
const MAX_IDENTIFIER_BYTES: usize = 128;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RigDefinition {
    schema_version: u32,
    fixtures: Vec<FixturePatch>,
    #[serde(default)]
    groups: BTreeMap<String, Vec<String>>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FixturePatch {
    id: String,
    profile: String,
    universe: u16,
    address: usize,
    #[serde(default)]
    pan: MovementRange,
    #[serde(default)]
    tilt: MovementRange,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct MovementRange {
    min: f64,
    max: f64,
    inverted: bool,
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

impl MovementRange {
    fn validate(&self) -> Result<()> {
        unit_interval("movement minimum", self.min)?;
        unit_interval("movement maximum", self.max)?;
        ensure!(self.min <= self.max, "movement minimum exceeds maximum");
        Ok(())
    }

    fn map(&self, position: f64) -> f64 {
        let position = if self.inverted {
            1.0 - position
        } else {
            position
        };
        self.min + position * (self.max - self.min)
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileCatalogue {
    schema_version: u32,
    profiles: BTreeMap<String, FixtureProfile>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FixtureProfile {
    source: String,
    notes: String,
    /// Safe complete channel state; semantic effects may change only mapped channels.
    safe_values: Vec<u8>,
    dimmer: Option<usize>,
    rgb: Option<[usize; 3]>,
    white: Option<usize>,
    colour_wheel: Option<ColourWheel>,
    pan: Option<AxisChannels>,
    tilt: Option<AxisChannels>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ColourWheel {
    channel: usize,
    slots: Vec<ColourSlot>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ColourSlot {
    value: u8,
    colour: Rgb,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AxisChannels {
    coarse: usize,
    fine: Option<usize>,
}

impl FixtureProfile {
    fn validate(&self) -> Result<()> {
        ensure!(!self.source.trim().is_empty(), "profile source is required");
        ensure!(!self.notes.trim().is_empty(), "profile notes are required");
        ensure!(
            (1..=DMX_CHANNELS).contains(&self.safe_values.len()),
            "invalid profile footprint"
        );
        ensure!(
            self.dimmer.is_some() || self.rgb.is_some(),
            "profile cannot control brightness"
        );
        ensure!(
            self.white.is_none() || self.rgb.is_some(),
            "white channel requires RGB channels"
        );
        ensure!(
            self.colour_wheel.is_none() || self.rgb.is_none(),
            "conflicting colour encoders"
        );
        ensure!(
            self.colour_wheel.is_none() || self.dimmer.is_some(),
            "colour wheel requires dimmer"
        );

        let mut channels = Vec::new();
        channels.extend(self.dimmer);
        channels.extend(self.rgb.into_iter().flatten());
        channels.extend(self.white);
        if let Some(wheel) = &self.colour_wheel {
            ensure!(!wheel.slots.is_empty(), "colour wheel has no slots");
            let mut values = BTreeSet::new();
            for slot in &wheel.slots {
                validate_colour(slot.colour)?;
                ensure!(
                    colour_peak(slot.colour) > 0.0,
                    "colour wheel slot must emit light"
                );
                ensure!(values.insert(slot.value), "duplicate colour wheel slot");
            }
            channels.push(wheel.channel);
        }
        for axis in [&self.pan, &self.tilt].into_iter().flatten() {
            channels.push(axis.coarse);
            channels.extend(axis.fine);
        }
        let mut used = BTreeSet::new();
        for channel in channels {
            ensure!(
                channel < self.safe_values.len(),
                "profile channel exceeds footprint"
            );
            ensure!(used.insert(channel), "overlapping profile channels");
        }
        Ok(())
    }

    fn encode(&self, patch: &FixturePatch, state: &FixtureState, channels: &mut [u8]) {
        channels.copy_from_slice(&self.safe_values);
        let colour_gain = if self.dimmer.is_some() {
            1.0
        } else {
            state.intensity
        };
        if let Some([red, green, blue]) = self.rgb {
            let white = if self.white.is_some() {
                state
                    .colour
                    .red
                    .min(state.colour.green)
                    .min(state.colour.blue)
            } else {
                0.0
            };
            channels[red] = dmx_byte((state.colour.red - white) * colour_gain);
            channels[green] = dmx_byte((state.colour.green - white) * colour_gain);
            channels[blue] = dmx_byte((state.colour.blue - white) * colour_gain);
            if let Some(channel) = self.white {
                channels[channel] = dmx_byte(white * colour_gain);
            }
        }
        let mut intensity = state.intensity;
        if let Some(wheel) = &self.colour_wheel {
            let peak = colour_peak(state.colour);
            intensity *= peak;
            let requested = if peak > 0.0 {
                normalize_colour(state.colour, peak)
            } else {
                Rgb::default()
            };
            let slot = wheel
                .slots
                .iter()
                .min_by(|left, right| {
                    colour_distance(left.colour, requested)
                        .total_cmp(&colour_distance(right.colour, requested))
                })
                .expect("validated colour wheel has slots");
            channels[wheel.channel] = slot.value;
        }
        if let Some(channel) = self.dimmer {
            channels[channel] = dmx_byte(intensity);
        }
        for (axis, range, position) in [
            (&self.pan, &patch.pan, state.pan),
            (&self.tilt, &patch.tilt, state.tilt),
        ] {
            if let Some(axis) = axis {
                let position = range.map(position);
                if let Some(fine) = axis.fine {
                    let [coarse_value, fine_value] =
                        ((position * FINE_POSITION_MAX).round() as u16).to_be_bytes();
                    channels[axis.coarse] = coarse_value;
                    channels[fine] = fine_value;
                } else {
                    channels[axis.coarse] = dmx_byte(position);
                }
            }
        }
    }
}

/// An immutable, validated patch with fixture data separate from sequence logic.
#[derive(Clone, Debug)]
pub struct FixtureRig {
    fixtures: Vec<FixturePatch>,
    groups: BTreeMap<String, Vec<String>>,
    profiles: BTreeMap<String, FixtureProfile>,
}

impl FixtureRig {
    pub fn load(path: &Path) -> Result<Self> {
        let mut json = String::new();
        File::open(path)
            .with_context(|| format!("open rig {}", path.display()))?
            .take((MAX_RIG_INPUT_BYTES + 1) as u64)
            .read_to_string(&mut json)
            .with_context(|| format!("read rig {}", path.display()))?;
        Self::from_json(&json).with_context(|| format!("load rig {}", path.display()))
    }

    pub fn from_json(json: &str) -> Result<Self> {
        ensure!(
            json.len() <= MAX_RIG_INPUT_BYTES,
            "fixture rig exceeds input byte limit"
        );
        let definition: RigDefinition = serde_json::from_str(json).context("parse fixture rig")?;
        ensure!(
            definition.schema_version == RIG_SCHEMA_VERSION,
            "unsupported rig schema version"
        );
        ensure!(
            !definition.fixtures.is_empty() && definition.fixtures.len() <= MAX_FIXTURES,
            "rig must contain 1..{MAX_FIXTURES} fixtures"
        );
        ensure!(
            definition.groups.len() <= MAX_FIXTURE_GROUPS,
            "fixture group count exceeds limit"
        );
        let catalogue: ProfileCatalogue =
            serde_json::from_str(PROFILE_CATALOGUE).context("parse fixture profiles")?;
        ensure!(
            catalogue.schema_version == PROFILE_SCHEMA_VERSION,
            "unsupported fixture profile version"
        );
        for (id, profile) in &catalogue.profiles {
            profile
                .validate()
                .with_context(|| format!("profile {id}"))?;
        }

        let mut ids = BTreeSet::new();
        let mut occupied = BTreeMap::<u16, BTreeSet<usize>>::new();
        for patch in &definition.fixtures {
            validate_identifier(&patch.id)?;
            validate_identifier(&patch.profile)?;
            ensure!(
                ids.insert(patch.id.clone()),
                "duplicate fixture ID {}",
                patch.id
            );
            ensure!(
                patch.universe <= MAX_ARTNET_UNIVERSE,
                "fixture {} has invalid universe",
                patch.id
            );
            let profile = catalogue
                .profiles
                .get(&patch.profile)
                .with_context(|| format!("unknown fixture profile {}", patch.profile))?;
            ensure!(
                (1..=DMX_CHANNELS).contains(&patch.address),
                "fixture {} has invalid DMX address",
                patch.id
            );
            let end = patch.address - 1 + profile.safe_values.len();
            ensure!(
                end <= DMX_CHANNELS,
                "fixture {} exceeds universe boundary",
                patch.id
            );
            for channel in (patch.address - 1)..end {
                ensure!(
                    occupied.entry(patch.universe).or_default().insert(channel),
                    "fixture {} overlaps DMX channel {} in universe {}",
                    patch.id,
                    channel + 1,
                    patch.universe
                );
            }
            patch
                .pan
                .validate()
                .with_context(|| format!("fixture {} pan bounds", patch.id))?;
            patch
                .tilt
                .validate()
                .with_context(|| format!("fixture {} tilt bounds", patch.id))?;
        }
        for (group, members) in &definition.groups {
            validate_identifier(group)?;
            ensure!(!members.is_empty(), "fixture group {group} is empty");
            ensure!(
                members.len() <= MAX_FIXTURES,
                "fixture group {group} exceeds member limit"
            );
            let mut unique = BTreeSet::new();
            for member in members {
                ensure!(
                    ids.contains(member),
                    "group {group} contains unknown fixture {member}"
                );
                ensure!(
                    unique.insert(member),
                    "group {group} repeats fixture {member}"
                );
            }
        }
        Ok(Self {
            fixtures: definition.fixtures,
            groups: definition.groups,
            profiles: catalogue.profiles,
        })
    }

    pub fn resolve(&self, target: &Target) -> Result<Vec<String>> {
        match target {
            Target::All => Ok(self.fixture_ids()),
            Target::Group(group) => self
                .groups
                .get(group)
                .cloned()
                .with_context(|| format!("unknown fixture group {group}")),
            Target::Fixture(id) => {
                ensure!(
                    self.fixtures.iter().any(|fixture| fixture.id == *id),
                    "unknown fixture {id}"
                );
                Ok(vec![id.clone()])
            }
        }
    }

    pub fn groups(&self) -> &BTreeMap<String, Vec<String>> {
        &self.groups
    }

    pub fn fixture_ids(&self) -> Vec<String> {
        self.fixtures
            .iter()
            .map(|fixture| fixture.id.clone())
            .collect()
    }

    pub fn empty_states(&self) -> FixtureStates {
        self.fixture_ids()
            .into_iter()
            .map(|id| (id, FixtureState::default()))
            .collect()
    }

    /// Encode a complete state after time-dependent strobe has been resolved.
    /// Blackout means zero intensity, not zeroing movement or device-control bytes.
    pub fn encode(&self, states: &FixtureStates) -> Result<BTreeMap<u16, Vec<u8>>> {
        ensure!(
            states.len() == self.fixtures.len(),
            "fixture state does not match rig"
        );
        let mut universes = BTreeMap::new();
        for fixture in &self.fixtures {
            let state = states
                .get(&fixture.id)
                .with_context(|| format!("missing fixture state {}", fixture.id))?;
            validate_state(state).with_context(|| format!("fixture {}", fixture.id))?;
            let profile = &self.profiles[&fixture.profile];
            let frame = universes
                .entry(fixture.universe)
                .or_insert_with(|| vec![0; DMX_CHANNELS]);
            let start = fixture.address - 1;
            profile.encode(
                fixture,
                state,
                &mut frame[start..start + profile.safe_values.len()],
            );
        }
        Ok(universes)
    }
}

fn validate_identifier(identifier: &str) -> Result<()> {
    ensure!(
        !identifier.trim().is_empty() && identifier.len() <= MAX_IDENTIFIER_BYTES,
        "fixture, profile and group identifiers must contain 1..{MAX_IDENTIFIER_BYTES} bytes"
    );
    Ok(())
}

fn validate_state(state: &FixtureState) -> Result<()> {
    unit_interval("intensity", state.intensity)?;
    validate_colour(state.colour)?;
    unit_interval("pan", state.pan)?;
    unit_interval("tilt", state.tilt)?;
    ensure!(
        state.strobe_hz == 0.0,
        "strobe must be resolved into frame intensity before fixture encoding"
    );
    Ok(())
}

fn unit_interval(name: &str, value: f64) -> Result<()> {
    ensure!(
        value.is_finite() && (0.0..=1.0).contains(&value),
        "{name} must be finite and in 0..1"
    );
    Ok(())
}

fn validate_colour(colour: Rgb) -> Result<()> {
    unit_interval("red", colour.red)?;
    unit_interval("green", colour.green)?;
    unit_interval("blue", colour.blue)
}

fn colour_peak(colour: Rgb) -> f64 {
    colour.red.max(colour.green).max(colour.blue)
}

fn normalize_colour(colour: Rgb, peak: f64) -> Rgb {
    Rgb {
        red: colour.red / peak,
        green: colour.green / peak,
        blue: colour.blue / peak,
    }
}

fn colour_distance(left: Rgb, right: Rgb) -> f64 {
    (left.red - right.red).powi(2)
        + (left.green - right.green).powi(2)
        + (left.blue - right.blue).powi(2)
}

fn dmx_byte(value: f64) -> u8 {
    (value * DMX_MAX).round() as u8
}
