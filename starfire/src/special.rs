//! Map the fixture-agnostic special vocabulary onto GDTF attribute families.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, ensure};

use crate::fixtures::FixtureRig;
use crate::gdtf::{FunctionInfo, GdtfProfile, numbered_attribute as numbered};
use crate::model::{AttributeValue, FixtureState, Rgb, Special};
use crate::palette::PaletteSlots;
use crate::protocol::wire;

pub const MAX_ENVELOPE_BEATS: f64 = 1_024.0;
pub const MAX_PULSE_BEATS: u32 = 64;
pub const MAX_SCAN_CYCLES_PER_BEAT: f64 = 0.5;
const PULSE_OPEN_FRACTION: f64 = 0.5;
const SCAN_CENTRE_PHASE: f64 = 0.25;

#[derive(Clone, Debug)]
struct Continuous {
    channel: usize,
    key: String,
    from: f64,
    to: f64,
    rest: f64,
}

impl Continuous {
    fn new(function: &FunctionInfo) -> Self {
        Self {
            channel: function.channel,
            key: function.key.clone(),
            from: function.physical_from,
            to: function.physical_to,
            rest: function.rest,
        }
    }

    fn at_speed(&self, speed: f64) -> f64 {
        let from = if self.from < 0.0 && self.to > 0.0 {
            0.0
        } else {
            self.from
        };
        from + (self.to - from) * speed
    }
}

#[derive(Clone, Debug)]
struct Slots {
    channel: usize,
    key: String,
    values: Vec<AttributeValue>,
}

impl Slots {
    fn new(function: &FunctionInfo) -> Self {
        Self {
            channel: function.channel,
            key: function.key.clone(),
            values: function.slots.clone(),
        }
    }

    fn at(&self, index: usize) -> AttributeValue {
        self.values[index % self.values.len()].clone()
    }

    fn index_of(&self, pattern: f64) -> usize {
        ((pattern * self.values.len() as f64) as usize).min(self.values.len() - 1)
    }
}

#[derive(Clone, Debug)]
enum Pulse {
    Advance(Slots),
    Shutter(Continuous),
    Intensity,
}

/// What one fixture's profile can express, resolved once per session.
#[derive(Clone, Debug, Default)]
pub struct FixtureMap {
    colour_channels: BTreeSet<usize>,
    energy_channels: BTreeSet<usize>,
    rotations: Vec<Continuous>,
    scan_channels: BTreeSet<usize>,
    pulse: Option<Pulse>,
    pattern: Option<Slots>,
}

impl FixtureMap {
    pub fn new(profile: &GdtfProfile) -> Self {
        let functions = profile.functions();
        let family = |predicate: &dyn Fn(&FunctionInfo) -> bool| -> Vec<&FunctionInfo> {
            functions
                .iter()
                .filter(|function| predicate(function))
                .collect()
        };
        let has = |attribute: &str| functions.iter().any(|f| f.attribute == attribute);
        let mixing = ["ColorAdd_R", "ColorAdd_G", "ColorAdd_B"]
            .iter()
            .all(|attribute| has(attribute))
            || ["ColorSub_C", "ColorSub_M", "ColorSub_Y"]
                .iter()
                .all(|attribute| has(attribute));
        let colour_wheels = family(&|f| numbered(&f.attribute, "Color") && f.slot_colours);
        let colour_channels: BTreeSet<_> = if mixing || !colour_wheels.is_empty() {
            family(&|f| {
                f.attribute.starts_with("ColorAdd_")
                    || f.attribute.starts_with("ColorSub_")
                    || numbered(&f.attribute, "Color")
            })
            .iter()
            .map(|f| f.channel)
            .collect()
        } else {
            BTreeSet::new()
        };
        let dimmers = family(&|f| f.attribute == "Dimmer");
        let rotations: Vec<_> = family(&|f| {
            (f.attribute.ends_with("Rotate") || f.attribute.ends_with("WheelSpin"))
                && f.slots.is_empty()
                && f.physical_from != f.physical_to
        })
        .into_iter()
        .map(Continuous::new)
        .collect();
        let scan_channels = family(&|f| f.attribute == "Pan")
            .iter()
            .map(|f| f.channel)
            .collect();
        let wheel = |prefix: &str| {
            family(&|f| numbered(&f.attribute, prefix) && f.slots.len() >= 2)
                .first()
                .map(|f| Slots::new(f))
        };
        let gobo = wheel("Gobo");
        let shutter = family(&|f| {
            numbered(&f.attribute, "Shutter") && f.slots.is_empty() && f.rest != f.physical_from
        })
        .first()
        .map(|f| Continuous::new(f));
        let pulse = gobo
            .clone()
            .map(Pulse::Advance)
            .or_else(|| {
                (colour_channels.is_empty())
                    .then(|| wheel("Color").map(Pulse::Advance))
                    .flatten()
            })
            .or_else(|| shutter.map(Pulse::Shutter))
            .or_else(|| profile.expresses_intensity().then_some(Pulse::Intensity));
        Self {
            colour_channels,
            energy_channels: dimmers.iter().map(|f| f.channel).collect(),
            rotations,
            scan_channels,
            pulse,
            pattern: gobo.or_else(|| wheel("Prism")),
        }
    }

    pub fn colour(&self) -> bool {
        !self.colour_channels.is_empty()
    }

    pub fn speed(&self) -> bool {
        !self.rotations.is_empty() || !self.scan_channels.is_empty()
    }

    pub fn pulse(&self) -> bool {
        self.pulse.is_some()
    }

    pub fn pattern(&self) -> bool {
        self.pattern.is_some()
    }
}

pub fn fixture_maps(rig: &FixtureRig) -> BTreeMap<String, FixtureMap> {
    rig.fixture_ids()
        .into_iter()
        .filter_map(|id| {
            let map = FixtureMap::new(rig.profile(&id)?);
            Some((id, map))
        })
        .collect()
}

pub fn capabilities(rig: &FixtureRig) -> Vec<wire::GroupCapability> {
    let maps = fixture_maps(rig);
    rig.groups()
        .iter()
        .map(|(name, members)| {
            let any = |axis: fn(&FixtureMap) -> bool| {
                members
                    .iter()
                    .any(|member| maps.get(member).is_some_and(axis))
            };
            wire::GroupCapability {
                name: name.clone(),
                fixture_count: members.len() as u32,
                colour: any(FixtureMap::colour),
                speed: any(FixtureMap::speed),
                pulse: any(FixtureMap::pulse),
                pattern: any(FixtureMap::pattern),
            }
        })
        .collect()
}

pub fn validate(special: &Special) -> Result<()> {
    for (name, value) in [("energy", special.energy), ("speed", special.speed)]
        .into_iter()
        .chain(special.pattern.map(|pattern| ("pattern", pattern)))
    {
        ensure!(
            value.is_finite() && (0.0..=1.0).contains(&value),
            "special {name} must be between zero and one"
        );
    }
    for (name, value) in [
        ("attack", special.attack_beats),
        ("release", special.release_beats),
    ] {
        ensure!(
            value.is_finite() && (0.0..=MAX_ENVELOPE_BEATS).contains(&value),
            "special {name} must be 0..{MAX_ENVELOPE_BEATS} beats"
        );
    }
    ensure!(
        special
            .pulse_beats
            .is_none_or(|beats| (1..=MAX_PULSE_BEATS).contains(&beats)),
        "special pulse must be 1..{MAX_PULSE_BEATS} beats"
    );
    ensure!(
        special.colour_slot.is_empty()
            || PaletteSlots::NAMES.contains(&special.colour_slot.as_str()),
        "unknown palette slot {:?}",
        special.colour_slot
    );
    Ok(())
}

pub fn attack_level(special: &Special, elapsed_beats: f64) -> f64 {
    if special.attack_beats > 0.0 {
        (elapsed_beats / special.attack_beats).clamp(0.0, 1.0)
    } else {
        1.0
    }
}

pub fn release_level(special: &Special, from_level: f64, elapsed_beats: f64) -> f64 {
    if special.release_beats > 0.0 {
        from_level * (1.0 - elapsed_beats / special.release_beats).clamp(0.0, 1.0)
    } else {
        0.0
    }
}

/// Musical time and session state one special renders against.
#[derive(Clone, Copy, Debug)]
pub struct SpecialFrame<'a> {
    pub level: f64,
    pub beat_position: f64,
    pub elapsed_beats: f64,
    pub palette: &'a PaletteSlots,
    pub intensity_scale: f64,
}

pub fn apply(
    state: &mut FixtureState,
    map: &FixtureMap,
    profile: &GdtfProfile,
    special: &Special,
    frame: &SpecialFrame,
) {
    let level = frame.level;
    if level <= 0.0 {
        return;
    }
    let colour = (map.colour())
        .then(|| frame.palette.slot(&special.colour_slot))
        .flatten();
    let speed = special.speed * frame.intensity_scale;
    let pulse = special
        .pulse_beats
        .filter(|_| frame.intensity_scale > 0.0)
        .and_then(|beats| Some((map.pulse.as_ref()?, f64::from(beats))));
    let pattern = special.pattern.and_then(|value| {
        let slots = map.pattern.as_ref()?;
        Some((slots, slots.index_of(value)))
    });
    let scan = speed > 0.0 && !map.scan_channels.is_empty();

    let mut claimed = map.energy_channels.clone();
    if colour.is_some() {
        claimed.extend(&map.colour_channels);
    }
    claimed.extend(map.rotations.iter().map(|rotation| rotation.channel));
    if scan {
        claimed.extend(&map.scan_channels);
    }
    match pulse {
        Some((Pulse::Advance(slots), _)) => {
            claimed.insert(slots.channel);
        }
        Some((Pulse::Shutter(shutter), _)) => {
            claimed.insert(shutter.channel);
        }
        _ => {}
    }
    if let Some((slots, _)) = pattern {
        claimed.insert(slots.channel);
    }
    let baseline: BTreeMap<usize, f64> = state
        .attributes
        .iter()
        .filter_map(|(key, value)| match value {
            AttributeValue::Number(number) => Some((profile.channels_matching(key), *number)),
            AttributeValue::Preset(_) => None,
        })
        .filter(|(channels, _)| channels.len() == 1)
        .filter_map(|(channels, number)| Some((*channels.first()?, number)))
        .collect();
    state
        .attributes
        .retain(|key, _| profile.channels_matching(key).is_disjoint(&claimed));
    let mut blend = |target: &Continuous, value: f64| {
        let from = baseline
            .get(&target.channel)
            .copied()
            .unwrap_or(target.rest);
        state.attributes.insert(
            target.key.clone(),
            AttributeValue::Number(lerp(from, value, level)),
        );
    };

    for rotation in &map.rotations {
        blend(rotation, rotation.at_speed(speed));
    }
    let mut open = true;
    let mut advanced = None;
    if let Some((stepper, beats)) = pulse {
        let steps = frame.beat_position / beats;
        open = steps.fract() < PULSE_OPEN_FRACTION;
        match stepper {
            Pulse::Advance(slots) => advanced = Some((slots, steps.floor() as usize)),
            Pulse::Shutter(shutter) => {
                blend(shutter, if open { shutter.rest } else { shutter.from });
            }
            Pulse::Intensity => {}
        }
    }
    let base = pattern.map_or(0, |(_, index)| index);
    if let Some((slots, step)) = advanced {
        let shared = pattern.is_some_and(|(pattern, _)| pattern.channel == slots.channel);
        let index = if shared { base + step } else { step };
        state.attributes.insert(slots.key.clone(), slots.at(index));
    }
    if let Some((slots, index)) = pattern
        && advanced.is_none_or(|(advanced, _)| advanced.channel != slots.channel)
    {
        state.attributes.insert(slots.key.clone(), slots.at(index));
    }

    state.intensity = lerp(state.intensity, special.energy, level);
    if let Some(colour) = colour {
        state.colour = Rgb {
            red: lerp(state.colour.red, colour.red, level),
            green: lerp(state.colour.green, colour.green, level),
            blue: lerp(state.colour.blue, colour.blue, level),
        };
    }
    if scan {
        let cycles = frame.elapsed_beats * speed * MAX_SCAN_CYCLES_PER_BEAT;
        let sweep = 1.0 - (2.0 * (cycles + SCAN_CENTRE_PHASE).fract() - 1.0).abs();
        state.pan = lerp(state.pan, sweep, level);
    }
    if !open && matches!(pulse, Some((Pulse::Intensity, _))) {
        state.intensity *= 1.0 - level;
    }
}

fn lerp(from: f64, to: f64, amount: f64) -> f64 {
    from + (to - from) * amount
}
