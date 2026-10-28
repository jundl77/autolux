use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::Path;

use anyhow::{Context, Result, ensure};

use crate::artnet::MAX_ARTNET_UNIVERSE;
use crate::gdtf::{GdtfCell, GdtfProfile};
use crate::model::{AttributeValue, DMX_CHANNELS, FixtureState, FixtureStates, Rgb, Target};
use crate::protocol::{MAX_LOGIN_BYTES, wire};
use crate::spatial::{Position, Spatial};
use prost::Message;

pub const MAX_FIXTURES: usize = 512;
pub const MAX_CELLS: usize = 512;
const MAX_FIXTURE_GROUPS: usize = 512;
const MAX_IDENTIFIER_BYTES: usize = 128;
const ORTHONORMAL_TOLERANCE: f64 = 1e-3;
const GIMBAL_TOLERANCE: f64 = 1e-9;
const FULL_TURN_DEGREES: f64 = 360.0;
const HALF_TURN_DEGREES: f64 = 180.0;
const MAX_PITCH_DEGREES: f64 = 60.0;
const MATRIX_VALUES: usize = 12;

#[derive(Clone, Debug)]
struct FixturePatch {
    id: String,
    name: String,
    profile: String,
    universe: u16,
    address: usize,
    pan: MovementRange,
    tilt: MovementRange,
    pan_offset_degrees: f64,
    tilt_offset_degrees: f64,
    declared_cells: Vec<wire::fixture::Cell>,
    cells: Vec<String>,
    position: Position,
    area: wire::StageArea,
}

#[derive(Clone, Debug)]
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

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct HomeOrientation {
    pub pan_offset_degrees: f64,
    pub tilt_offset_degrees: f64,
    pub reversed: bool,
}

pub fn home_orientation(matrix: &[f64]) -> Result<HomeOrientation> {
    if matrix.is_empty() {
        return Ok(HomeOrientation {
            pan_offset_degrees: 0.0,
            tilt_offset_degrees: 0.0,
            reversed: false,
        });
    }
    ensure!(
        matrix.len() == MATRIX_VALUES && matrix.iter().all(|value| value.is_finite()),
        "MVR Matrix must contain twelve finite numbers"
    );
    let mut axes = [[0.0; 3]; 3];
    for (axis, values) in axes.iter_mut().zip(matrix.as_chunks::<3>().0) {
        let length = values.iter().map(|value| value * value).sum::<f64>().sqrt();
        ensure!(
            length > ORTHONORMAL_TOLERANCE,
            "MVR Matrix axes must be nonzero"
        );
        for (target, value) in axis.iter_mut().zip(values) {
            *target = value / length;
        }
    }
    let [u, v, w] = axes;
    let dot = |a: [f64; 3], b: [f64; 3]| a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    let cross = [
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    ];
    ensure!(
        dot(u, v).abs().max(dot(v, w).abs()).max(dot(u, w).abs()) <= ORTHONORMAL_TOLERANCE
            && dot(cross, w) >= 1.0 - ORTHONORMAL_TOLERANCE,
        "MVR Matrix rotation must be orthogonal and unmirrored"
    );
    let pitch = v[2].clamp(-1.0, 1.0).asin().to_degrees();
    ensure!(
        pitch.abs() <= MAX_PITCH_DEGREES,
        "fixture pitch {pitch:.1} exceeds +/-{MAX_PITCH_DEGREES} degrees; the tilt home is only approximated for near-level bases"
    );
    let mut yaw = if v[2].abs() > 1.0 - GIMBAL_TOLERANCE {
        u[1].atan2(u[0])
    } else {
        (-v[0]).atan2(v[1])
    }
    .to_degrees();
    if yaw <= -HALF_TURN_DEGREES {
        yaw += FULL_TURN_DEGREES;
    }
    Ok(HomeOrientation {
        pan_offset_degrees: yaw + 0.0,
        tilt_offset_degrees: pitch + 0.0,
        reversed: w[2] < 0.0,
    })
}

/// Cells are the addressing unit: targets, groups and states name cells, never whole fixtures.
#[derive(Clone, Debug)]
pub struct FixtureRig {
    fixtures: Vec<FixturePatch>,
    groups: BTreeMap<String, Vec<String>>,
    accent_groups: BTreeSet<String>,
    accent_cells: BTreeSet<String>,
    profiles: BTreeMap<String, Vec<GdtfCell>>,
    cells: BTreeMap<String, (usize, usize)>,
    names: BTreeMap<String, usize>,
    spatial: Spatial,
}

/// A single-cell fixture's cell is the fixture itself; other cells are named after their geometry.
pub fn cell_id(fixture_id: &str, geometry: &str, cell_count: usize) -> String {
    if cell_count == 1 {
        fixture_id.to_owned()
    } else {
        format!("{fixture_id}/{geometry}")
    }
}

impl FixtureRig {
    pub fn from_wire(rig: &wire::RigDefinition) -> Result<Self> {
        let fixtures = rig
            .fixtures
            .iter()
            .map(|fixture| -> Result<_> {
                let home = home_orientation(&fixture.matrix)
                    .with_context(|| format!("fixture {}", fixture.name))?;
                let mut pan = MovementRange::from_wire(&fixture.pan);
                let mut tilt = MovementRange::from_wire(&fixture.tilt);
                pan.inverted ^= home.reversed;
                tilt.inverted ^= home.reversed;
                Ok(FixturePatch {
                    id: fixture.fixture_id.clone(),
                    name: fixture.name.clone(),
                    profile: fixture.profile_id.clone(),
                    universe: u16::try_from(fixture.universe)
                        .context("fixture universe exceeds range")?,
                    address: fixture.address as usize,
                    pan,
                    tilt,
                    pan_offset_degrees: home.pan_offset_degrees,
                    tilt_offset_degrees: home.tilt_offset_degrees,
                    declared_cells: fixture.cells.clone(),
                    cells: Vec::new(),
                    // The Matrix's o row; an empty Matrix is identity, at the origin.
                    position: fixture
                        .matrix
                        .get(9..12)
                        .map_or([0.0; 3], |o| [o[0], o[1], o[2]]),
                    area: Some(fixture.area())
                        .filter(|&area| area != wire::StageArea::Unspecified)
                        .with_context(|| format!("fixture {} has no stage area", fixture.name))?,
                })
            })
            .collect::<Result<_>>()?;
        let mut groups = BTreeMap::new();
        let accent_groups = rig
            .groups
            .iter()
            .filter(|group| group.role() == wire::GroupRole::Accent)
            .map(|group| group.group_id.clone())
            .collect();
        for group in &rig.groups {
            ensure!(
                wire::GroupRole::try_from(group.role).is_ok(),
                "fixture group {} has unknown role {}",
                group.group_id,
                group.role
            );
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
            let mode = profile.mode.as_ref().with_context(|| {
                format!("profile {} has no compiled DMX mode", profile.profile_id)
            })?;
            let converted = GdtfProfile::cells_from_wire(mode)
                .with_context(|| format!("profile {}", profile.profile_id))?;
            ensure!(
                profiles
                    .insert(profile.profile_id.clone(), converted)
                    .is_none(),
                "duplicate fixture profile ID {}",
                profile.profile_id
            );
        }
        Self::from_parts(fixtures, groups, accent_groups, profiles)
    }

    pub fn load(path: &Path) -> Result<Self> {
        Self::decode(&read_bounded(path, MAX_LOGIN_BYTES)?)
    }

    pub fn decode(definition: &[u8]) -> Result<Self> {
        Self::from_wire(&wire::RigDefinition::decode(definition).context("decode RigDefinition")?)
    }

    fn from_parts(
        mut fixtures: Vec<FixturePatch>,
        groups: BTreeMap<String, Vec<String>>,
        accent_groups: BTreeSet<String>,
        profiles: BTreeMap<String, Vec<GdtfCell>>,
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
        let mut names = BTreeMap::new();
        let mut cells = BTreeMap::new();
        let mut occupied = BTreeMap::<u16, BTreeSet<usize>>::new();
        for (index, patch) in fixtures.iter_mut().enumerate() {
            validate_identifier(&patch.id)?;
            validate_identifier(&patch.name)?;
            validate_identifier(&patch.profile)?;
            ensure!(
                ids.insert(patch.id.clone()),
                "duplicate fixture ID {} ({})",
                patch.id,
                patch.name
            );
            // Autoloops name fixtures by label, so a label must pick out one fixture.
            ensure!(
                names.insert(patch.name.clone(), index).is_none(),
                "duplicate fixture name {}",
                patch.name
            );
            ensure!(
                patch.universe <= MAX_ARTNET_UNIVERSE,
                "fixture {} has invalid universe",
                patch.name
            );
            let profile_cells = profiles
                .get(&patch.profile)
                .with_context(|| format!("unknown fixture profile {}", patch.profile))?;
            let profile = &profile_cells[0].profile;
            ensure!(
                patch.declared_cells.is_empty() || declares(&patch.declared_cells, profile_cells),
                "fixture {} declares cells that differ from its GDTF mode",
                patch.name
            );
            for (cell, layout) in profile_cells.iter().enumerate() {
                let id = cell_id(&patch.id, &layout.geometry, profile_cells.len());
                validate_identifier(&id)?;
                ensure!(
                    cells.insert(id.clone(), (index, cell)).is_none(),
                    "duplicate cell ID {id}"
                );
                patch.cells.push(id);
            }
            ensure!(
                (1..=DMX_CHANNELS).contains(&patch.address),
                "fixture {} has invalid DMX address",
                patch.name
            );
            let end = patch.address - 1 + profile.footprint();
            ensure!(
                end <= DMX_CHANNELS,
                "fixture {} exceeds universe boundary",
                patch.name
            );
            for channel in (patch.address - 1)..end {
                ensure!(
                    occupied.entry(patch.universe).or_default().insert(channel),
                    "fixture {} overlaps DMX channel {} in universe {}",
                    patch.name,
                    channel + 1,
                    patch.universe
                );
            }
            patch
                .pan
                .validate()
                .with_context(|| format!("fixture {} pan bounds", patch.name))?;
            patch
                .tilt
                .validate()
                .with_context(|| format!("fixture {} tilt bounds", patch.name))?;
        }
        ensure!(
            cells.len() <= MAX_CELLS,
            "rig must contain at most {MAX_CELLS} cells"
        );
        let mut cell_groups = BTreeMap::new();
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
            // A master cell follows its pixels, so groups name the pixels only.
            let members = members
                .iter()
                .filter_map(|member| fixtures.iter().find(|fixture| fixture.id == *member))
                .flat_map(|fixture| {
                    fixture
                        .cells
                        .iter()
                        .zip(&profiles[&fixture.profile])
                        .filter(|(_, cell)| cell.governed.is_empty())
                        .map(|(id, _)| id.clone())
                })
                .collect();
            cell_groups.insert(group.clone(), members);
        }
        let placed = || {
            fixtures.iter().flat_map(|fixture| {
                std::iter::once(&fixture.id)
                    .chain(&fixture.cells)
                    .map(move |id| (id.clone(), fixture))
            })
        };
        let positions = placed()
            .map(|(id, fixture)| (id, fixture.position))
            .collect();
        let areas = placed().map(|(id, fixture)| (id, fixture.area)).collect();
        let spatial = Spatial::new(positions, areas, &cell_groups, |cell| {
            cells.get(cell).map_or(usize::MAX, |&(fixture, _)| fixture)
        });
        for (group, members) in &cell_groups {
            for accent in accent_groups.iter().filter(|accent| *accent != group) {
                ensure!(
                    !cell_groups[accent]
                        .iter()
                        .any(|cell| members.contains(cell)),
                    "loop group {group} shares a fixture with accent group {accent}, which plays specials only"
                );
            }
        }
        let accent_cells = accent_groups
            .iter()
            .flat_map(|group| &cell_groups[group])
            .cloned()
            .collect();
        Ok(Self {
            fixtures,
            groups: cell_groups,
            accent_groups,
            accent_cells,
            profiles,
            cells,
            names,
            spatial,
        })
    }

    /// A fixture target names one cell, or every cell of a multi-cell fixture.
    pub fn resolve(&self, target: &Target) -> Result<Vec<String>> {
        match target {
            Target::All => Ok(self.cell_ids()),
            Target::Group(group) => self
                .groups
                .get(group)
                .cloned()
                .with_context(|| format!("unknown fixture group {group}")),
            Target::Fixture(id) => {
                if self.cells.contains_key(id) {
                    return Ok(vec![id.clone()]);
                }
                self.fixtures
                    .iter()
                    .find(|fixture| fixture.id == *id)
                    .map(|fixture| fixture.cells.clone())
                    .with_context(|| format!("unknown fixture {id}"))
            }
        }
    }

    pub fn groups(&self) -> &BTreeMap<String, Vec<String>> {
        &self.groups
    }

    /// Cell positions, stage areas and mirror partners, derived once at rig load.
    pub fn spatial(&self) -> &Spatial {
        &self.spatial
    }

    /// Groups whose MVR Layer is `accent`: they play specials only, never autoloops.
    pub fn is_accent(&self, group: &str) -> bool {
        self.accent_groups.contains(group)
    }

    pub fn is_accent_cell(&self, cell_id: &str) -> bool {
        self.accent_cells.contains(cell_id)
    }

    pub fn refuse_accent_group(&self, group: &str) -> Result<()> {
        ensure!(
            !self.is_accent(group),
            "accent group {group} plays specials only, never autoloops"
        );
        Ok(())
    }

    /// The cells an autoloop's target lights. Naming an accent group or fixture is refused;
    /// `all` passes over accent cells, which rig load keeps out of loop groups.
    pub fn resolve_for_loop(&self, target: &Target) -> Result<Vec<String>> {
        let mut cells = self.resolve(target)?;
        match target {
            Target::Group(group) => self.refuse_accent_group(group)?,
            Target::Fixture(id) => ensure!(
                !cells.iter().any(|cell| self.is_accent_cell(cell)),
                "accent fixture {} plays specials only, never autoloops",
                self.label(id)
            ),
            Target::All => cells.retain(|cell| !self.is_accent_cell(cell)),
        }
        Ok(cells)
    }

    pub fn validate_attributes(
        &self,
        cell_id: &str,
        values: &BTreeMap<String, AttributeValue>,
    ) -> Result<()> {
        self.profile(cell_id)
            .context("unknown cell")?
            .validate_attributes(values)
    }

    pub fn profile(&self, cell_id: &str) -> Option<&GdtfProfile> {
        let &(fixture, cell) = self.cells.get(cell_id)?;
        Some(&self.profiles[&self.fixtures[fixture].profile][cell].profile)
    }

    pub fn fixture_of(&self, cell_id: &str) -> Option<&str> {
        let &(fixture, _) = self.cells.get(cell_id)?;
        Some(&self.fixtures[fixture].id)
    }

    /// The fixture ID a label names; autoloops are written against labels.
    pub fn fixture_named(&self, name: &str) -> Option<&str> {
        Some(&self.fixtures[*self.names.get(name)?].id)
    }

    /// The target with a fixture label resolved to its ID; IDs and unknown names pass through.
    pub fn by_label(&self, target: Target) -> Target {
        match target {
            Target::Fixture(name) => {
                Target::Fixture(self.fixture_named(&name).map_or(name, str::to_owned))
            }
            target => target,
        }
    }

    /// A fixture's or cell's ID with its fixture's label in place of the fixture ID, for
    /// messages people read; unknown IDs come back unchanged.
    pub fn label(&self, id: &str) -> String {
        let fixture = self
            .cells
            .get(id)
            .map(|&(fixture, _)| &self.fixtures[fixture])
            .or_else(|| self.fixtures.iter().find(|fixture| fixture.id == id));
        fixture.map_or_else(
            || id.to_owned(),
            |fixture| id.replacen(&fixture.id, &fixture.name, 1),
        )
    }

    pub fn fixture_ids(&self) -> Vec<String> {
        self.fixtures
            .iter()
            .map(|fixture| fixture.id.clone())
            .collect()
    }

    /// Every cell in patch order, each fixture's cells in DMX offset order.
    pub fn cell_ids(&self) -> Vec<String> {
        self.fixtures
            .iter()
            .flat_map(|fixture| fixture.cells.iter().cloned())
            .collect()
    }

    pub fn empty_states(&self) -> FixtureStates {
        self.cell_ids()
            .into_iter()
            .map(|id| (id, FixtureState::default()))
            .collect()
    }

    // Blackout zeroes intensity only; movement and device-control bytes keep their values.
    pub fn encode(&self, states: &FixtureStates) -> Result<BTreeMap<u16, Vec<u8>>> {
        ensure!(
            states.len() == self.cells.len(),
            "cell state does not match rig"
        );
        let mut universes = BTreeMap::new();
        for fixture in &self.fixtures {
            let cells = &self.profiles[&fixture.profile];
            let profile = &cells[0].profile;
            let frame = universes
                .entry(fixture.universe)
                .or_insert_with(|| vec![0; DMX_CHANNELS]);
            let start = fixture.address - 1;
            let pan_range = profile.movement_range("Pan");
            let tilt_range = profile.movement_range("Tilt");
            let pan = |position| {
                let position = fixture.pan.map(position);
                pan_range.map_or(position, |(from, to)| {
                    offset_pan(position, fixture.pan_offset_degrees, from, to)
                })
            };
            let tilt = |position| {
                let position = fixture.tilt.map(position);
                tilt_range.map_or(position, |(from, to)| {
                    offset_tilt(position, fixture.tilt_offset_degrees, from, to)
                })
            };
            let cell_states = fixture
                .cells
                .iter()
                .map(|id| {
                    let state = states
                        .get(id)
                        .with_context(|| format!("missing cell state {id}"))?;
                    validate_state(state).with_context(|| format!("cell {id}"))?;
                    Ok(state)
                })
                .collect::<Result<Vec<_>>>()?;
            let master_levels: Vec<f64> = cells
                .iter()
                .map(|cell| {
                    cell.governed
                        .iter()
                        .map(|&pixel| light_demand(cell_states[pixel]))
                        .fold(0.0, f64::max)
                })
                .collect();
            for (index, ((id, cell), state)) in fixture
                .cells
                .iter()
                .zip(cells)
                .zip(&cell_states)
                .enumerate()
            {
                let mut calibrated = (*state).clone();
                if !cell.governed.is_empty() {
                    calibrated.intensity = master_levels[index];
                } else if let Some(master) = cells
                    .iter()
                    .position(|master| master.governed.contains(&index))
                {
                    // Master level times pixel output equals what the pixel would show alone.
                    let level = master_levels[master];
                    let scale = |value: f64| {
                        if level > 0.0 {
                            (value / level).min(1.0)
                        } else {
                            0.0
                        }
                    };
                    calibrated.intensity = scale(state.intensity);
                    calibrated.attribute_brightness = scale(state.attribute_brightness);
                }
                calibrated.pan = pan(state.pan);
                calibrated.tilt = tilt(state.tilt);
                cell.profile
                    .encode_calibrated(
                        &calibrated,
                        &mut frame[start..start + profile.footprint()],
                        pan,
                        tilt,
                    )
                    .with_context(|| format!("cell {id}"))?;
            }
        }
        Ok(universes)
    }
}

/// The light a pixel cell asks for: its intensity, or the brightness scaling its explicit
/// attributes when it has any.
fn light_demand(state: &FixtureState) -> f64 {
    if state.attributes.is_empty() {
        state.intensity
    } else {
        state.intensity.max(state.attribute_brightness)
    }
}

fn declares(declared: &[wire::fixture::Cell], cells: &[GdtfCell]) -> bool {
    declared.len() == cells.len()
        && declared.iter().zip(cells).all(|(declared, cell)| {
            declared.geometry == cell.geometry
                && declared
                    .offsets
                    .iter()
                    .map(|&offset| offset as usize)
                    .eq(cell.offsets.iter().copied())
        })
}

fn offset_pan(position: f64, offset_degrees: f64, from: f64, to: f64) -> f64 {
    if offset_degrees == 0.0 || from == to {
        return position;
    }
    let (low, high) = (from.min(to), from.max(to));
    let mut degrees = from + position * (to - from) + offset_degrees;
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

fn read_bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let mut content = Vec::new();
    File::open(path)
        .with_context(|| format!("open fixture data {}", path.display()))?
        .take((limit + 1) as u64)
        .read_to_end(&mut content)
        .with_context(|| format!("read fixture data {}", path.display()))?;
    ensure!(
        content.len() <= limit,
        "fixture data exceeds input byte limit"
    );
    Ok(content)
}

#[cfg(test)]
mod tests {
    use super::{HomeOrientation, home_orientation, offset_pan, offset_tilt};

    fn yawed(degrees: f64) -> Vec<f64> {
        let (sin, cos) = degrees.to_radians().sin_cos();
        vec![cos, sin, 0.0, -sin, cos, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    }

    fn home(matrix: &[f64]) -> (f64, f64, bool) {
        let HomeOrientation {
            pan_offset_degrees,
            tilt_offset_degrees,
            reversed,
        } = home_orientation(matrix).unwrap();
        (pan_offset_degrees, tilt_offset_degrees, reversed)
    }

    #[test]
    fn matrix_yaw_is_the_pan_offset_in_one_canonical_turn() {
        for (yaw, expected) in [
            (0.0, 0.0),
            (180.0, 180.0),
            (-180.0, 180.0),
            (-90.0, -90.0),
            (270.0, -90.0),
            (540.0, 180.0),
        ] {
            let (pan, tilt, reversed) = home(&yawed(yaw));
            assert!((pan - expected).abs() < 1e-9, "{yaw} -> {pan}");
            assert!(tilt.abs() < 1e-9 && !reversed);
        }
        let exact = [-1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0, 5.0, 6.0, 7.0];
        assert_eq!(home(&exact), (180.0, 0.0, false));
        assert_eq!(home(&[]), (0.0, 0.0, false));
    }

    #[test]
    fn hanging_half_turn_and_standing_head_differ_only_in_direction() {
        let hanging = [-1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0];
        let standing = [1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0];
        assert_eq!(home(&hanging), (180.0, 0.0, false));
        assert_eq!(home(&standing), (180.0, 0.0, true));
    }

    #[test]
    fn matrix_pitch_is_the_tilt_offset_up_to_sixty_degrees() {
        let matrix = [
            0.773_337, 0.192_63, 0.604_023, -0.469_846, 0.813_798, 0.342_02, -0.425_669,
            -0.548_295, 0.719_846, 0.0, 0.0, 0.0,
        ];
        let (pan, tilt, reversed) = home(&matrix);
        assert!((pan - 30.0).abs() < 1e-3 && (tilt - 20.0).abs() < 1e-3 && !reversed);
        let (sin, cos) = 61.0_f64.to_radians().sin_cos();
        let steep = [1.0, 0.0, 0.0, 0.0, cos, sin, 0.0, -sin, cos, 0.0, 0.0, 0.0];
        let error = home_orientation(&steep).unwrap_err().to_string();
        assert!(error.contains("exceeds +/-60 degrees"), "{error}");
    }

    #[test]
    fn shear_mirror_and_partial_matrices_are_rejected() {
        let mut mirrored = yawed(0.0);
        mirrored[0] = -1.0;
        let mut sheared = yawed(0.0);
        sheared[3] = 1.0;
        for matrix in [mirrored, sheared, vec![1.0; 9], vec![f64::NAN; 12]] {
            assert!(home_orientation(&matrix).is_err(), "{matrix:?}");
        }
    }

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
        let wrapped = degrees(offset_pan(420.0 / 540.0, 180.0, 0.0, 540.0), 540.0);
        assert!((wrapped - 240.0).abs() < 1e-9);
        let below = degrees(offset_pan(0.0, -90.0, 0.0, 540.0), 540.0);
        assert!((below - 270.0).abs() < 1e-9);
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
