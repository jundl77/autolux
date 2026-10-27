//! Stateful playback and property composition; no wall clock or hardware I/O.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use serde::Serialize;

use crate::clock::{BeatFact, ClockSample, MusicalClock, validate_time};
use crate::fixtures::FixtureRig;
use crate::intensity;
use crate::model::{
    Command, Effect, FixtureState, FixtureStates, LightingOverride, MAX_STROBE_HZ,
    OverrideLifetime, PropertyValues, RenderContext, SpecialFx, Target, Transition, lerp,
};
use crate::palette::{PaletteSlots, PaletteTable};
use crate::protocol::wire;
use crate::script::{AutoloopCatalog, LuaAutoloop};
use crate::special::{self, CellMap, Progress, SpecialFrame};

const MAX_ACTIVE_OVERRIDES: usize = 128;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_INTENSITY_MULTIPLIER: f64 = 8.0;
const MAX_TRANSITION_BEATS: f64 = 1_024.0;
const MAX_STROBE_BURST_SECONDS: f64 = 60.0;
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
    pub active_autoloop: Option<String>,
    pub script_error: Option<String>,
}

struct ActiveAutoloop {
    bank: String,
    name: String,
    script: LuaAutoloop,
    start_beat: f64,
    start_animation_beats: f64,
}

impl ActiveAutoloop {
    fn render(
        &mut self,
        seconds: f64,
        time: ClockSample,
        normalized_rms_level: Option<f64>,
        palette: &PaletteSlots,
        intensity: f64,
    ) -> Result<Vec<Effect>> {
        self.script.render(
            &RenderContext {
                time_seconds: seconds,
                beat_position: time.beat_position,
                bar_phase: time.bar_phase,
                bar_index: time.bar_index,
                beats_per_bar: time.beats_per_bar,
                autoloop_elapsed_beats: (time.beat_position - self.start_beat).max(0.0),
                autoloop_speed_scaled_elapsed_beats: (time.animation_beats
                    - self.start_animation_beats)
                    .max(0.0),
                normalized_rms_level,
                intensity,
            },
            palette,
        )
    }
}

enum FadeSource {
    Running(Box<ActiveAutoloop>),
    Frozen(FixtureStates),
}

struct Crossfade {
    source: FadeSource,
    start_beat: f64,
    duration_beats: f64,
}

struct ActiveOverride {
    value: LightingOverride,
    cell_ids: Vec<String>,
    order: u64,
    expires_seconds: Option<f64>,
    expires_beat: Option<f64>,
    progress: Progress,
}

impl ActiveOverride {
    fn expired(&self, seconds: f64, beat: f64) -> bool {
        self.expires_seconds.is_some_and(|expiry| seconds >= expiry)
            || self.expires_beat.is_some_and(|expiry| beat >= expiry)
    }

    fn release(self, beat: f64) -> Option<ReleasingSpecial> {
        let special = *self.value.special_fx?;
        let from_level = special::attack_level(&special, beat - self.progress.started_beat);
        (special.release_beats > 0.0 && from_level > 0.0).then_some(ReleasingSpecial {
            id: self.value.id,
            special,
            cell_ids: self.cell_ids,
            priority: self.value.priority,
            order: self.order,
            progress: self.progress,
            released_beat: beat,
            from_level,
        })
    }
}

struct ReleasingSpecial {
    id: String,
    special: SpecialFx,
    cell_ids: Vec<String>,
    priority: i32,
    order: u64,
    progress: Progress,
    released_beat: f64,
    from_level: f64,
}

impl ReleasingSpecial {
    fn level(&self, beat: f64) -> f64 {
        special::release_level(&self.special, self.from_level, beat - self.released_beat)
    }

    /// Shortens the fade still to run to at most `cap` beats, falling from its level now.
    fn cut(&mut self, beat: f64, cap: f64) {
        let remaining = self.special.release_beats - (beat - self.released_beat);
        self.from_level = self.level(beat);
        self.released_beat = beat;
        self.special.release_beats = remaining.min(cap);
    }
}

pub struct Engine {
    rig: FixtureRig,
    catalog: AutoloopCatalog,
    palettes: PaletteTable,
    clock: MusicalClock,
    last_time_seconds: f64,
    active: Option<ActiveAutoloop>,
    fade: Option<Crossfade>,
    overrides: BTreeMap<String, ActiveOverride>,
    releasing: Vec<ReleasingSpecial>,
    special_maps: BTreeMap<String, CellMap>,
    override_order: u64,
    master: MasterFade,
    /// Levels below one only; a group at full is absent.
    group_masters: BTreeMap<String, f64>,
    normalized_rms_level: Option<f64>,
    blackout: bool,
    script_error: Option<String>,
    last_output_states: FixtureStates,
    paused_at_seconds: Option<f64>,
    default_crossfade_beats: f64,
    strobe_max_rate_hz: f64,
    strobe_max_burst_seconds: f64,
    strobe_started_seconds: BTreeMap<String, f64>,
    palette: ActivePalette,
    intensity: f64,
    last_render_seconds: Option<f64>,
}

impl Engine {
    pub fn new(rig: FixtureRig, catalog: AutoloopCatalog, palettes: PaletteTable) -> Result<Self> {
        let last_output_states = rig.empty_states();
        let special_maps = special::cell_maps(&rig);
        // Validate the final encoding boundary before accepting commands.
        rig.encode(&last_output_states)?;
        let palette =
            ActivePalette::steady(palettes.initial(), palettes.slots(palettes.initial())?);
        Ok(Self {
            rig,
            catalog,
            palettes,
            clock: MusicalClock::default(),
            last_time_seconds: 0.0,
            active: None,
            fade: None,
            overrides: BTreeMap::new(),
            releasing: Vec::new(),
            special_maps,
            override_order: 0,
            master: MasterFade::steady(1.0),
            group_masters: BTreeMap::new(),
            normalized_rms_level: None,
            blackout: false,
            script_error: None,
            last_output_states,
            paused_at_seconds: None,
            default_crossfade_beats: 0.0,
            strobe_max_rate_hz: MAX_STROBE_HZ,
            strobe_max_burst_seconds: f64::INFINITY,
            strobe_started_seconds: BTreeMap::new(),
            palette,
            intensity: 1.0,
            last_render_seconds: None,
        })
    }

    pub fn apply(&mut self, at_seconds: f64, command: &Command) -> Result<()> {
        self.validate_time(at_seconds)?;
        let time = self.clock.sample_at(at_seconds)?;
        match command {
            Command::Beat {
                beat_index,
                bar_phase,
                beats_per_bar,
                bpm,
            } => {
                self.clock.set_beat(
                    at_seconds,
                    BeatFact {
                        beat_index: *beat_index,
                        bar_phase: *bar_phase,
                        beats_per_bar: *beats_per_bar,
                        bpm: *bpm,
                    },
                )?;
            }
            Command::Select {
                bank,
                autoloop,
                transition,
                palette,
            } => {
                validate_identifier(bank)?;
                validate_identifier(autoloop)?;
                let transition = &self.resolve_transition(transition);
                validate_transition(transition, time.elapsed_musical_beats)?;
                let slots = self.palettes.slots(palette)?;
                if !self
                    .active
                    .as_ref()
                    .is_some_and(|active| active.bank == *bank && active.name == *autoloop)
                {
                    self.select(at_seconds, time, bank, autoloop, transition)?;
                }
                let duration_beats = match transition {
                    Transition::Crossfade { duration_beats } => *duration_beats,
                    Transition::Cut | Transition::RendererDefault => 0.0,
                };
                self.palette
                    .retarget(time.elapsed_musical_beats, palette, slots, duration_beats);
            }
            Command::SetPalette { name } => {
                let slots = self.palettes.slots(name)?;
                self.palette.retarget(
                    time.elapsed_musical_beats,
                    name,
                    slots,
                    self.default_crossfade_beats,
                );
            }
            Command::SetOverride { value } => self.set_override(at_seconds, time, value)?,
            Command::ClearOverride {
                id,
                release_beats,
                release_seconds,
            } => {
                validate_identifier(id)?;
                for cap in [release_beats, release_seconds].into_iter().flatten() {
                    ensure!(
                        cap.is_finite() && *cap >= 0.0,
                        "release caps must be finite and non-negative"
                    );
                }
                // Applied at this frame's beat, never deferred: an operator cut must read at once.
                let seconds_cap = release_seconds.map(|cap| cap * self.clock.beats_per_second());
                let cap = [*release_beats, seconds_cap]
                    .into_iter()
                    .flatten()
                    .reduce(f64::min);
                if let Some(cap) = cap {
                    // A special already fading under this ID (released, or replaced) is cut too.
                    for releasing in self.releasing.iter_mut().filter(|r| r.id == *id) {
                        releasing.cut(time.elapsed_musical_beats, cap);
                    }
                }
                if let Some(mut removed) = self.overrides.remove(id) {
                    if let (Some(cap), Some(special)) = (cap, removed.value.special_fx.as_mut()) {
                        special.release_beats = special.release_beats.min(cap);
                    }
                    self.release(removed, time.elapsed_musical_beats);
                }
            }
            Command::SetMaster {
                intensity,
                fade_seconds,
            } => {
                validate_unit(*intensity, "master intensity")?;
                ensure!(
                    fade_seconds.is_finite() && *fade_seconds >= 0.0,
                    "master fade must be finite and nonnegative"
                );
                self.master.retarget(at_seconds, *intensity, *fade_seconds);
            }
            Command::SetNormalizedRmsLevel { level } => {
                validate_unit(*level, "normalized RMS level")?;
                self.normalized_rms_level = Some(*level);
            }
            Command::SetSpeed { multiplier } => self.clock.set_speed(at_seconds, *multiplier)?,
            Command::SetBlackout { enabled } => self.blackout = *enabled,
            Command::SetPaused { paused } => self.set_paused(at_seconds, *paused)?,
            Command::SetDefaultCrossfadeBeats { beats } => {
                ensure!(
                    beats.is_finite() && (0.0..=MAX_TRANSITION_BEATS).contains(beats),
                    "default crossfade is outside the supported range"
                );
                self.default_crossfade_beats = *beats;
            }
            Command::SetStrobeMaxRateHz { hz } => {
                ensure!(
                    hz.is_finite() && (0.0..=MAX_STROBE_HZ).contains(hz),
                    "strobe rate cap is outside the supported range"
                );
                self.strobe_max_rate_hz = *hz;
            }
            Command::SetStrobeMaxBurstSeconds { seconds } => {
                ensure!(
                    seconds.is_finite() && *seconds > 0.0 && *seconds <= MAX_STROBE_BURST_SECONDS,
                    "strobe burst cap is outside the supported range"
                );
                self.strobe_max_burst_seconds = *seconds;
            }
            Command::SetIntensity { level } => {
                validate_unit(*level, "intensity")?;
                self.intensity = *level;
            }
            Command::SetGroupMaster { group, level } => {
                validate_unit(*level, "group master")?;
                ensure!(
                    self.rig.groups().contains_key(group),
                    "unknown fixture group {group}"
                );
                if *level < 1.0 {
                    self.group_masters.insert(group.clone(), *level);
                } else {
                    self.group_masters.remove(group);
                }
            }
            Command::Stop => {
                self.active = None;
                self.fade = None;
                self.overrides.clear();
                self.releasing.clear();
                self.script_error = None;
                extinguish(&mut self.last_output_states);
            }
        }
        self.last_time_seconds = at_seconds;
        Ok(())
    }

    pub fn play_autoloop(
        &mut self,
        at_seconds: f64,
        autoloop_id: &str,
        transition: Transition,
        palette: &str,
    ) -> Result<()> {
        let (bank, autoloop) = self.catalog.resolve(autoloop_id)?.clone();
        self.apply(
            at_seconds,
            &Command::Select {
                bank,
                autoloop,
                transition,
                palette: palette.to_owned(),
            },
        )
    }

    pub fn active_palette(&self) -> &str {
        &self.palette.name
    }

    pub fn set_parameter(&mut self, seconds: f64, parameter: &wire::Parameter) -> Result<()> {
        use wire::{ParameterType, parameter::Value};
        let kind = ParameterType::try_from(parameter.r#type);
        ensure!(
            parameter.group.is_empty() || kind == Ok(ParameterType::GroupMaster),
            "only a group master names a group"
        );
        let command = match (kind, parameter.value.as_ref()) {
            (Ok(ParameterType::OutputBrightness), Some(Value::Number(value))) => {
                Command::SetMaster {
                    intensity: *value,
                    fade_seconds: parameter.fade_seconds,
                }
            }
            _ if parameter.fade_seconds != 0.0 => {
                anyhow::bail!("only output brightness accepts a fade")
            }
            (Ok(ParameterType::AnimationSpeed), Some(Value::Number(value))) => {
                Command::SetSpeed { multiplier: *value }
            }
            (Ok(ParameterType::OutputBlackout), Some(Value::Boolean(value))) => {
                Command::SetBlackout { enabled: *value }
            }
            (Ok(ParameterType::AudioRmsNormalized), Some(Value::Number(value))) => {
                Command::SetNormalizedRmsLevel { level: *value }
            }
            (Ok(ParameterType::Paused), Some(Value::Boolean(value))) => {
                Command::SetPaused { paused: *value }
            }
            (Ok(ParameterType::DefaultCrossfadeBeats), Some(Value::Number(value))) => {
                Command::SetDefaultCrossfadeBeats { beats: *value }
            }
            (Ok(ParameterType::StrobeMaxRateHz), Some(Value::Number(value))) => {
                Command::SetStrobeMaxRateHz { hz: *value }
            }
            (Ok(ParameterType::StrobeMaxBurstSeconds), Some(Value::Number(value))) => {
                Command::SetStrobeMaxBurstSeconds { seconds: *value }
            }
            (Ok(ParameterType::Palette), Some(Value::Text(name))) => {
                Command::SetPalette { name: name.clone() }
            }
            (Ok(ParameterType::Intensity), Some(Value::Number(value))) => {
                Command::SetIntensity { level: *value }
            }
            (Ok(ParameterType::GroupMaster), Some(Value::Number(value))) => {
                Command::SetGroupMaster {
                    group: parameter.group.clone(),
                    level: *value,
                }
            }
            _ => anyhow::bail!("unknown parameter type or incompatible value"),
        };
        self.apply(seconds, &command)
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
            self.compose_overrides(time_seconds, time, &mut states);
            let master = perceptual_to_linear_master(self.master.level_at(time_seconds));
            for state in states.values_mut() {
                state.intensity = state.intensity.clamp(0.0, 1.0) * master;
                state.attribute_brightness = master;
            }
            // A cell in several capped groups takes the product of their masters.
            for (group, level) in &self.group_masters {
                for cell in &self.rig.groups()[group] {
                    if let Some(state) = states.get_mut(cell) {
                        state.intensity *= level;
                        state.attribute_brightness *= level;
                    }
                }
            }
        }
        self.limit_movement_speed(time_seconds, &mut states);
        self.limit_strobes(time_seconds, &mut states);
        // Pause is the operator's lights-off. A dimmerless fixture (the Kinta) shows any master
        // above zero at full colour and keeps its frozen spin, so all but pan/tilt go to the
        // GDTF default. Overrides and static looks go dark too, on purpose: pause means off.
        if self.blackout || self.clock.paused() {
            extinguish(&mut states);
        }

        let mut encoded_states = states.clone();
        apply_software_strobe(time_seconds, &mut encoded_states);
        let universes = match self.rig.encode(&encoded_states) {
            Ok(universes) => universes,
            Err(error) => {
                self.stop_on_script_error(&error);
                states = self.dark_states();
                self.rig
                    .encode(&states)
                    .context("encode safe darkness after render failure")?
            }
        };
        self.last_time_seconds = time_seconds;
        self.last_render_seconds = Some(time_seconds);
        self.last_output_states = states.clone();
        Ok(RenderedFrame {
            time_seconds,
            beat_position: time.beat_position,
            fixture_properties_before_strobe: states,
            dmx_frames_by_universe: universes,
            active_bank: self.active.as_ref().map(|active| active.bank.clone()),
            active_autoloop: self.active.as_ref().map(|active| active.name.clone()),
            script_error: self.script_error.clone(),
        })
    }

    fn set_paused(&mut self, seconds: f64, paused: bool) -> Result<()> {
        if paused == self.clock.paused() {
            return Ok(());
        }
        self.clock.set_paused(seconds, paused)?;
        self.paused_at_seconds = paused.then_some(seconds);
        if paused {
            self.release_strobe_overrides_instead_of_holding_them();
            // A frozen clock would hold a fade at its level and replay it on play.
            self.releasing.clear();
        }
        Ok(())
    }

    fn release_strobe_overrides_instead_of_holding_them(&mut self) {
        self.overrides
            .retain(|_, active| active.value.values.strobe_hz.is_none_or(|rate| rate <= 0.0));
    }

    fn resolve_transition(&self, transition: &Transition) -> Transition {
        match transition {
            Transition::RendererDefault if self.default_crossfade_beats > 0.0 => {
                Transition::Crossfade {
                    duration_beats: self.default_crossfade_beats,
                }
            }
            Transition::RendererDefault => Transition::Cut,
            explicit => explicit.clone(),
        }
    }

    fn limit_strobes(&mut self, seconds: f64, states: &mut FixtureStates) {
        let paused = self.clock.paused();
        let strobe_scale = intensity::strobe_scale(self.intensity);
        let (max_rate_hz, max_burst_seconds) = if strobe_scale > 0.0 {
            (
                self.strobe_max_rate_hz * strobe_scale,
                self.strobe_max_burst_seconds * strobe_scale,
            )
        } else {
            (0.0, 0.0)
        };
        for (id, state) in states.iter_mut() {
            if paused {
                state.strobe_hz = 0.0;
            }
            state.strobe_hz = state.strobe_hz.min(max_rate_hz);
            if state.strobe_hz <= 0.0 {
                self.strobe_started_seconds.remove(id);
                continue;
            }
            let started = *self
                .strobe_started_seconds
                .entry(id.clone())
                .or_insert(seconds);
            if seconds - started >= max_burst_seconds {
                state.strobe_hz = 0.0;
            }
        }
    }

    fn limit_movement_speed(&self, seconds: f64, states: &mut FixtureStates) {
        let Some(previous_seconds) = self.last_render_seconds else {
            return;
        };
        let Some(range_per_second) = intensity::movement_range_per_second(self.intensity) else {
            return;
        };
        let max_step = range_per_second * (seconds - previous_seconds);
        for (id, state) in states.iter_mut() {
            if let Some(previous) = self.last_output_states.get(id) {
                state.pan = step_toward(previous.pan, state.pan, max_step);
                state.tilt = step_toward(previous.tilt, state.tilt, max_step);
            }
        }
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
        autoloop: &str,
        transition: &Transition,
    ) -> Result<()> {
        let script = self
            .catalog
            .instantiate(bank, autoloop, &self.rig, &self.palettes)
            .with_context(|| format!("cannot load autoloop {bank}/{autoloop}"))?;

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
            Transition::Cut | Transition::RendererDefault => None,
            Transition::Crossfade { duration_beats } => {
                let source = match (frozen_source, previous) {
                    (Some(states), _) => FadeSource::Frozen(states),
                    (None, Some(previous)) if self.script_error.is_none() => {
                        FadeSource::Running(Box::new(previous))
                    }
                    _ => FadeSource::Frozen(self.dark_states()),
                };
                Some(Crossfade {
                    source,
                    start_beat: time.elapsed_musical_beats,
                    duration_beats: *duration_beats,
                })
            }
        };
        self.active = Some(ActiveAutoloop {
            bank: bank.to_owned(),
            name: autoloop.to_owned(),
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
        let seconds = self.paused_at_seconds.unwrap_or(seconds);
        let palette = self.palette.slots_at(time.elapsed_musical_beats);
        let Some(active) = &mut self.active else {
            return Ok(self.dark_states());
        };
        let effects = active
            .render(
                seconds,
                time,
                self.normalized_rms_level,
                &palette,
                self.intensity,
            )
            .with_context(|| format!("autoloop {}/{} failed", active.bank, active.name))?;
        let destination = states_for_effects(&self.rig, &effects)?;
        let Some(fade) = &mut self.fade else {
            return Ok(destination);
        };
        let source = match &mut fade.source {
            FadeSource::Frozen(states) => states.clone(),
            FadeSource::Running(previous) => {
                let effects = previous.render(
                    seconds,
                    time,
                    self.normalized_rms_level,
                    &palette,
                    self.intensity,
                )?;
                states_for_effects(&self.rig, &effects)?
            }
        };
        let amount =
            ((time.elapsed_musical_beats - fade.start_beat) / fade.duration_beats).clamp(0.0, 1.0);
        Ok(blend_states(&self.rig, source, destination, amount))
    }

    fn set_override(
        &mut self,
        seconds: f64,
        time: ClockSample,
        value: &LightingOverride,
    ) -> Result<()> {
        validate_identifier(&value.id)?;
        validate_properties(&value.values)?;
        if let Some(special) = &value.special_fx {
            special::validate(special)?;
        }
        if let Some(multiplier) = value.intensity_multiplier {
            ensure!(
                multiplier.is_finite() && (0.0..=MAX_INTENSITY_MULTIPLIER).contains(&multiplier),
                "intensity multiplier is outside the supported range"
            );
        }
        let other_active_count = self
            .overrides
            .values()
            .filter(|existing| {
                existing.value.id != value.id
                    && !existing.expired(seconds, time.elapsed_musical_beats)
            })
            .count();
        ensure!(
            other_active_count < MAX_ACTIVE_OVERRIDES,
            "too many active lighting overrides"
        );
        let cell_ids = match &value.target {
            Target::Group(group)
                if value.special_fx.is_some() && !self.rig.groups().contains_key(group) =>
            {
                Vec::new()
            }
            target => self.rig.resolve(target)?,
        };
        for id in &cell_ids {
            self.rig.validate_attributes(id, &value.values.attributes)?;
        }
        let (expires_seconds, expires_beat) = match value.lifetime {
            OverrideLifetime::UntilCleared => (None, None),
            OverrideLifetime::Beats { duration_beats } => (
                None,
                Some(expiry(time.elapsed_musical_beats, duration_beats)?),
            ),
            OverrideLifetime::Seconds { duration_seconds } => {
                (Some(expiry(seconds, duration_seconds)?), None)
            }
        };
        let order = self
            .override_order
            .checked_add(1)
            .context("override ordering exhausted")?;
        self.retire_expired(seconds, time.elapsed_musical_beats);
        let replaced = self.overrides.insert(
            value.id.clone(),
            ActiveOverride {
                value: value.clone(),
                cell_ids,
                order,
                expires_seconds,
                expires_beat,
                progress: Progress::new(time.elapsed_musical_beats, seconds),
            },
        );
        if let Some(replaced) = replaced {
            if let Some(current) = self.overrides.get_mut(&value.id) {
                current.progress.continue_scan(&replaced.progress);
            }
            self.release(replaced, time.elapsed_musical_beats);
        }
        self.override_order = order;
        Ok(())
    }

    fn release(&mut self, value: ActiveOverride, beat: f64) {
        if let Some(releasing) = value.release(beat) {
            if self.releasing.len() >= MAX_ACTIVE_OVERRIDES {
                self.releasing.remove(0);
            }
            self.releasing.push(releasing);
        }
    }

    fn retire_expired(&mut self, seconds: f64, beat: f64) {
        let expired: Vec<_> = self
            .overrides
            .iter()
            .filter(|(_, value)| value.expired(seconds, beat))
            .map(|(id, _)| id.clone())
            .collect();
        for id in expired {
            if let Some(value) = self.overrides.remove(&id) {
                self.release(value, beat);
            }
        }
        self.releasing
            .retain(|releasing| releasing.level(beat) > 0.0);
    }

    fn compose_overrides(&mut self, seconds: f64, time: ClockSample, states: &mut FixtureStates) {
        let beat = time.elapsed_musical_beats;
        self.retire_expired(seconds, beat);
        let palette = self.palette.slots_at(beat);
        let strobe_scale = intensity::strobe_scale(self.intensity);
        let max_pulse_hz = self.strobe_max_rate_hz * strobe_scale;
        let max_gate_seconds = self.strobe_max_burst_seconds * strobe_scale;
        let beats_per_second = self.clock.beats_per_second();
        for value in self.overrides.values_mut() {
            if let Some(special) = &value.value.special_fx {
                value.progress.advance(special, beat, self.intensity);
            }
        }
        for releasing in &mut self.releasing {
            releasing
                .progress
                .advance(&releasing.special, beat, self.intensity);
        }
        let layers = self
            .overrides
            .values()
            .map(|value| {
                let special = value.value.special_fx.as_deref().map(|special| {
                    (
                        special,
                        special::attack_level(special, beat - value.progress.started_beat),
                        value.progress,
                    )
                });
                (
                    (value.value.priority, value.order),
                    &value.cell_ids,
                    Some(&value.value),
                    special,
                )
            })
            .chain(self.releasing.iter().map(|releasing| {
                (
                    (releasing.priority, releasing.order),
                    &releasing.cell_ids,
                    None,
                    Some((
                        &releasing.special,
                        releasing.level(beat),
                        releasing.progress,
                    )),
                )
            }));
        let mut ordered: Vec<_> = layers.collect();
        ordered.sort_by_key(|(rank, ..)| *rank);
        for (_, cell_ids, value, special) in ordered {
            for id in cell_ids {
                let Some(state) = states.get_mut(id) else {
                    continue;
                };
                if let Some(value) = value {
                    overlay_properties(&self.rig, id, state, &value.values);
                    if let Some(multiplier) = value.intensity_multiplier {
                        state.intensity *= multiplier;
                    }
                }
                if let Some((special, level, progress)) = special
                    && let (Some(map), Some(profile)) =
                        (self.special_maps.get(id), self.rig.profile(id))
                {
                    special::apply(
                        state,
                        map,
                        profile,
                        special,
                        &SpecialFrame {
                            level,
                            beat_position: time.beat_position,
                            progress,
                            palette: &palette,
                            speed_scale: self.intensity,
                            beats_per_second,
                            max_pulse_hz,
                            gate_allowed: seconds - progress.started_seconds < max_gate_seconds,
                        },
                    );
                }
                state.intensity = state.intensity.clamp(0.0, 1.0);
            }
        }
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
        self.releasing.clear();
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

pub fn perceptual_to_linear_master(perceptual: f64) -> f64 {
    perceptual * perceptual
}

#[derive(Clone, Debug)]
struct ActivePalette {
    name: String,
    from: PaletteSlots,
    to: PaletteSlots,
    start_beat: f64,
    duration_beats: f64,
}

impl ActivePalette {
    fn steady(name: &str, slots: PaletteSlots) -> Self {
        Self {
            name: name.to_owned(),
            from: slots,
            to: slots,
            start_beat: 0.0,
            duration_beats: 0.0,
        }
    }

    fn slots_at(&self, beat: f64) -> PaletteSlots {
        let progress = if self.duration_beats > 0.0 {
            (beat - self.start_beat) / self.duration_beats
        } else {
            1.0
        };
        if progress >= 1.0 {
            self.to
        } else {
            self.from.blend(&self.to, progress.max(0.0))
        }
    }

    fn retarget(&mut self, beat: f64, name: &str, slots: PaletteSlots, duration_beats: f64) {
        if name == self.name {
            return;
        }
        *self = Self {
            name: name.to_owned(),
            from: self.slots_at(beat),
            to: slots,
            start_beat: beat,
            duration_beats,
        };
    }
}

#[derive(Clone, Copy, Debug)]
struct MasterFade {
    from: f64,
    to: f64,
    start_seconds: f64,
    duration_seconds: f64,
}

impl MasterFade {
    fn steady(level: f64) -> Self {
        Self {
            from: level,
            to: level,
            start_seconds: 0.0,
            duration_seconds: 0.0,
        }
    }

    fn level_at(&self, time_seconds: f64) -> f64 {
        if self.duration_seconds <= 0.0 {
            return self.to;
        }
        let progress =
            ((time_seconds - self.start_seconds) / self.duration_seconds).clamp(0.0, 1.0);
        lerp(self.from, self.to, progress)
    }

    fn retarget(&mut self, time_seconds: f64, to: f64, duration_seconds: f64) {
        *self = Self {
            from: self.level_at(time_seconds),
            to,
            start_seconds: time_seconds,
            duration_seconds,
        };
    }
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

/// Between layers the later one owns every channel it addresses.
fn overlay_properties(
    rig: &FixtureRig,
    id: &str,
    state: &mut FixtureState,
    values: &PropertyValues,
) {
    match rig.profile(id) {
        Some(profile) => profile.overlay_attributes(&mut state.attributes, &values.attributes),
        None => state.attributes.extend(values.attributes.clone()),
    }
    apply_scalar_properties(state, values);
}

/// Within one look two keys on a channel must both survive so the encoder reports the
/// authoring conflict instead of silently picking one.
fn apply_properties(state: &mut FixtureState, values: &PropertyValues) {
    state.attributes.extend(values.attributes.clone());
    apply_scalar_properties(state, values);
}

fn apply_scalar_properties(state: &mut FixtureState, values: &PropertyValues) {
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
    // LuaAutoloop::render has already validated every effect.
    for effect in effects {
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
    rig: &FixtureRig,
    source: FixtureStates,
    mut destination: FixtureStates,
    amount: f64,
) -> FixtureStates {
    for (id, to) in &mut destination {
        if let Some(from) = source.get(id) {
            let profile = rig.profile(id);
            for (key, value) in &from.attributes {
                if let Some(destination) = to.attributes.get(key) {
                    let blended = profile
                        .expect("validated cell state")
                        .interpolate_attribute(key, value, destination, amount);
                    to.attributes.insert(key.clone(), blended);
                } else if amount < 1.0
                    && profile.is_none_or(|profile| !profile.shares_channel(key, &to.attributes))
                {
                    to.attributes.insert(key.clone(), value.clone());
                }
            }
            to.intensity = lerp(from.intensity, to.intensity, amount);
            to.colour = from.colour.lerp(to.colour, amount);
            to.pan = lerp(from.pan, to.pan, amount);
            to.tilt = lerp(from.tilt, to.tilt, amount);
            if amount < 1.0 {
                to.strobe_hz = from.strobe_hz;
            }
        }
    }
    destination
}

fn step_toward(from: f64, to: f64, max_step: f64) -> f64 {
    from + (to - from).clamp(-max_step, max_step)
}

pub fn extinguish(states: &mut FixtureStates) {
    for state in states.values_mut() {
        state.intensity = 0.0;
        state.strobe_hz = 0.0;
        state.output_blackout = true;
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
                state.attribute_brightness = 0.0;
            }
        }
        state.strobe_hz = 0.0;
    }
}
