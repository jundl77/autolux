//! Stateful playback and property composition; no wall clock or hardware I/O.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, ensure};
use serde::Serialize;

use crate::clock::{ClockSample, MusicalClock, validate_time};
use crate::effects::{self, MAX_ATTRIBUTES, MAX_MULTIPLIER};
use crate::fixtures::FixtureRig;
use crate::model::{
    Command, Effect, FixtureState, FixtureStates, LightingOverride, MAX_STROBE_HZ, PropertyValues,
    RenderContext, Rgb, Transition,
};
use crate::protocol::wire;
use crate::script::{LuaSequence, SequenceCatalog};

const MAX_ACTIVE_OVERRIDES: usize = 128;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_TRANSITION_BEATS: f64 = 1_024.0;
const STROBE_OPEN_FRACTION: f64 = 0.5;
pub const MAX_SCRIPT_ERROR_BYTES: usize = 1024;
const TRUNCATED_ERROR_SUFFIX: &str = "… [truncated]";

/// Semantic properties precede software strobe gating; DMX contains the final output.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RenderedFrame {
    pub time_seconds: f64,
    pub beat_position: f64,
    pub fixture_properties_before_strobe: FixtureStates,
    pub dmx_frames_by_universe: BTreeMap<u16, Vec<u8>>,
    pub active_bank: Option<String>,
    pub active_sequence: Option<String>,
    pub script_error: Option<String>,
}

struct ActiveSequence {
    bank: String,
    name: String,
    script: LuaSequence,
    start_beat: f64,
    start_animation_beats: f64,
}

impl ActiveSequence {
    fn render(
        &mut self,
        seconds: f64,
        time: ClockSample,
        normalized_rms_level: Option<f64>,
    ) -> Result<Vec<Effect>> {
        self.script.render(&RenderContext {
            time_seconds: seconds,
            beat_position: time.beat_position,
            sequence_elapsed_beats: (time.beat_position - self.start_beat).max(0.0),
            sequence_speed_scaled_elapsed_beats: (time.animation_beats
                - self.start_animation_beats)
                .max(0.0),
            normalized_rms_level,
        })
    }
}

enum FadeSource {
    Running(Box<ActiveSequence>),
    Frozen(FixtureStates),
}

struct Crossfade {
    source: FadeSource,
    start_beat: f64,
    duration_beats: f64,
}

struct ActiveOverride {
    value: wire::SetActiveEffect,
    fixture_ids: Vec<String>,
    order: u64,
    expires_seconds: Option<f64>,
    expires_beat: Option<f64>,
}

impl ActiveOverride {
    fn expired(&self, seconds: f64, beat: f64) -> bool {
        self.expires_seconds.is_some_and(|expiry| seconds >= expiry)
            || self.expires_beat.is_some_and(|expiry| beat >= expiry)
    }
}

pub struct Engine {
    rig: FixtureRig,
    catalog: SequenceCatalog,
    clock: MusicalClock,
    last_time_seconds: f64,
    active: Option<ActiveSequence>,
    fade: Option<Crossfade>,
    overrides: BTreeMap<String, ActiveOverride>,
    override_order: u64,
    master_intensity: f64,
    normalized_rms_level: Option<f64>,
    blackout: bool,
    script_error: Option<String>,
    last_output_states: FixtureStates,
}

impl Engine {
    pub fn new(rig: FixtureRig, catalog: SequenceCatalog) -> Result<Self> {
        let last_output_states = rig.empty_states();
        // Validate the final encoding boundary before accepting commands.
        rig.encode(&last_output_states)?;
        Ok(Self {
            rig,
            catalog,
            clock: MusicalClock::default(),
            last_time_seconds: 0.0,
            active: None,
            fade: None,
            overrides: BTreeMap::new(),
            override_order: 0,
            master_intensity: 1.0,
            normalized_rms_level: None,
            blackout: false,
            script_error: None,
            last_output_states,
        })
    }

    pub fn apply(&mut self, at_seconds: f64, command: &Command) -> Result<()> {
        self.validate_time(at_seconds)?;
        let time = self.clock.sample_at(at_seconds)?;
        match command {
            Command::Beat {
                position_beats,
                bpm,
            } => {
                self.clock
                    .set_beat_anchor(at_seconds, *position_beats, *bpm)?;
            }
            Command::Select {
                bank,
                sequence,
                transition,
            } => {
                validate_identifier(bank)?;
                validate_identifier(sequence)?;
                validate_transition(transition, time.elapsed_musical_beats)?;
                if !self
                    .active
                    .as_ref()
                    .is_some_and(|active| active.bank == *bank && active.name == *sequence)
                {
                    self.select(at_seconds, time, bank, sequence, transition)?;
                }
            }
            Command::SetOverride { value } => self.set_override(at_seconds, value)?,
            Command::ClearOverride { id } => {
                validate_identifier(id)?;
                self.overrides.remove(id);
            }
            Command::SetMaster { intensity } => {
                validate_unit(*intensity, "master intensity")?;
                self.master_intensity = *intensity;
            }
            Command::SetNormalizedRmsLevel { level } => {
                validate_unit(*level, "normalized RMS level")?;
                self.normalized_rms_level = Some(*level);
            }
            Command::SetSpeed { multiplier } => self.clock.set_speed(at_seconds, *multiplier)?,
            Command::SetBlackout { enabled } => self.blackout = *enabled,
            Command::Stop => {
                self.active = None;
                self.fade = None;
                self.overrides.clear();
                self.script_error = None;
                extinguish(&mut self.last_output_states);
            }
        }
        self.last_time_seconds = at_seconds;
        Ok(())
    }

    pub fn render_at(&mut self, time_seconds: f64) -> Result<RenderedFrame> {
        self.validate_time(time_seconds)?;
        let time = self.clock.sample_at(time_seconds)?;
        let mut states = if self.script_error.is_some() {
            self.dark_states()
        } else {
            match self.render_baseline(time_seconds, time) {
                Ok(states) => states,
                Err(error) => {
                    self.stop_on_script_error(&error);
                    self.dark_states()
                }
            }
        };

        if self.script_error.is_none() {
            self.compose_overrides(time_seconds, time.elapsed_musical_beats, &mut states)?;
            for state in states.values_mut() {
                state.intensity = state.intensity.clamp(0.0, 1.0) * self.master_intensity;
            }
        }
        if self.blackout {
            extinguish(&mut states);
        }

        let mut encoded_states = states.clone();
        apply_software_strobe(time_seconds, &mut encoded_states);
        let universes = self.rig.encode(&encoded_states)?;
        self.last_time_seconds = time_seconds;
        self.last_output_states = states.clone();
        Ok(RenderedFrame {
            time_seconds,
            beat_position: time.beat_position,
            fixture_properties_before_strobe: states,
            dmx_frames_by_universe: universes,
            active_bank: self.active.as_ref().map(|active| active.bank.clone()),
            active_sequence: self.active.as_ref().map(|active| active.name.clone()),
            script_error: self.script_error.clone(),
        })
    }

    fn validate_time(&self, time_seconds: f64) -> Result<()> {
        validate_time(time_seconds)?;
        ensure!(
            time_seconds >= self.last_time_seconds,
            "playback time must not move backwards"
        );
        Ok(())
    }

    fn select(
        &mut self,
        seconds: f64,
        time: ClockSample,
        bank: &str,
        sequence: &str,
        transition: &Transition,
    ) -> Result<()> {
        validate_identifier(bank)?;
        validate_identifier(sequence)?;
        let script = self
            .catalog
            .instantiate(bank, sequence, &self.rig)
            .with_context(|| format!("cannot load sequence {bank}/{sequence}"))?;

        // A replacement is compiled before the active program is touched.
        let frozen_source = if matches!(transition, Transition::Crossfade { .. })
            && self.fade.is_some()
            && self.script_error.is_none()
        {
            match self.render_baseline(seconds, time) {
                Ok(states) => Some(states),
                Err(error) => {
                    self.stop_on_script_error(&error);
                    self.last_time_seconds = seconds;
                    return Err(error);
                }
            }
        } else {
            None
        };
        let previous = self.active.take();
        self.fade = match transition {
            Transition::Cut => None,
            Transition::Crossfade { duration_beats } => {
                let source = if let Some(states) = frozen_source {
                    FadeSource::Frozen(states)
                } else if self.script_error.is_some() {
                    FadeSource::Frozen(self.dark_states())
                } else if let Some(previous) = previous {
                    FadeSource::Running(Box::new(previous))
                } else {
                    FadeSource::Frozen(self.dark_states())
                };
                Some(Crossfade {
                    source,
                    start_beat: time.elapsed_musical_beats,
                    duration_beats: *duration_beats,
                })
            }
        };
        self.active = Some(ActiveSequence {
            bank: bank.to_owned(),
            name: sequence.to_owned(),
            script,
            start_beat: time.beat_position,
            start_animation_beats: time.animation_beats,
        });
        self.script_error = None;
        Ok(())
    }

    fn render_baseline(&mut self, seconds: f64, time: ClockSample) -> Result<FixtureStates> {
        if self
            .fade
            .as_ref()
            .is_some_and(|fade| time.elapsed_musical_beats >= fade.start_beat + fade.duration_beats)
        {
            self.fade = None;
        }
        let Some(active) = &mut self.active else {
            return Ok(self.dark_states());
        };
        let effects = active
            .render(seconds, time, self.normalized_rms_level)
            .with_context(|| format!("sequence {}/{} failed", active.bank, active.name))?;
        let destination = states_for_effects(&self.rig, &effects)?;
        let Some(fade) = &mut self.fade else {
            return Ok(destination);
        };
        let source = match &mut fade.source {
            FadeSource::Frozen(states) => states.clone(),
            FadeSource::Running(previous) => {
                let effects = previous.render(seconds, time, self.normalized_rms_level)?;
                states_for_effects(&self.rig, &effects)?
            }
        };
        let amount =
            ((time.elapsed_musical_beats - fade.start_beat) / fade.duration_beats).clamp(0.0, 1.0);
        Ok(blend_states(source, destination, amount))
    }

    pub fn set_parameter(&mut self, seconds: f64, parameter: &wire::SetParameter) -> Result<()> {
        use wire::set_parameter::Parameter;
        self.validate_time(seconds)?;
        match parameter
            .parameter
            .as_ref()
            .context("parameter is required")?
        {
            Parameter::OutputBrightness(value) => {
                validate_unit(*value, "output brightness")?;
                self.master_intensity = *value;
            }
            Parameter::AnimationSpeed(value) => self.clock.set_speed(seconds, *value)?,
            Parameter::OutputBlackout(value) => self.blackout = *value,
            Parameter::AudioRmsNormalized(value) => {
                validate_unit(*value, "normalized RMS level")?;
                self.normalized_rms_level = Some(*value);
            }
            Parameter::SequenceParameter(parameter) => {
                let identity = parameter
                    .sequence
                    .as_ref()
                    .context("sequence parameter requires its sequence identity")?;
                let active = self.active.as_mut().context("no sequence is active")?;
                ensure!(
                    active.bank == identity.bank_id && active.name == identity.sequence_id,
                    "parameter target does not match the active sequence"
                );
                active.script.set_parameter(
                    &parameter.name,
                    parameter
                        .value
                        .as_ref()
                        .context("sequence parameter requires a value")?,
                )?;
            }
        }
        self.last_time_seconds = seconds;
        Ok(())
    }
    pub fn sequence_reply(&self, error: String) -> wire::PlaySequenceReply {
        wire::PlaySequenceReply {
            error,
            sequence: self.active.as_ref().map(|active| wire::SequenceReference {
                bank_id: active.bank.clone(),
                sequence_id: active.name.clone(),
            }),
            parameters: self
                .active
                .as_ref()
                .map(|active| active.script.parameter_definitions().to_vec())
                .unwrap_or_default(),
        }
    }
    fn set_override(&mut self, seconds: f64, value: &LightingOverride) -> Result<()> {
        validate_properties(&value.values)?;
        if let Some(multiplier) = value.intensity_multiplier {
            ensure!(
                multiplier.is_finite() && (0.0..=MAX_MULTIPLIER).contains(&multiplier),
                "intensity multiplier is outside the supported range"
            );
        }
        self.set_active_effect(seconds, &effects::from_override(value))
    }

    pub fn set_active_effect(&mut self, seconds: f64, value: &wire::SetActiveEffect) -> Result<()> {
        self.validate_time(seconds)?;
        let time = self.clock.sample_at(seconds)?;
        validate_identifier(&value.effect_id)?;
        ensure!(
            !value.attributes.is_empty() && value.attributes.len() <= MAX_ATTRIBUTES,
            "effect must contain 1..{MAX_ATTRIBUTES} attributes"
        );
        let mut keys = BTreeSet::new();
        for attribute in &value.attributes {
            ensure!(
                keys.insert(attribute.attribute),
                "duplicate effect attribute"
            );
            effects::validate_attribute(attribute)?;
        }
        let other_active_count = self
            .overrides
            .values()
            .filter(|existing| {
                existing.value.effect_id != value.effect_id
                    && !existing.expired(seconds, time.elapsed_musical_beats)
            })
            .count();
        ensure!(
            other_active_count < MAX_ACTIVE_OVERRIDES,
            "too many active lighting overrides"
        );
        let target = effects::fixture_target(
            value
                .fixtures
                .as_ref()
                .context("effect fixtures are required")?,
        )?;
        let fixture_ids = self.rig.resolve(&target)?;
        let expiration = value
            .lifetime
            .as_ref()
            .and_then(|lifetime| lifetime.expiration.as_ref())
            .context("effect lifetime is required")?;
        let (expires_seconds, expires_beat) = match expiration {
            wire::effect_lifetime::Expiration::UntilRemoved(_) => (None, None),
            wire::effect_lifetime::Expiration::DurationBeats(duration_beats) => (
                None,
                Some(expiry(time.elapsed_musical_beats, *duration_beats)?),
            ),
            wire::effect_lifetime::Expiration::DurationSeconds(duration_seconds) => {
                (Some(expiry(seconds, *duration_seconds)?), None)
            }
        };
        let order = self
            .override_order
            .checked_add(1)
            .context("override ordering exhausted")?;
        self.overrides
            .retain(|_, existing| !existing.expired(seconds, time.elapsed_musical_beats));
        self.overrides.insert(
            value.effect_id.clone(),
            ActiveOverride {
                value: value.clone(),
                fixture_ids,
                order,
                expires_seconds,
                expires_beat,
            },
        );
        self.override_order = order;
        self.last_time_seconds = seconds;
        Ok(())
    }

    fn compose_overrides(
        &mut self,
        seconds: f64,
        beat: f64,
        states: &mut FixtureStates,
    ) -> Result<()> {
        self.overrides
            .retain(|_, value| !value.expired(seconds, beat));
        let mut ordered: Vec<_> = self.overrides.values().collect();
        ordered.sort_by_key(|value| (value.value.priority, value.order));
        for value in &ordered {
            for id in &value.fixture_ids {
                if let Some(state) = states.get_mut(id) {
                    for attribute in &value.value.attributes {
                        effects::apply_attribute(state, attribute)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn dark_states(&self) -> FixtureStates {
        let mut states = self.last_output_states.clone();
        extinguish(&mut states);
        states
    }

    fn stop_on_script_error(&mut self, error: &anyhow::Error) {
        let mut diagnostic = format!("{error:#}");
        if diagnostic.len() > MAX_SCRIPT_ERROR_BYTES {
            let end = diagnostic
                .floor_char_boundary(MAX_SCRIPT_ERROR_BYTES - TRUNCATED_ERROR_SUFFIX.len());
            diagnostic.truncate(end);
            diagnostic.push_str(TRUNCATED_ERROR_SUFFIX);
            diagnostic.shrink_to_fit();
        }
        self.script_error = Some(diagnostic);
        self.fade = None;
        self.overrides.clear();
    }
}

fn validate_transition(transition: &Transition, elapsed_beats: f64) -> Result<()> {
    if let Transition::Crossfade { duration_beats } = transition {
        ensure!(
            duration_beats.is_finite()
                && *duration_beats > 0.0
                && *duration_beats <= MAX_TRANSITION_BEATS,
            "crossfade duration is outside the supported range"
        );
        expiry(elapsed_beats, *duration_beats)?;
    }
    Ok(())
}

fn expiry(start: f64, duration: f64) -> Result<f64> {
    ensure!(
        duration.is_finite() && duration > 0.0,
        "override duration must be finite and positive"
    );
    let end = start + duration;
    ensure!(
        end.is_finite() && end > start,
        "override expiry cannot be represented"
    );
    Ok(end)
}

fn validate_identifier(value: &str) -> Result<()> {
    ensure!(
        !value.trim().is_empty() && value.len() <= MAX_IDENTIFIER_BYTES,
        "invalid identifier length"
    );
    Ok(())
}

fn validate_unit(value: f64, name: &str) -> Result<()> {
    ensure!(
        value.is_finite() && (0.0..=1.0).contains(&value),
        "{name} must be between zero and one"
    );
    Ok(())
}

fn validate_properties(values: &PropertyValues) -> Result<()> {
    for (name, value) in [
        ("intensity", values.intensity),
        ("pan", values.pan),
        ("tilt", values.tilt),
    ] {
        if let Some(value) = value {
            validate_unit(value, name)?;
        }
    }
    if let Some(colour) = values.colour {
        validate_unit(colour.red, "red")?;
        validate_unit(colour.green, "green")?;
        validate_unit(colour.blue, "blue")?;
    }
    if let Some(rate) = values.strobe_hz {
        ensure!(
            rate.is_finite() && (0.0..=MAX_STROBE_HZ).contains(&rate),
            "strobe rate is outside the supported range"
        );
    }
    Ok(())
}

fn apply_properties(state: &mut FixtureState, values: &PropertyValues) {
    if let Some(value) = values.intensity {
        state.intensity = value;
    }
    if let Some(value) = values.colour {
        state.colour = value;
    }
    if let Some(value) = values.pan {
        state.pan = value;
    }
    if let Some(value) = values.tilt {
        state.tilt = value;
    }
    if let Some(value) = values.strobe_hz {
        state.strobe_hz = value;
    }
}

fn states_for_effects(rig: &FixtureRig, effects: &[Effect]) -> Result<FixtureStates> {
    let mut states = rig.empty_states();
    for effect in effects {
        validate_properties(&effect.values)?;
        for id in rig.resolve(&effect.target)? {
            let state = states
                .get_mut(&id)
                .context("resolved fixture is absent from the rig")?;
            apply_properties(state, &effect.values);
        }
    }
    Ok(states)
}

fn blend_states(
    source: FixtureStates,
    mut destination: FixtureStates,
    amount: f64,
) -> FixtureStates {
    for (id, to) in &mut destination {
        if let Some(from) = source.get(id) {
            to.intensity = interpolate(from.intensity, to.intensity, amount);
            to.colour = Rgb {
                red: interpolate(from.colour.red, to.colour.red, amount),
                green: interpolate(from.colour.green, to.colour.green, amount),
                blue: interpolate(from.colour.blue, to.colour.blue, amount),
            };
            to.pan = interpolate(from.pan, to.pan, amount);
            to.tilt = interpolate(from.tilt, to.tilt, amount);
            if amount < 1.0 {
                to.strobe_hz = from.strobe_hz;
            }
        }
    }
    destination
}

fn interpolate(from: f64, to: f64, amount: f64) -> f64 {
    from + (to - from) * amount
}

fn extinguish(states: &mut FixtureStates) {
    for state in states.values_mut() {
        state.intensity = 0.0;
        state.strobe_hz = 0.0;
    }
}

fn apply_software_strobe(seconds: f64, states: &mut FixtureStates) {
    for state in states.values_mut() {
        if state.strobe_hz > 0.0 {
            let cycles = seconds * state.strobe_hz;
            let phase = if cycles.is_finite() {
                cycles.fract()
            } else {
                seconds.rem_euclid(1.0 / state.strobe_hz) * state.strobe_hz
            };
            if phase >= STROBE_OPEN_FRACTION {
                state.intensity = 0.0;
            }
        }
        state.strobe_hz = 0.0;
    }
}
