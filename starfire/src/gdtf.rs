//! Validate DMX modes Autolux compiled from GDTF once, then encode physical attributes.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, ensure};

use crate::model::{AttributeValue, DMX_CHANNELS, FixtureState, Rgb};
use crate::protocol::wire;

const MAX_CHANNEL_BYTES: usize = 4;
const MAX_FUNCTIONS: usize = 4096;
const MAX_ATTRIBUTE_VALUES: usize = 512;
const MAX_ATTRIBUTE_ID_BYTES: usize = 512;
const DMX_BYTE_BITS: usize = 8;
const DMX_BYTE_MASK: u32 = 255;
const CIE_LUMINANCE_SCALE: f64 = 100.0;
const SRGB_LINEAR_THRESHOLD: f64 = 0.003_130_8;
const SRGB_LINEAR_SCALE: f64 = 12.92;
const SRGB_GAMMA: f64 = 2.4;
const SRGB_SCALE: f64 = 1.055;
const SRGB_OFFSET: f64 = 0.055;
const BLACK: Rgb = Rgb {
    red: 0.0,
    green: 0.0,
    blue: 0.0,
};
const XYZ_TO_SRGB: [[f64; 3]; 3] = [
    [3.240_454_2, -1.537_138_5, -0.498_531_4],
    [-0.969_266, 1.876_010_8, 0.041_556],
    [0.055_643_4, -0.204_025_9, 1.057_225_2],
];

#[derive(Clone, Debug, PartialEq)]
struct ChannelSet {
    name: String,
    first: u32,
    last: u32,
    physical_from: f64,
    physical_to: f64,
    colour: Option<Rgb>,
}

#[derive(Clone, Debug, PartialEq)]
struct Function {
    path: String,
    attribute: String,
    qualified_attribute: String,
    first: u32,
    maximum_dmx_value: u32,
    physical_from: f64,
    physical_to: f64,
    snap: bool,
    sets: Vec<ChannelSet>,
    condition: Option<ModeCondition>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct ModeCondition {
    channel: usize,
    function: Option<usize>,
    first: u32,
    last: u32,
}

#[derive(Clone, Debug, PartialEq)]
struct Channel {
    /// The channel's geometry and its ancestors, root first.
    geometry_path: Vec<String>,
    offsets: Vec<usize>,
    default: u32,
    functions: Vec<Function>,
    functions_by_dmx_start: Vec<usize>,
    has_dimmer: bool,
    has_rgb: bool,
    has_white: bool,
    closes_when_dark: bool,
}

/// One channel function as seen by renderers that address attributes by family.
#[derive(Clone, Debug, PartialEq)]
pub struct FunctionInfo {
    pub channel: usize,
    pub attribute: String,
    pub key: String,
    pub physical_from: f64,
    pub physical_to: f64,
    pub rest: f64,
    pub slots: Vec<AttributeValue>,
    pub slot_colours: bool,
}

/// Resolved mode: offsets, physical mappings and dependencies are validated before playback.
#[derive(Clone, Debug, PartialEq)]
pub struct GdtfProfile {
    channels: Vec<Channel>,
    order: Vec<usize>,
    footprint: usize,
}

/// One channel-owning geometry of a mode, encoded from its own state into the shared footprint.
#[derive(Clone, Debug)]
pub struct GdtfCell {
    pub geometry: String,
    /// One-based DMX offsets of this geometry's channels within the fixture footprint, ascending.
    pub offsets: Vec<usize>,
    pub profile: GdtfProfile,
}

impl GdtfProfile {
    /// Validates a mode Autolux compiled from GDTF; Starfire reads no GDTF itself.
    pub fn from_wire(mode: &wire::DmxMode) -> Result<Self> {
        ensure!(
            !mode.channels.is_empty() && mode.channels.len() <= DMX_CHANNELS,
            "GDTF mode must contain 1..512 physical channels"
        );
        let mut occupied = BTreeSet::new();
        let mut channels = Vec::with_capacity(mode.channels.len());
        let mut function_count = 0;
        for channel in &mode.channels {
            ensure!(
                !channel.geometry_path.is_empty(),
                "GDTF channel has no geometry"
            );
            let width = channel.offsets.len();
            ensure!(
                (1..=MAX_CHANNEL_BYTES).contains(&width),
                "GDTF channel resolution must be 8, 16, 24 or 32 bits"
            );
            let maximum =
                (u64::from(u32::MAX) >> ((MAX_CHANNEL_BYTES - width) * DMX_BYTE_BITS)) as u32;
            let mut offsets = Vec::with_capacity(width);
            for &offset in &channel.offsets {
                let offset = offset as usize;
                ensure!(
                    (1..=DMX_CHANNELS).contains(&offset),
                    "GDTF channel offset outside 1..512"
                );
                ensure!(
                    occupied.insert(offset - 1),
                    "overlapping GDTF channel offsets"
                );
                offsets.push(offset - 1);
            }
            ensure!(
                channel.default <= maximum,
                "GDTF default exceeds its channel resolution"
            );
            ensure!(
                !channel.functions.is_empty(),
                "GDTF channel contains no functions"
            );
            let mut functions = Vec::with_capacity(channel.functions.len());
            for function in &channel.functions {
                functions.push(compile_wire_function(function, maximum, &mode.channels)?);
            }
            ensure!(
                functions
                    .iter()
                    .map(|function| &function.path)
                    .collect::<BTreeSet<_>>()
                    .len()
                    == functions.len(),
                "duplicate GDTF channel-function path"
            );
            for function in functions
                .iter()
                .filter(|function| is_brightness_attribute(&function.attribute))
            {
                ensure!(
                    functions.len() == 1
                        && function.sets.is_empty()
                        && function.condition.is_none()
                        && function.first == 0
                        && function.physical_from == 0.0
                        && function.physical_to > 0.0,
                    "GDTF brightness attribute {} requires one continuous zero-based function without ChannelSets or ModeMaster",
                    function.path
                );
            }
            function_count += functions.len();
            ensure!(
                function_count <= MAX_FUNCTIONS,
                "GDTF channel-function limit exceeded"
            );
            let mut functions_by_dmx_start: Vec<_> = (0..functions.len()).collect();
            functions_by_dmx_start.sort_by_key(|&index| functions[index].first);
            channels.push(Channel {
                geometry_path: channel.geometry_path.clone(),
                offsets,
                default: channel.default,
                functions,
                functions_by_dmx_start,
                has_dimmer: false,
                has_rgb: false,
                has_white: false,
                closes_when_dark: false,
            });
        }
        Self::assemble(channels)
    }

    /// The compiled mode's cells in order of their first DMX offset.
    pub fn cells_from_wire(mode: &wire::DmxMode) -> Result<Vec<GdtfCell>> {
        Self::from_wire(mode)?.into_cells()
    }

    fn into_cells(self) -> Result<Vec<GdtfCell>> {
        let owners: BTreeSet<&[String]> = self
            .channels
            .iter()
            .map(|channel| channel.geometry_path.as_slice())
            .collect();
        let leaves: Vec<&[String]> = owners
            .iter()
            .copied()
            .filter(|path| {
                owners
                    .iter()
                    .all(|other| other == path || !other.starts_with(path))
            })
            .collect();
        let cell_names: Vec<String> = self
            .channels
            .iter()
            .map(|channel| {
                let path = channel.geometry_path.as_slice();
                let below: Vec<_> = leaves
                    .iter()
                    .filter(|leaf| leaf.starts_with(path))
                    .collect();
                let owner = if below.len() == 1 { below[0] } else { path };
                owner.last().cloned().unwrap_or_default()
            })
            .collect();
        let mut geometries: Vec<(usize, String)> = Vec::new();
        for (channel, name) in self.channels.iter().zip(&cell_names) {
            let first = channel.offsets.iter().copied().min().unwrap_or_default();
            match geometries.iter_mut().find(|(_, geometry)| geometry == name) {
                Some(entry) => entry.0 = entry.0.min(first),
                None => geometries.push((first, name.clone())),
            }
        }
        geometries.sort();
        let offsets = |channels: &[Channel]| {
            let mut offsets: Vec<_> = channels
                .iter()
                .flat_map(|channel| channel.offsets.iter().map(|offset| offset + 1))
                .collect();
            offsets.sort_unstable();
            offsets
        };
        if geometries.len() == 1 {
            return Ok(vec![GdtfCell {
                geometry: geometries.remove(0).1,
                offsets: offsets(&self.channels),
                profile: self,
            }]);
        }
        geometries
            .into_iter()
            .map(|(_, geometry)| {
                let members: Vec<_> = (0..self.channels.len())
                    .filter(|&index| cell_names[index] == geometry)
                    .collect();
                let mut channels = Vec::with_capacity(members.len());
                for &index in &members {
                    let mut channel = self.channels[index].clone();
                    for function in &mut channel.functions {
                        if let Some(condition) = &mut function.condition {
                            condition.channel = members
                                .iter()
                                .position(|&member| member == condition.channel)
                                .context("GDTF ModeMaster across geometries is not supported")?;
                        }
                    }
                    channels.push(channel);
                }
                let order = dependency_order(&channels)?;
                Ok(GdtfCell {
                    geometry,
                    offsets: offsets(&channels),
                    profile: Self {
                        channels,
                        order,
                        footprint: self.footprint,
                    },
                })
            })
            .collect()
    }

    fn assemble(mut channels: Vec<Channel>) -> Result<Self> {
        // A dimmer controls its own geometry and descendants, never unrelated cells.
        for index in 0..channels.len() {
            let path = &channels[index].geometry_path;
            let geometry = path.last().context("GDTF channel has no geometry")?;
            let mut attributes = BTreeSet::new();
            let mut has_dimmer = false;
            for other in &channels {
                let other_geometry = other.geometry_path.last();
                if other_geometry.is_some_and(|name| path.contains(name)) {
                    has_dimmer |= other
                        .functions
                        .iter()
                        .any(|function| function.attribute == "Dimmer");
                }
                if other.geometry_path.contains(geometry) {
                    attributes.extend(
                        other
                            .functions
                            .iter()
                            .map(|function| function.attribute.clone()),
                    );
                }
            }
            let channel = &mut channels[index];
            channel.has_dimmer = has_dimmer;
            channel.has_rgb = ["ColorAdd_R", "ColorAdd_G", "ColorAdd_B"]
                .iter()
                .all(|attribute| attributes.contains(*attribute));
            channel.has_white = attributes.contains("ColorAdd_W");
        }
        let dark_slot = channels
            .iter()
            .flat_map(|channel| &channel.functions)
            .flat_map(|function| &function.sets)
            .any(|set| set.colour == Some(BLACK));
        for channel in &mut channels {
            channel.closes_when_dark = !dark_slot
                && !channel.has_dimmer
                && !channel.has_rgb
                && channel.functions.iter().any(is_gate_shutter);
        }
        let footprint = channels
            .iter()
            .flat_map(|channel| &channel.offsets)
            .max()
            .copied()
            .context("GDTF mode has no DMX offsets")?
            + 1;
        let order = dependency_order(&channels)?;
        Ok(Self {
            channels,
            order,
            footprint,
        })
    }

    pub fn footprint(&self) -> usize {
        self.footprint
    }

    pub fn movement_range(&self, attribute: &str) -> Option<(f64, f64)> {
        self.channels
            .iter()
            .flat_map(|channel| &channel.functions)
            .find(|function| function.attribute == attribute && function.sets.is_empty())
            .map(|function| (function.physical_from, function.physical_to))
    }

    pub fn validate_attributes(&self, attributes: &BTreeMap<String, AttributeValue>) -> Result<()> {
        ensure!(
            attributes.len() <= MAX_ATTRIBUTE_VALUES,
            "too many GDTF attributes"
        );
        for (key, value) in attributes {
            ensure!(
                !key.is_empty() && key.len() <= MAX_ATTRIBUTE_ID_BYTES,
                "invalid GDTF attribute identifier"
            );
            let matches = self.matching_functions(key);
            ensure!(!matches.is_empty(), "unknown GDTF attribute {key}");
            let channels: BTreeSet<_> = matches.iter().map(|(channel, _)| *channel).collect();
            ensure!(
                channels.len() == 1,
                "ambiguous GDTF attribute {key}; specify geometry or full function path"
            );
            ensure!(
                matches
                    .iter()
                    .any(|(_, function)| function.encode(value).is_ok()),
                "invalid physical value or preset for GDTF attribute {key}"
            );
        }
        Ok(())
    }

    pub fn interpolate_attribute(
        &self,
        key: &str,
        from: &AttributeValue,
        to: &AttributeValue,
        amount: f64,
    ) -> AttributeValue {
        if let (AttributeValue::Number(first), AttributeValue::Number(last)) = (from, to) {
            let functions = self.matching_functions(key);
            if functions.len() == 1 {
                let function = functions[0].1;
                if !function.snap && function.sets.is_empty() {
                    return AttributeValue::Number(first + (last - first) * amount);
                }
            }
        }
        if amount < 1.0 {
            from.clone()
        } else {
            to.clone()
        }
    }

    pub fn encode(&self, state: &FixtureState, output: &mut [u8]) -> Result<()> {
        self.encode_calibrated(state, output, |position| position, |position| position)
    }

    pub fn encode_calibrated(
        &self,
        state: &FixtureState,
        output: &mut [u8],
        pan: impl Fn(f64) -> f64,
        tilt: impl Fn(f64) -> f64,
    ) -> Result<()> {
        ensure!(
            output.len() == self.footprint,
            "GDTF output footprint mismatch"
        );
        self.validate_attributes(&state.attributes)?;
        let mut values: Vec<u32> = self
            .channels
            .iter()
            .map(|channel| channel.default)
            .collect();
        let mut active_function_ends = vec![Vec::new(); self.channels.len()];
        let mut resolved_attributes = BTreeSet::new();
        for &index in &self.order {
            let channel = &self.channels[index];
            active_function_ends[index] =
                channel.active_function_ends(&values, &active_function_ends, &self.channels);
            let dimmer = channel.has_dimmer;
            let mut requested = None;
            for (function_index, function) in channel.functions.iter().enumerate() {
                let Some(last) = active_function_ends[index][function_index] else {
                    continue;
                };
                if let Some(value) = semantic_value(function, last, state, channel) {
                    ensure!(
                        requested.is_none(),
                        "ambiguous semantic GDTF attribute on {}",
                        function.path
                    );
                    requested = Some(value);
                }
            }
            let mut explicit = None;
            for (key, value) in &state.attributes {
                for (function_index, function) in channel.functions.iter().enumerate() {
                    let Some(last) = active_function_ends[index][function_index] else {
                        continue;
                    };
                    if function.matches(key) {
                        if state.output_blackout
                            && !matches!(function.attribute.as_str(), "Pan" | "Tilt")
                        {
                            continue;
                        }
                        if let Ok(mut encoded) = function.encode_in_range(value, last) {
                            let span = f64::from(last - function.first);
                            let position = if span == 0.0 {
                                0.0
                            } else {
                                f64::from(encoded - function.first) / span
                            };
                            let calibrated = match function.attribute.as_str() {
                                "Pan" => Some(pan(position)),
                                "Tilt" => Some(tilt(position)),
                                _ => None,
                            };
                            if let Some(position) = calibrated {
                                encoded =
                                    (f64::from(function.first) + span * position).round() as u32;
                            }
                            if function.attribute == "Dimmer"
                                || (!dimmer && function.attribute.starts_with("ColorAdd_"))
                            {
                                encoded = (f64::from(function.first)
                                    + f64::from(encoded - function.first)
                                        * state.attribute_brightness)
                                    .round() as u32;
                            }
                            if !dimmer && function.attribute.starts_with("ColorSub_") {
                                encoded = (f64::from(last)
                                    - f64::from(last - encoded) * state.attribute_brightness)
                                    .round() as u32;
                            }
                            ensure!(
                                explicit.is_none(),
                                "conflicting GDTF values on {}",
                                function.path
                            );
                            explicit = Some(encoded);
                            resolved_attributes.insert(key);
                        }
                    }
                }
            }
            values[index] = explicit.or(requested).unwrap_or(channel.default);
        }
        // A supplied value must resolve in the final mode, not silently disappear behind ModeMaster.
        for key in state.attributes.keys().filter(|_| !state.output_blackout) {
            ensure!(
                resolved_attributes.contains(key),
                "GDTF attribute {key} value is inactive in the selected mode"
            );
        }
        for (channel, value) in self.channels.iter().zip(values) {
            for (index, &offset) in channel.offsets.iter().enumerate() {
                let shift = (channel.offsets.len() - index - 1) * DMX_BYTE_BITS;
                output[offset] = ((value >> shift) & DMX_BYTE_MASK) as u8;
            }
        }
        Ok(())
    }

    /// Unconditional functions with their physical rest value and addressable channel sets.
    pub fn functions(&self) -> Vec<FunctionInfo> {
        let mut infos = Vec::new();
        for (index, channel) in self.channels.iter().enumerate() {
            for (position, &function_index) in channel.functions_by_dmx_start.iter().enumerate() {
                let function = &channel.functions[function_index];
                if function.condition.is_some() {
                    continue;
                }
                let last = channel.functions_by_dmx_start[position + 1..]
                    .iter()
                    .map(|&next| channel.functions[next].first)
                    .find(|&first| first > function.first)
                    .map_or(function.maximum_dmx_value, |first| first - 1);
                let rest = if (function.first..=last).contains(&channel.default) {
                    let span = f64::from(last - function.first);
                    let amount = if span == 0.0 {
                        0.0
                    } else {
                        f64::from(channel.default - function.first) / span
                    };
                    function.physical_from
                        + (function.physical_to - function.physical_from) * amount
                } else {
                    function.physical_from
                };
                let named = |name: &str| {
                    !name.is_empty()
                        && function.sets.iter().filter(|set| set.name == name).count() == 1
                };
                let slots = function
                    .sets
                    .iter()
                    .filter(|set| set.first <= last)
                    .filter_map(|set| {
                        if named(&set.name) {
                            return Some(AttributeValue::Preset(set.name.clone()));
                        }
                        let value = AttributeValue::Number(set.physical_from);
                        function
                            .encode_in_range(&value, last)
                            .is_ok()
                            .then_some(value)
                    })
                    .collect();
                let unique = |key: &str| self.matching_functions(key).len() == 1;
                let key = [&function.attribute, &function.qualified_attribute]
                    .into_iter()
                    .find(|key| unique(key))
                    .unwrap_or(&function.path)
                    .clone();
                infos.push(FunctionInfo {
                    channel: index,
                    attribute: function.attribute.clone(),
                    key,
                    physical_from: function.physical_from,
                    physical_to: function.physical_to,
                    rest,
                    slots,
                    slot_colours: function.sets.iter().any(|set| set.colour.is_some()),
                });
            }
        }
        infos
    }

    pub fn has_dark_wheel_slot(&self) -> bool {
        self.channels
            .iter()
            .flat_map(|channel| &channel.functions)
            .flat_map(|function| &function.sets)
            .any(|set| set.colour == Some(BLACK))
    }

    /// A later layer's key replaces every earlier key that addresses one of its channels.
    pub fn overlay_attributes(
        &self,
        target: &mut BTreeMap<String, AttributeValue>,
        layer: &BTreeMap<String, AttributeValue>,
    ) {
        for (key, value) in layer {
            let channels = self.channels_matching(key);
            target.retain(|existing, _| {
                existing == key || self.channels_matching(existing).is_disjoint(&channels)
            });
            target.insert(key.clone(), value.clone());
        }
    }

    pub fn shares_channel(&self, key: &str, attributes: &BTreeMap<String, AttributeValue>) -> bool {
        let channels = self.channels_matching(key);
        attributes
            .keys()
            .any(|existing| !self.channels_matching(existing).is_disjoint(&channels))
    }

    pub fn channels_matching(&self, key: &str) -> BTreeSet<usize> {
        self.matching_functions(key)
            .into_iter()
            .map(|(channel, _)| channel)
            .collect()
    }

    fn matching_functions(&self, key: &str) -> Vec<(usize, &Function)> {
        self.channels
            .iter()
            .enumerate()
            .flat_map(|(index, channel)| {
                channel
                    .functions
                    .iter()
                    .filter(move |function| function.matches(key))
                    .map(move |function| (index, function))
            })
            .collect()
    }
}

impl Channel {
    fn active_function_ends(
        &self,
        values: &[u32],
        function_ends: &[Vec<Option<u32>>],
        channels: &[Channel],
    ) -> Vec<Option<u32>> {
        let mut ends = vec![None; self.functions.len()];
        let mut next_start = u64::from(self.functions[0].maximum_dmx_value) + 1;
        let mut cursor = self.functions_by_dmx_start.len();
        while cursor > 0 {
            let first = self.functions[self.functions_by_dmx_start[cursor - 1]].first;
            let mut any_active = false;
            while cursor > 0 {
                let index = self.functions_by_dmx_start[cursor - 1];
                let function = &self.functions[index];
                if function.first != first {
                    break;
                }
                if function.active(values, function_ends, channels) {
                    ends[index] = Some((next_start - 1) as u32);
                    any_active = true;
                }
                cursor -= 1;
            }
            if any_active {
                next_start = u64::from(first);
            }
        }
        ends
    }
}

impl Function {
    fn matches(&self, key: &str) -> bool {
        key == self.attribute || key == self.qualified_attribute || key == self.path
    }
    fn active(
        &self,
        values: &[u32],
        function_ends: &[Vec<Option<u32>>],
        channels: &[Channel],
    ) -> bool {
        self.condition.is_none_or(|condition| {
            let value = values[condition.channel];
            (condition.first..=condition.last).contains(&value)
                && condition.function.is_none_or(|index| {
                    function_ends[condition.channel][index].is_some_and(|last| {
                        (channels[condition.channel].functions[index].first..=last).contains(&value)
                    })
                })
        })
    }

    fn encode(&self, value: &AttributeValue) -> Result<u32> {
        self.encode_in_range(value, self.maximum_dmx_value)
    }

    fn encode_in_range(&self, value: &AttributeValue, last: u32) -> Result<u32> {
        match value {
            AttributeValue::Preset(name) => self
                .sets
                .iter()
                .find(|set| &set.name == name && set.first <= last)
                .map(|set| set.first)
                .with_context(|| format!("unknown preset {name}")),
            AttributeValue::Number(value) => {
                ensure!(value.is_finite(), "GDTF physical value must be finite");
                if self.sets.is_empty() {
                    physical_to_dmx(
                        *value,
                        self.physical_from,
                        self.physical_to,
                        self.first,
                        last,
                    )
                } else {
                    let candidates: Vec<_> = self
                        .sets
                        .iter()
                        .filter(|set| set.first <= last)
                        .filter_map(|set| {
                            physical_to_dmx(
                                *value,
                                set.physical_from,
                                set.physical_to,
                                set.first,
                                set.last.min(last),
                            )
                            .ok()
                        })
                        .collect();
                    ensure!(
                        candidates.len() == 1,
                        "physical value is ambiguous or outside GDTF channel sets; select a preset"
                    );
                    Ok(candidates[0])
                }
            }
        }
    }

    fn normalized(&self, value: f64, last: u32) -> Option<u32> {
        if self.sets.is_empty() {
            Some((f64::from(self.first) + f64::from(last - self.first) * value).round() as u32)
        } else {
            None
        }
    }
}

fn semantic_value(
    function: &Function,
    last: u32,
    state: &FixtureState,
    channel: &Channel,
) -> Option<u32> {
    let rgb = channel.has_rgb;
    let gain = if channel.has_dimmer {
        1.0
    } else {
        state.intensity
    };
    let white = if channel.has_white && rgb {
        state
            .colour
            .red
            .min(state.colour.green)
            .min(state.colour.blue)
    } else {
        0.0
    };
    let wheel = !function.sets.is_empty() && function.sets.iter().any(|set| set.colour.is_some());
    if wheel && function.attribute.starts_with("Color") {
        let dark = state.intensity <= 0.0 || colour_peak(state.colour) <= 0.0;
        let requested = if rgb {
            Rgb::default()
        } else if dark && !channel.has_dimmer {
            BLACK
        } else {
            normalize(state.colour)
        };
        return function
            .sets
            .iter()
            .rev()
            .filter(|set| set.first <= last)
            .filter_map(|set| set.colour.map(|colour| (set, colour)))
            .min_by(|(_, left), (_, right)| {
                colour_distance(*left, requested).total_cmp(&colour_distance(*right, requested))
            })
            .map(|(set, _)| set.first);
    }
    if channel.closes_when_dark && is_gate_shutter(function) {
        return (state.intensity <= 0.0).then_some(function.first);
    }
    let value = match function.attribute.as_str() {
        "Dimmer" => {
            state.intensity
                * if rgb || channel.has_white {
                    1.0
                } else {
                    colour_peak(state.colour)
                }
        }
        "ColorAdd_R" => (state.colour.red - white) * gain,
        "ColorAdd_G" => (state.colour.green - white) * gain,
        "ColorAdd_B" => (state.colour.blue - white) * gain,
        "ColorAdd_W" => {
            (if rgb {
                white
            } else {
                colour_peak(state.colour)
            }) * gain
        }
        "ColorSub_C" => 1.0 - state.colour.red * gain,
        "ColorSub_M" => 1.0 - state.colour.green * gain,
        "ColorSub_Y" => 1.0 - state.colour.blue * gain,
        // Extra emitters require explicit physical attributes; their defaults
        // must never turn them on during idle, blackout or script failure.
        attribute if attribute.starts_with("ColorAdd_") => 0.0,
        attribute if attribute.starts_with("ColorSub_") => 1.0,
        "Pan" => state.pan,
        "Tilt" => state.tilt,
        _ => return None,
    };
    function.normalized(value, last)
}

fn compile_wire_function(
    function: &wire::ChannelFunction,
    maximum: u32,
    channels: &[wire::DmxChannel],
) -> Result<Function> {
    ensure!(
        function.physical_from.is_finite() && function.physical_to.is_finite(),
        "GDTF physical range must be finite"
    );
    ensure!(
        !function.attribute.is_empty() && function.attribute.len() <= MAX_ATTRIBUTE_ID_BYTES,
        "GDTF function attribute must be a single name"
    );
    ensure!(
        function.dmx_from <= maximum,
        "GDTF function start exceeds its channel resolution"
    );
    let mut sets = Vec::with_capacity(function.sets.len());
    for (index, set) in function.sets.iter().enumerate() {
        ensure!(
            set.dmx_from >= function.dmx_from
                && set.dmx_from <= set.dmx_to
                && set.dmx_to <= maximum,
            "GDTF channel set exceeds function range"
        );
        if let Some(next) = function.sets.get(index + 1) {
            ensure!(
                next.dmx_from > set.dmx_from && set.dmx_to == next.dmx_from - 1,
                "GDTF channel sets must have increasing ranges"
            );
        } else {
            ensure!(
                set.dmx_to == maximum,
                "GDTF channel sets must have increasing ranges"
            );
        }
        ensure!(
            set.physical_from.is_finite() && set.physical_to.is_finite(),
            "nonfinite GDTF channel-set range"
        );
        sets.push(ChannelSet {
            name: set.name.clone(),
            first: set.dmx_from,
            last: set.dmx_to,
            physical_from: set.physical_from,
            physical_to: set.physical_to,
            colour: set.wheel_slot_colour.as_ref().map(cie_to_rgb).transpose()?,
        });
    }
    let condition = function
        .mode_master
        .as_ref()
        .map(|master| -> Result<_> {
            let channel = master.channel as usize;
            let linked = channels
                .get(channel)
                .context("GDTF ModeMaster references a missing channel/function")?;
            let width = linked.offsets.len();
            ensure!(
                (1..=MAX_CHANNEL_BYTES).contains(&width),
                "virtual ModeMaster is unsupported"
            );
            let limit =
                (u64::from(u32::MAX) >> ((MAX_CHANNEL_BYTES - width) * DMX_BYTE_BITS)) as u32;
            ensure!(
                master.dmx_from <= master.dmx_to && master.dmx_to <= limit,
                "invalid GDTF ModeMaster interval"
            );
            let function = master
                .function
                .map(|index| -> Result<_> {
                    ensure!(
                        (index as usize) < linked.functions.len(),
                        "GDTF ModeMaster function is missing"
                    );
                    Ok(index as usize)
                })
                .transpose()?;
            Ok(ModeCondition {
                channel,
                function,
                first: master.dmx_from,
                last: master.dmx_to,
            })
        })
        .transpose()?;
    Ok(Function {
        path: function.path.clone(),
        attribute: function.attribute.clone(),
        qualified_attribute: function.qualified_attribute.clone(),
        first: function.dmx_from,
        maximum_dmx_value: maximum,
        physical_from: function.physical_from,
        physical_to: function.physical_to,
        snap: function.snap,
        sets,
        condition,
    })
}

fn dependency_order(channels: &[Channel]) -> Result<Vec<usize>> {
    let mut order = Vec::new();
    let mut remaining: BTreeSet<_> = (0..channels.len()).collect();
    while !remaining.is_empty() {
        let ready = remaining.iter().copied().find(|&index| {
            channels[index]
                .functions
                .iter()
                .filter_map(|function| function.condition)
                .all(|condition| !remaining.contains(&condition.channel))
        });
        let index = ready.context("cyclic GDTF ModeMaster dependencies")?;
        remaining.remove(&index);
        order.push(index);
    }
    Ok(order)
}

pub fn numbered_attribute(attribute: &str, prefix: &str) -> bool {
    attribute
        .strip_prefix(prefix)
        .is_some_and(|index| !index.is_empty() && index.bytes().all(|byte| byte.is_ascii_digit()))
}

fn is_gate_shutter(function: &Function) -> bool {
    numbered_attribute(&function.attribute, "Shutter")
        && function.sets.is_empty()
        && function.first == 0
}

fn is_brightness_attribute(attribute: &str) -> bool {
    attribute == "Dimmer"
        || attribute.starts_with("ColorAdd_")
        || attribute.starts_with("ColorSub_")
}

fn physical_to_dmx(value: f64, from: f64, to: f64, first: u32, last: u32) -> Result<u32> {
    ensure!(
        (from.min(to)..=from.max(to)).contains(&value),
        "GDTF physical value is outside function range"
    );
    let amount = if from == to {
        0.0
    } else {
        (value - from) / (to - from)
    };
    Ok((f64::from(first) + f64::from(last - first) * amount).round() as u32)
}

fn cie_to_rgb(colour: &wire::CieColor) -> Result<Rgb> {
    ensure!(
        [colour.x, colour.y, colour.luminance]
            .iter()
            .all(|value| value.is_finite())
            && colour.y > 0.0
            && colour.luminance >= 0.0,
        "invalid GDTF CIE wheel colour"
    );
    let luminance = colour.luminance / CIE_LUMINANCE_SCALE;
    let xyz = [
        colour.x * luminance / colour.y,
        luminance,
        (1.0 - colour.x - colour.y) * luminance / colour.y,
    ];
    let rgb = XYZ_TO_SRGB.map(|row| {
        let linear = row
            .iter()
            .zip(xyz)
            .map(|(weight, value)| weight * value)
            .sum::<f64>()
            .clamp(0.0, 1.0);
        if linear <= SRGB_LINEAR_THRESHOLD {
            linear * SRGB_LINEAR_SCALE
        } else {
            SRGB_SCALE * linear.powf(1.0 / SRGB_GAMMA) - SRGB_OFFSET
        }
    });
    let colour = Rgb {
        red: rgb[0],
        green: rgb[1],
        blue: rgb[2],
    };
    Ok(if colour_peak(colour) > 0.0 {
        normalize(colour)
    } else {
        BLACK
    })
}

fn colour_peak(colour: Rgb) -> f64 {
    colour.red.max(colour.green).max(colour.blue)
}
fn normalize(colour: Rgb) -> Rgb {
    let peak = colour_peak(colour);
    if peak > 0.0 {
        Rgb {
            red: colour.red / peak,
            green: colour.green / peak,
            blue: colour.blue / peak,
        }
    } else {
        Rgb::default()
    }
}
fn colour_distance(left: Rgb, right: Rgb) -> f64 {
    (left.red - right.red).powi(2)
        + (left.green - right.green).powi(2)
        + (left.blue - right.blue).powi(2)
}
