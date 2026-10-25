//! Compile standard GDTF descriptions once, then encode physical attributes without XML work.

use std::collections::{BTreeMap, BTreeSet};
use std::str::FromStr;

use anyhow::{Context, Result, bail, ensure};
use gdtf::Description;
use gdtf::dmx_mode::{DmxBreak, DmxChannel, DmxMode, ModeMaster};
use gdtf::fixture_type::FixtureType;
use gdtf::geometry::{AnyGeometry, Geometry};
use gdtf::values::{ColorCie, DmxValue};
use gdtf::wheel::WheelSlotOptic;
use quick_xml::{Reader, Writer, events::Event};

use crate::model::{AttributeValue, DMX_CHANNELS, FixtureState, Rgb};

pub const MAX_GDTF_XML_BYTES: usize = 1024 * 1024;
const MAX_CHANNEL_BYTES: usize = 4;
const MAX_FUNCTIONS: usize = 4096;
const MAX_ATTRIBUTE_VALUES: usize = 512;
const MAX_ATTRIBUTE_ID_BYTES: usize = 512;
const MAX_XML_DEPTH: usize = 128;
const DMX_BYTE_BITS: usize = 8;
const DMX_BYTE_MASK: u32 = 255;
const CIE_LUMINANCE_SCALE: f64 = 100.0;
const SRGB_LINEAR_THRESHOLD: f64 = 0.003_130_8;
const SRGB_LINEAR_SCALE: f64 = 12.92;
const SRGB_GAMMA: f64 = 2.4;
const SRGB_SCALE: f64 = 1.055;
const SRGB_OFFSET: f64 = 0.055;
const XYZ_TO_SRGB: [[f64; 3]; 3] = [
    [3.240_454_2, -1.537_138_5, -0.498_531_4],
    [-0.969_266, 1.876_010_8, 0.041_556],
    [0.055_643_4, -0.204_025_9, 1.057_225_2],
];

#[derive(Clone, Debug)]
struct ChannelSet {
    name: String,
    first: u32,
    last: u32,
    physical_from: f64,
    physical_to: f64,
    colour: Option<Rgb>,
}

#[derive(Clone, Debug)]
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

#[derive(Clone, Copy, Debug)]
struct ModeCondition {
    channel: usize,
    function: Option<usize>,
    first: u32,
    last: u32,
}

#[derive(Clone, Debug)]
struct Channel {
    offsets: Vec<usize>,
    default: u32,
    functions: Vec<Function>,
    functions_by_dmx_start: Vec<usize>,
    has_dimmer: bool,
    has_rgb: bool,
    has_white: bool,
}

/// Resolved mode: offsets, physical mappings and dependencies are validated before playback.
#[derive(Clone, Debug)]
pub struct GdtfProfile {
    channels: Vec<Channel>,
    order: Vec<usize>,
    footprint: usize,
}

impl GdtfProfile {
    pub fn from_xml(xml: &str, mode_name: &str) -> Result<Self> {
        ensure!(
            xml.len() <= MAX_GDTF_XML_BYTES,
            "GDTF description exceeds size limit"
        );
        ensure!(
            !xml.contains("<!DOCTYPE") && !xml.contains("<!ENTITY"),
            "GDTF DTD/entities are unsupported"
        );
        let description = Description::from_str(&materialize_parser_defaults(xml)?)
            .context("parse GDTF description")?;
        ensure!(
            description.data_version.is_supported(),
            "unsupported GDTF version"
        );
        ensure!(
            description.fixture_types.len() == 1,
            "GDTF definition must contain exactly one FixtureType"
        );
        let fixture = &description.fixture_types[0];
        let mode = fixture
            .dmx_modes
            .iter()
            .find(|mode| mode.name.as_deref() == Some(mode_name))
            .with_context(|| format!("unknown GDTF DMX mode {mode_name}"))?;
        ensure!(
            mode.relations.is_empty(),
            "GDTF channel relations are not supported"
        );
        ensure!(
            mode.ft_macros.is_empty(),
            "GDTF fixture macros are not supported"
        );
        ensure!(
            !mode.dmx_channels.is_empty() && mode.dmx_channels.len() <= DMX_CHANNELS,
            "GDTF mode must contain 1..512 physical channels"
        );
        let geometry = mode
            .geometry(fixture)
            .context("GDTF mode references missing geometry")?;
        let mut geometries = BTreeMap::new();
        collect_geometries(geometry, &[], &mut geometries)?;
        ensure!(
            mode.dmx_channels.iter().all(|channel| channel
                .logical_channels
                .iter()
                .all(|logical| logical.attribute.len() == 1)),
            "GDTF logical-channel attribute must be a single name"
        );
        let names: BTreeMap<_, _> = mode
            .dmx_channels
            .iter()
            .enumerate()
            .map(|(index, channel)| (channel.name().to_string(), index))
            .collect();
        ensure!(
            names.len() == mode.dmx_channels.len(),
            "duplicate GDTF channel name"
        );
        let mut occupied = BTreeSet::new();
        let mut channels = Vec::new();
        let mut function_count = 0;
        for channel in &mode.dmx_channels {
            ensure!(
                channel.dmx_break == DmxBreak::Value(1),
                "GDTF multiple/overwritten DMX breaks are not supported"
            );
            ensure!(
                geometries.contains_key(&channel.geometry.to_string()),
                "GDTF channel references geometry outside its selected mode"
            );
            let offsets = channel
                .offset
                .as_ref()
                .context("GDTF virtual channels are not supported")?;
            ensure!(
                (1..=MAX_CHANNEL_BYTES).contains(&offsets.len()),
                "GDTF channel resolution must be 8, 16, 24 or 32 bits"
            );
            let mut resolved_offsets = Vec::new();
            for &offset in offsets {
                ensure!(
                    (1..=DMX_CHANNELS as i32).contains(&offset),
                    "GDTF channel offset outside 1..512"
                );
                ensure!(
                    occupied.insert(offset as usize - 1),
                    "overlapping GDTF channel offsets"
                );
                resolved_offsets.push(offset as usize - 1);
            }
            let initial = channel
                .initial_function()
                .context("GDTF initial function is missing")?
                .1;
            let default = dmx_value(initial.default, offsets.len())?;
            let functions = compile_functions(fixture, mode, channel, &names)?;
            let mut functions_by_dmx_start: Vec<_> = (0..functions.len()).collect();
            functions_by_dmx_start.sort_by_key(|&index| functions[index].first);
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
            channels.push(Channel {
                offsets: resolved_offsets,
                default,
                functions,
                functions_by_dmx_start,
                has_dimmer: false,
                has_rgb: false,
                has_white: false,
            });
        }
        // A dimmer controls its own geometry and descendants, never unrelated cells.
        for (index, channel) in mode.dmx_channels.iter().enumerate() {
            let geometry = channel.geometry.to_string();
            let ancestors = &geometries[&geometry];
            let mut attributes = BTreeSet::new();
            let mut has_dimmer = false;
            for (other, compiled) in mode.dmx_channels.iter().zip(&channels) {
                let other_geometry = other.geometry.to_string();
                if ancestors.contains(&other_geometry) {
                    has_dimmer |= compiled
                        .functions
                        .iter()
                        .any(|function| function.attribute == "Dimmer");
                }
                if geometries[&other_geometry].contains(&geometry) {
                    attributes.extend(
                        compiled
                            .functions
                            .iter()
                            .map(|function| function.attribute.clone()),
                    );
                }
            }
            channels[index].has_dimmer = has_dimmer;
            channels[index].has_rgb = ["ColorAdd_R", "ColorAdd_G", "ColorAdd_B"]
                .iter()
                .all(|attribute| attributes.contains(*attribute));
            channels[index].has_white = attributes.contains("ColorAdd_W");
        }
        let footprint = occupied
            .last()
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
        output.fill(0);
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
            let rgb = channel.has_rgb;
            let white = if channel.has_white && rgb {
                state
                    .colour
                    .red
                    .min(state.colour.green)
                    .min(state.colour.blue)
            } else {
                0.0
            };
            let mut requested = None;
            for (function_index, function) in channel.functions.iter().enumerate() {
                let Some(last) = active_function_ends[index][function_index] else {
                    continue;
                };
                if let Some(value) = semantic_value(function, last, state, dimmer, rgb, white) {
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
    dimmer: bool,
    rgb: bool,
    white: f64,
) -> Option<u32> {
    let gain = if dimmer { 1.0 } else { state.intensity };
    let wheel = !function.sets.is_empty() && function.sets.iter().any(|set| set.colour.is_some());
    if wheel && function.attribute.starts_with("Color") {
        let requested = if rgb {
            Rgb::default()
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
    let value = match function.attribute.as_str() {
        "Dimmer" => state.intensity * if rgb { 1.0 } else { colour_peak(state.colour) },
        "ColorAdd_R" => (state.colour.red - white) * gain,
        "ColorAdd_G" => (state.colour.green - white) * gain,
        "ColorAdd_B" => (state.colour.blue - white) * gain,
        "ColorAdd_W" => white * gain,
        "ColorSub_C" => 1.0 - state.colour.red * gain,
        "ColorSub_M" => 1.0 - state.colour.green * gain,
        "ColorSub_Y" => 1.0 - state.colour.blue * gain,
        "Pan" => state.pan,
        "Tilt" => state.tilt,
        _ => return None,
    };
    function.normalized(value, last)
}

fn compile_functions(
    fixture: &FixtureType,
    mode: &DmxMode,
    channel: &DmxChannel,
    names: &BTreeMap<String, usize>,
) -> Result<Vec<Function>> {
    let bytes = channel
        .offset
        .as_ref()
        .context("missing channel offsets")?
        .len() as u8;
    let maximum =
        (u64::from(u32::MAX) >> ((MAX_CHANNEL_BYTES - usize::from(bytes)) * DMX_BYTE_BITS)) as u32;
    let mut functions = Vec::new();
    for logical in &channel.logical_channels {
        ensure!(
            logical.dmx_change_time_limit == 0.0 && logical.mib_fade == 0.0,
            "GDTF timed logical-channel constraints are unsupported"
        );
        ensure!(
            !logical.channel_functions.is_empty(),
            "GDTF logical channel has no functions"
        );
        for (index, function) in logical.channel_functions.iter().enumerate() {
            ensure!(
                function.dmx_profile.is_none() && function.sub_channel_sets.is_empty(),
                "GDTF DMX profiles and sub-channel sets are unsupported"
            );
            ensure!(
                function.physical_from.is_finite() && function.physical_to.is_finite(),
                "GDTF physical range must be finite"
            );
            ensure!(
                function.attribute.len() == 1,
                "GDTF function attribute must be a single name"
            );
            let attribute = function.attribute[0].to_string();
            ensure!(
                attribute == "NoFeature" || function.attribute(fixture).is_some(),
                "missing GDTF attribute definition {attribute}"
            );
            let first = dmx_value(function.dmx_from, usize::from(bytes))?;
            let function_name = function
                .name
                .as_ref()
                .map(ToString::to_string)
                .unwrap_or_else(|| format!("{attribute}{}", index + 1));
            let path = format!("{}.{}.{}", channel.name(), logical.name(), function_name);
            let condition = function
                .mode_master
                .as_ref()
                .map(|condition| -> Result<_> {
                    let linked = condition
                        .mode_master(mode)
                        .context("GDTF ModeMaster references a missing channel/function")?;
                    let master_name = condition
                        .node
                        .first()
                        .context("empty GDTF ModeMaster")?
                        .to_string();
                    let master = *names
                        .get(&master_name)
                        .context("missing GDTF ModeMaster channel")?;
                    let width = mode.dmx_channels[master]
                        .offset
                        .as_ref()
                        .context("virtual ModeMaster is unsupported")?
                        .len() as u8;
                    let from = dmx_value(condition.from, usize::from(width))?;
                    let to = dmx_value(condition.to, usize::from(width))?;
                    ensure!(from <= to, "invalid GDTF ModeMaster interval");
                    let function = match linked {
                        ModeMaster::DmxChannel(_) => None,
                        ModeMaster::ChannelFunction(channel, _, function) => Some(
                            channel
                                .logical_channels
                                .iter()
                                .flat_map(|logical| &logical.channel_functions)
                                .position(|candidate| std::ptr::eq(candidate, function))
                                .context("GDTF ModeMaster function is missing")?,
                        ),
                    };
                    Ok(ModeCondition {
                        channel: master,
                        function,
                        first: from,
                        last: to,
                    })
                })
                .transpose()?;
            let mut sets = Vec::new();
            let mut ordered_sets = function
                .channel_sets
                .iter()
                .map(|set| Ok((dmx_value(set.dmx_from, usize::from(bytes))?, set)))
                .collect::<Result<Vec<_>>>()?;
            ordered_sets.sort_by_key(|(first, _)| *first);
            for (set_index, &(set_first, set)) in ordered_sets.iter().enumerate() {
                let set_last = if let Some(&(next, _)) = ordered_sets.get(set_index + 1) {
                    ensure!(
                        next > set_first,
                        "GDTF channel sets must have increasing ranges"
                    );
                    next - 1
                } else {
                    maximum
                };
                ensure!(
                    set_first >= first && set_first <= set_last,
                    "GDTF channel set exceeds function range"
                );
                let physical_from = set.physical_from(function);
                let physical_to = set.physical_to(function);
                ensure!(
                    physical_from.is_finite() && physical_to.is_finite(),
                    "nonfinite GDTF channel-set range"
                );
                let colour = if set.wheel_slot_index.is_some() {
                    let wheel = function
                        .wheel(fixture)
                        .context("GDTF wheel link is missing")?;
                    let slot = set
                        .wheel_slot(wheel)
                        .context("GDTF wheel slot is missing")?;
                    match slot.optic {
                        WheelSlotOptic::Color(colour) => Some(cie_to_rgb(colour)?),
                        WheelSlotOptic::Filter(_) => {
                            bail!("GDTF spectral wheel filters are unsupported")
                        }
                    }
                } else {
                    None
                };
                sets.push(ChannelSet {
                    name: set
                        .name
                        .as_ref()
                        .map(ToString::to_string)
                        .unwrap_or_default(),
                    first: set_first,
                    last: set_last,
                    physical_from,
                    physical_to,
                    colour,
                });
            }
            functions.push(Function {
                path,
                attribute: attribute.clone(),
                qualified_attribute: format!("{}.{attribute}", channel.geometry),
                first,
                maximum_dmx_value: maximum,
                physical_from: function.physical_from,
                physical_to: function.physical_to,
                snap: logical.snap,
                sets,
                condition,
            });
        }
    }
    ensure!(!functions.is_empty(), "GDTF channel contains no functions");
    ensure!(
        functions
            .iter()
            .map(|function| &function.path)
            .collect::<BTreeSet<_>>()
            .len()
            == functions.len(),
        "duplicate GDTF channel-function path"
    );
    Ok(functions)
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

fn is_brightness_attribute(attribute: &str) -> bool {
    attribute == "Dimmer"
        || attribute.starts_with("ColorAdd_")
        || attribute.starts_with("ColorSub_")
}

fn collect_geometries(
    geometry: &Geometry,
    ancestors: &[String],
    names: &mut BTreeMap<String, Vec<String>>,
) -> Result<()> {
    ensure!(
        !matches!(geometry, Geometry::Reference(_)),
        "GDTF geometry references require expansion and are unsupported"
    );
    let name = geometry
        .name()
        .context("GDTF geometry is missing a name")?
        .to_string();
    let mut path = ancestors.to_vec();
    path.push(name.clone());
    ensure!(
        names.insert(name, path.clone()).is_none(),
        "duplicate GDTF geometry name"
    );
    for child in geometry.children() {
        collect_geometries(child, &path, names)?;
    }
    Ok(())
}

// gdtf 0.3 requires Highlight even though GDTF makes it optional. Keep this adapter
// limited to the standard default; the library still owns description parsing.
fn materialize_parser_defaults(xml: &str) -> Result<String> {
    let mut reader = Reader::from_str(xml);
    let mut writer = Writer::new(Vec::with_capacity(xml.len()));
    let mut depth = 0;
    loop {
        let mut event = reader.read_event().context("read GDTF XML")?;
        match &event {
            Event::Start(_) => {
                depth += 1;
                ensure!(depth <= MAX_XML_DEPTH, "GDTF XML nesting exceeds limit");
            }
            Event::End(_) => depth = depth.saturating_sub(1),
            _ => {}
        }
        match &mut event {
            Event::Start(element) | Event::Empty(element)
                if element.name().as_ref() == b"DMXChannel" =>
            {
                if element.try_get_attribute("Highlight")?.is_none() {
                    element.push_attribute(("Highlight", "None"));
                }
            }
            Event::Eof => break,
            _ => {}
        }
        writer.write_event(event)?;
    }
    Ok(String::from_utf8(writer.into_inner())?)
}

// gdtf 0.3's widening conversion skips the last mirrored byte and permits a
// value one past its declared width. Validate and expand its parsed value here.
fn dmx_value(value: DmxValue, width: usize) -> Result<u32> {
    ensure!(
        (1..=MAX_CHANNEL_BYTES).contains(&width),
        "GDTF target resolution must be 1..4 bytes"
    );
    let source_width = usize::from(value.bytes().get());
    ensure!(
        (1..=MAX_CHANNEL_BYTES).contains(&source_width),
        "GDTF DMX value resolution must be 1..4 bytes"
    );
    let maximum = u64::from(u32::MAX) >> ((MAX_CHANNEL_BYTES - source_width) * DMX_BYTE_BITS);
    ensure!(
        value.value() <= maximum,
        "GDTF DMX value exceeds its declared resolution"
    );
    let mut output = 0;
    for index in 0..width {
        let byte = if value.shifting() && index >= source_width {
            0
        } else {
            let shift = (source_width - 1 - index % source_width) * DMX_BYTE_BITS;
            ((value.value() >> shift) & u64::from(DMX_BYTE_MASK)) as u32
        };
        output = (output << DMX_BYTE_BITS) | byte;
    }
    Ok(output)
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

fn cie_to_rgb(colour: ColorCie) -> Result<Rgb> {
    ensure!(
        [colour.x, colour.y, colour.z]
            .iter()
            .all(|value| value.is_finite())
            && colour.y > 0.0
            && colour.z >= 0.0,
        "invalid GDTF CIE wheel colour"
    );
    let luminance = colour.z / CIE_LUMINANCE_SCALE;
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
    Ok(normalize(Rgb {
        red: rgb[0],
        green: rgb[1],
        blue: rgb[2],
    }))
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
