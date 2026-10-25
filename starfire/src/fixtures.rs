//! Validated fixture patches and semantic values encoded into safe DMX frames.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;

use crate::artnet::MAX_ARTNET_UNIVERSE;
use crate::gdtf::GdtfProfile;
use crate::model::{AttributeValue, DMX_CHANNELS, FixtureState, FixtureStates, Rgb, Target};
use crate::protocol::wire;

const RIG_SCHEMA_VERSION: u32 = 1;
const PROFILE_SCHEMA_VERSION: u32 = 1;
const PROFILE_CATALOGUE: &str = include_str!("../../rigs/profiles.json");
pub const MAX_RIG_INPUT_BYTES: usize = 1024 * 1024;
pub const MAX_FIXTURES: usize = 512;
const MAX_FIXTURE_GROUPS: usize = 512;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_PAN_OFFSET_DEGREES: f64 = 540.0;
const MAX_TILT_OFFSET_DEGREES: f64 = 270.0;
const FULL_TURN_DEGREES: f64 = 360.0;

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
    #[serde(default)]
    pan_offset_degrees: f64,
    #[serde(default)]
    tilt_offset_degrees: f64,
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
    fn from_wire(value: &Option<wire::fixture::MovementRange>) -> Self {
        value.as_ref().map_or_else(Self::default, |value| Self {
            min: value.minimum.unwrap_or(0.0),
            max: value.maximum.unwrap_or(1.0),
            inverted: value.inverted,
        })
    }

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
    profiles: BTreeMap<String, ProfileDefinition>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileDefinition {
    gdtf_xml: String,
    dmx_mode: String,
}

/// An immutable, validated patch with fixture data separate from autoloop logic.
#[derive(Clone, Debug)]
pub struct FixtureRig {
    fixtures: Vec<FixturePatch>,
    groups: BTreeMap<String, Vec<String>>,
    profiles: BTreeMap<String, GdtfProfile>,
}

impl FixtureRig {
    pub fn from_wire(rig: &wire::RigDefinition) -> Result<Self> {
        let fixtures = rig
            .fixtures
            .iter()
            .map(|fixture| -> Result<_> {
                Ok(FixturePatch {
                    id: fixture.fixture_id.clone(),
                    profile: fixture.profile_id.clone(),
                    universe: u16::try_from(fixture.universe)
                        .context("fixture universe exceeds range")?,
                    address: fixture.address as usize,
                    pan: MovementRange::from_wire(&fixture.pan),
                    tilt: MovementRange::from_wire(&fixture.tilt),
                    pan_offset_degrees: fixture.pan_offset_degrees,
                    tilt_offset_degrees: fixture.tilt_offset_degrees,
                })
            })
            .collect::<Result<_>>()?;
        let mut groups = BTreeMap::new();
        for group in &rig.groups {
            ensure!(
                groups
                    .insert(group.group_id.clone(), group.fixture_ids.clone())
                    .is_none(),
                "duplicate fixture group ID {}",
                group.group_id
            );
        }
        let mut profiles = BTreeMap::new();
        for profile in &rig.profiles {
            let converted = GdtfProfile::from_xml(&profile.gdtf_xml, &profile.dmx_mode)
                .with_context(|| format!("profile {}", profile.profile_id))?;
            ensure!(
                profiles
                    .insert(profile.profile_id.clone(), converted)
                    .is_none(),
                "duplicate fixture profile ID {}",
                profile.profile_id
            );
        }
        Self::from_parts(fixtures, groups, profiles)
    }

    pub fn load(path: &Path) -> Result<Self> {
        Self::from_json(&read_rig_file(path)?)
    }

    pub fn load_with_profiles(path: &Path, profiles: &Path) -> Result<Self> {
        Self::from_json_with_profiles(&read_rig_file(path)?, &read_rig_file(profiles)?)
    }

    pub fn from_json(json: &str) -> Result<Self> {
        Self::from_json_with_profiles(json, PROFILE_CATALOGUE)
    }

    pub fn from_json_with_profiles(json: &str, profiles_json: &str) -> Result<Self> {
        ensure!(
            json.len() <= MAX_RIG_INPUT_BYTES,
            "fixture rig exceeds input byte limit"
        );
        ensure!(
            profiles_json.len() <= MAX_RIG_INPUT_BYTES,
            "profile catalogue exceeds input byte limit"
        );
        let definition: RigDefinition = serde_json::from_str(json).context("parse fixture rig")?;
        ensure!(
            definition.schema_version == RIG_SCHEMA_VERSION,
            "unsupported rig schema version"
        );
        let catalogue: ProfileCatalogue =
            serde_json::from_str(profiles_json).context("parse GDTF profile catalogue")?;
        ensure!(
            catalogue.schema_version == PROFILE_SCHEMA_VERSION,
            "unsupported fixture profile version"
        );
        let used_profiles: BTreeSet<_> = definition
            .fixtures
            .iter()
            .map(|fixture| fixture.profile.as_str())
            .collect();
        let profiles = catalogue
            .profiles
            .into_iter()
            .filter(|(id, _)| used_profiles.contains(id.as_str()))
            .map(|(id, profile)| {
                let compiled = GdtfProfile::from_xml(&profile.gdtf_xml, &profile.dmx_mode)
                    .with_context(|| format!("profile {id}"))?;
                Ok((id, compiled))
            })
            .collect::<Result<_>>()?;
        Self::from_parts(definition.fixtures, definition.groups, profiles)
    }

    fn from_parts(
        fixtures: Vec<FixturePatch>,
        groups: BTreeMap<String, Vec<String>>,
        profiles: BTreeMap<String, GdtfProfile>,
    ) -> Result<Self> {
        ensure!(
            !fixtures.is_empty() && fixtures.len() <= MAX_FIXTURES,
            "rig must contain 1..{MAX_FIXTURES} fixtures"
        );
        ensure!(
            groups.len() <= MAX_FIXTURE_GROUPS,
            "fixture group count exceeds limit"
        );
        ensure!(
            profiles.len() <= MAX_FIXTURES,
            "fixture profile count exceeds limit"
        );
        for id in profiles.keys() {
            validate_identifier(id)?;
        }

        let mut ids = BTreeSet::new();
        let mut occupied = BTreeMap::<u16, BTreeSet<usize>>::new();
        for patch in &fixtures {
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
            let profile = profiles
                .get(&patch.profile)
                .with_context(|| format!("unknown fixture profile {}", patch.profile))?;
            ensure!(
                (1..=DMX_CHANNELS).contains(&patch.address),
                "fixture {} has invalid DMX address",
                patch.id
            );
            let end = patch.address - 1 + profile.footprint();
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
            for (axis, attribute, offset, limit) in [
                (
                    "pan",
                    "Pan",
                    patch.pan_offset_degrees,
                    MAX_PAN_OFFSET_DEGREES,
                ),
                (
                    "tilt",
                    "Tilt",
                    patch.tilt_offset_degrees,
                    MAX_TILT_OFFSET_DEGREES,
                ),
            ] {
                ensure!(
                    offset.is_finite() && offset.abs() <= limit,
                    "fixture {} {axis} offset must be finite and within +/-{limit} degrees",
                    patch.id
                );
                ensure!(
                    offset == 0.0 || profile.movement_range(attribute).is_some(),
                    "fixture {} has a {axis} offset but its profile has no continuous {attribute}",
                    patch.id
                );
            }
        }
        for (group, members) in &groups {
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
            fixtures,
            groups,
            profiles,
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

    pub fn validate_attributes(
        &self,
        fixture_id: &str,
        values: &BTreeMap<String, AttributeValue>,
    ) -> Result<()> {
        let patch = self
            .fixtures
            .iter()
            .find(|fixture| fixture.id == fixture_id)
            .context("unknown fixture")?;
        self.profiles[&patch.profile].validate_attributes(values)
    }

    pub fn interpolate_attribute(
        &self,
        fixture_id: &str,
        key: &str,
        from: &AttributeValue,
        to: &AttributeValue,
        amount: f64,
    ) -> AttributeValue {
        let patch = self
            .fixtures
            .iter()
            .find(|fixture| fixture.id == fixture_id)
            .expect("validated fixture state");
        self.profiles[&patch.profile].interpolate_attribute(key, from, to, amount)
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
            let pan_range = profile.movement_range("Pan");
            let tilt_range = profile.movement_range("Tilt");
            let pan = |position| {
                pan_range.map_or(position, |(from, to)| {
                    offset_pan(
                        fixture.pan.map(position),
                        fixture.pan_offset_degrees,
                        from,
                        to,
                    )
                })
            };
            let tilt = |position| {
                tilt_range.map_or(position, |(from, to)| {
                    offset_tilt(
                        fixture.tilt.map(position),
                        fixture.tilt_offset_degrees,
                        from,
                        to,
                    )
                })
            };
            let mut calibrated = state.clone();
            calibrated.pan = pan(state.pan);
            calibrated.tilt = tilt(state.tilt);
            profile
                .encode_calibrated(
                    &calibrated,
                    &mut frame[start..start + profile.footprint()],
                    pan,
                    tilt,
                )
                .with_context(|| format!("fixture {}", fixture.id))?;
        }
        Ok(universes)
    }
}

// The rig declares where each head's home is, so autoloops can address
// normalised directions without knowing how a fixture was hung.
fn offset_pan(position: f64, offset_degrees: f64, from: f64, to: f64) -> f64 {
    if offset_degrees == 0.0 || from == to {
        return position;
    }
    let (low, high) = (from.min(to), from.max(to));
    let mut degrees = from + position * (to - from) + offset_degrees;
    // Whole turns preserve beam direction; modulo travel would not on a 540-degree head.
    if degrees > high {
        degrees -= FULL_TURN_DEGREES * ((degrees - high) / FULL_TURN_DEGREES).ceil();
    }
    if degrees < low {
        degrees += FULL_TURN_DEGREES * ((low - degrees) / FULL_TURN_DEGREES).ceil();
    }
    ((degrees.clamp(low, high) - from) / (to - from)).clamp(0.0, 1.0)
}

fn offset_tilt(position: f64, offset_degrees: f64, from: f64, to: f64) -> f64 {
    if offset_degrees == 0.0 || from == to {
        return position;
    }
    (position + offset_degrees / (to - from)).clamp(0.0, 1.0)
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
    unit_interval("attribute brightness", state.attribute_brightness)?;
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

fn read_rig_file(path: &Path) -> Result<String> {
    let mut json = String::new();
    File::open(path)
        .with_context(|| format!("open fixture data {}", path.display()))?
        .take((MAX_RIG_INPUT_BYTES + 1) as u64)
        .read_to_string(&mut json)
        .with_context(|| format!("read fixture data {}", path.display()))?;
    ensure!(
        json.len() <= MAX_RIG_INPUT_BYTES,
        "fixture data exceeds input byte limit"
    );
    Ok(json)
}

#[cfg(test)]
mod tests {
    use super::{offset_pan, offset_tilt};

    fn degrees(position: f64, travel: f64) -> f64 {
        position * travel
    }

    #[test]
    fn pan_offset_moves_home_in_physical_degrees() {
        assert!((degrees(offset_pan(0.5, 180.0, 0.0, 540.0), 540.0) - 450.0).abs() < 1e-9);
        assert!((degrees(offset_pan(0.5, -180.0, 0.0, 540.0), 540.0) - 90.0).abs() < 1e-9);
    }

    #[test]
    fn pan_offset_wraps_by_whole_turns_to_keep_beam_direction() {
        // 420 + 180 = 600 points like 240; 600 mod 540 = 60 would face the opposite way.
        let wrapped = degrees(offset_pan(420.0 / 540.0, 180.0, 0.0, 540.0), 540.0);
        assert!((wrapped - 240.0).abs() < 1e-9);
        let below = degrees(offset_pan(0.0, -90.0, 0.0, 540.0), 540.0);
        assert!((below - 270.0).abs() < 1e-9);
        // Short travel holds at the end stop when no whole turn reaches it.
        assert_eq!(offset_pan(0.5, 180.0, 0.0, 270.0), 1.0);
    }

    #[test]
    fn tilt_offset_clamps_at_end_stops() {
        assert_eq!(offset_tilt(0.9, 90.0, 0.0, 270.0), 1.0);
        assert_eq!(offset_tilt(0.1, -90.0, 0.0, 270.0), 0.0);
        assert!((degrees(offset_tilt(0.5, 45.0, 0.0, 270.0), 270.0) - 180.0).abs() < 1e-9);
    }

    #[test]
    fn zero_offset_is_identity_including_travel_ends() {
        for position in [0.0, 0.25, 0.5, 1.0] {
            assert_eq!(offset_pan(position, 0.0, 0.0, 540.0), position);
            assert_eq!(offset_tilt(position, 0.0, 0.0, 270.0), position);
        }
    }
}
