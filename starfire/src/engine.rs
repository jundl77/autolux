use std::cell::RefCell;
use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;

use anyhow::{Context, Result, ensure};
use serde::Serialize;

use crate::clock::{BeatFact, ClockSample, MusicalClock, validate_time};
use crate::cue::{
    CueSample, CueTimeline, DueStep, InstanceState, MAX_OVERLAYS, MAX_PREROLL_BEATS, MAX_ROTATION,
};
use crate::fixtures::FixtureRig;
use crate::gdtf::{GdtfProfile, SNAP_POINT};
use crate::intensity;
use crate::model::{
    AttributeValue, BeatFeatures, Command, CueSection, Effect, FixtureState, FixtureStates,
    LightingOverride, MAX_SPAN_BEATS, MAX_STROBE_HZ, OverrideLifetime, PropertyValues,
    RenderContext, SectionIntent, SpecialFx, Target, Transition, lerp,
};
use crate::palette::{PaletteSlots, PaletteTable};
use crate::protocol::wire;
use crate::script::{AutoloopCatalog, CompiledScripts, LuaAutoloop};
use crate::special::{self, CellMap, Progress, SpecialFrame};

const MAX_ACTIVE_OVERRIDES: usize = 128;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_INTENSITY_MULTIPLIER: f64 = 8.0;
const MAX_STROBE_BURST_SECONDS: f64 = 60.0;
const STROBE_FLASH_SECONDS: f64 = 0.05;
const MAX_STROBE_OPEN_FRACTION: f64 = 0.5;
/// Keeps float rounding from moving a flash edge that falls exactly on a frame time.
const STROBE_EDGE_CYCLES: f64 = 1e-9;
/// Must stay inside even calm's movement slew limit.
const PAN_SWEEP_SECONDS: f64 = 8.0;
pub const MAX_SCRIPT_ERROR_BYTES: usize = 1024;
const ONE_TOLERANCE_BEATS: f64 = 1e-6;
const TRUNCATED_ERROR_SUFFIX: &str = "… [truncated]";

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
    script: Rc<RefCell<LuaAutoloop>>,
    start_beat: f64,
    start_animation_beats: f64,
    seed: u32,
}

impl ActiveAutoloop {
    fn render(
        &mut self,
        frame: &RenderContext,
        time: ClockSample,
        palette: &PaletteSlots,
    ) -> Result<Vec<Effect>> {
        self.script.borrow_mut().render(
            &RenderContext {
                autoloop_elapsed_beats: (time.beat_position - self.start_beat).max(0.0),
                autoloop_speed_scaled_elapsed_beats: (time.animation_beats
                    - self.start_animation_beats)
                    .max(0.0),
                seed: self.seed,
                ..*frame
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
    posed: Option<FixtureRig>,
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
        let level = special
            .envelope
            .level(self.progress.started_beat, None, beat);
        (special.envelope.release_beats > 0.0 && level > 0.0).then_some(ReleasingSpecial {
            id: self.value.id,
            special,
            cell_ids: self.cell_ids,
            priority: self.value.priority,
            order: self.order,
            progress: self.progress,
            released_beat: beat,
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
}

impl ReleasingSpecial {
    fn level(&self, beat: f64) -> f64 {
        self.special
            .envelope
            .level(self.progress.started_beat, Some(self.released_beat), beat)
    }
}

pub struct Engine {
    rig: FixtureRig,
    catalog: AutoloopCatalog,
    scripts: CompiledScripts,
    cues: CueTimeline,
    features: Option<BeatFeatures>,
    phrase_start: Option<f64>,
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
    strobe_gates: StrobeGates,
    cell_rests: BTreeMap<String, BTreeMap<String, AttributeValue>>,
    overlay_scripts: BTreeMap<u64, OverlayScript>,
    refused_overlay_strobes: BTreeSet<String>,
    palette: ActivePalette,
    intensity: f64,
    last_render_seconds: Option<f64>,
}

impl Engine {
    pub fn new(rig: FixtureRig, catalog: AutoloopCatalog, palettes: PaletteTable) -> Result<Self> {
        let last_output_states = rig.empty_states();
        let special_maps = special::cell_maps(&rig);
        rig.encode(&last_output_states)?;
        let palette =
            ActivePalette::steady(palettes.initial(), palettes.slots(palettes.initial())?);
        let scripts = catalog.compile(&rig, &palettes);
        let cell_rests = rig
            .cell_ids()
            .into_iter()
            .filter_map(|id| {
                let rests = rig
                    .profile(&id)?
                    .functions()
                    .into_iter()
                    .map(|function| (function.key, AttributeValue::Number(function.rest)))
                    .collect();
                Some((id, rests))
            })
            .collect();
        Ok(Self {
            rig,
            catalog,
            scripts,
            cues: CueTimeline::default(),
            features: None,
            phrase_start: None,
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
            strobe_gates: StrobeGates::default(),
            cell_rests,
            overlay_scripts: BTreeMap::new(),
            refused_overlay_strobes: BTreeSet::new(),
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
                features,
            } => {
                if let Some(features) = features {
                    for (name, value) in [
                        ("onset", features.onset),
                        ("bass", features.bass),
                        ("mid", features.mid),
                        ("high", features.high),
                    ] {
                        validate_unit(value, name)?;
                    }
                }
                self.clock.set_beat(
                    at_seconds,
                    BeatFact {
                        beat_index: *beat_index,
                        bar_phase: *bar_phase,
                        beats_per_bar: *beats_per_bar,
                        bpm: *bpm,
                    },
                )?;
                self.features = *features;
            }
            Command::CueSection { value } => {
                self.validate_cue(value, time)?;
                self.cues
                    .receive((**value).clone(), self.cue_sample(time))?;
            }
            Command::ClearCues => {
                self.cues.clear(time.elapsed_musical_beats, true);
                self.features = None;
                self.phrase_start = Some(bar_line_at_or_before(time));
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
                if !self.is_playing(bank, autoloop) {
                    self.select(at_seconds, time, bank, autoloop, transition)?;
                    self.phrase_start = Some(bar_line_at_or_before(time));
                }
                self.cues.clear(time.elapsed_musical_beats, true);
                self.features = None;
                self.palette.retarget(
                    time.elapsed_musical_beats,
                    palette,
                    slots,
                    transition.crossfade_beats(),
                );
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
                instantaneous_release,
            } => {
                validate_identifier(id)?;
                let removed = self.overrides.remove(id);
                if *instantaneous_release {
                    self.releasing.retain(|releasing| releasing.id != *id);
                } else if let Some(removed) = removed {
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
            Command::SetPaused { paused } => {
                self.set_paused(at_seconds, *paused)?;
                if *paused {
                    self.cues.drop_releasing();
                }
            }
            Command::SetDefaultCrossfadeBeats { beats } => {
                ensure!(
                    beats.is_finite() && (0.0..=MAX_SPAN_BEATS).contains(beats),
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
                self.cues.clear(time.elapsed_musical_beats, false);
                self.phrase_start = None;
                self.features = None;
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

    pub fn check_scripts_pure(&self) -> Result<()> {
        let time = self.clock.sample_at(0.0)?;
        let context = RenderContext {
            overlay_duration_beats: Some(1.0),
            ..self.frame_context(0.0, time)
        };
        self.scripts
            .check_pure(&context, &self.palette.slots_at(time.elapsed_musical_beats))
    }

    pub fn resolve_autoloop(&self, autoloop_id: &str) -> Result<(String, String)> {
        self.catalog.resolve(autoloop_id).cloned()
    }

    pub fn pending_cue_count(&self) -> usize {
        self.cues.pending_count()
    }

    pub fn active_overlay_count(&self) -> usize {
        self.cues.active_overlay_count()
    }

    pub fn overlay_error(&self) -> Option<&str> {
        self.cues.overlay_error()
    }

    pub fn sounding_section(&self) -> Result<(Option<SectionIntent>, Vec<String>)> {
        let now = self.cue_sample(self.clock.sample_at(self.last_time_seconds)?);
        let overlays = self
            .cues
            .instances()
            .iter()
            .filter(|instance| !matches!(instance.state, InstanceState::Pending))
            .map(|instance| format!("{}/{}", instance.overlay.bank, instance.overlay.autoloop))
            .collect();
        Ok((self.cues.context(&now).section_intent, overlays))
    }

    fn cue_sample(&self, time: ClockSample) -> CueSample {
        CueSample {
            beat_position: time.beat_position,
            elapsed_musical_beats: time.elapsed_musical_beats,
            beats_per_bar: time.beats_per_bar,
            anchor: self.clock.last_beat(),
        }
    }

    fn frame_context(&self, seconds: f64, time: ClockSample) -> RenderContext {
        let section = self.cues.context(&self.cue_sample(time));
        let phrase = self
            .phrase_start
            .map(|start| (time.beat_position - start).max(0.0));
        let section_elapsed_beats = section.section_elapsed_beats.or(phrase);
        let next_known_at =
            section_elapsed_beats
                .zip(section.next_received_at)
                .map(|(elapsed, received)| {
                    (elapsed - (time.elapsed_musical_beats - received)).max(0.0)
                });
        let features = self.features;
        RenderContext {
            time_seconds: self.paused_at_seconds.unwrap_or(seconds),
            beat_position: time.beat_position,
            bar_phase: time.bar_phase,
            bar_index: time.bar_index,
            beats_per_bar: time.beats_per_bar,
            autoloop_elapsed_beats: 0.0,
            autoloop_speed_scaled_elapsed_beats: 0.0,
            normalized_rms_level: self.normalized_rms_level,
            intensity: self.intensity,
            bpm: self.clock.tempo_bpm(),
            strobe_max_hz: self.strobe_rate_cap_hz(),
            section_intent: section.section_intent,
            section_elapsed_beats,
            next_intent: section.next_intent,
            beats_until_next: section.beats_until_next,
            beats_until_dip: section.beats_until_dip,
            next_known_at,
            drop_strength: section.drop_strength,
            next_drop_strength: section.next_drop_strength,
            seed: section.seed,
            onset: features.map(|features| features.onset),
            bass: features.map(|features| features.bass),
            mid: features.map(|features| features.mid),
            high: features.map(|features| features.high),
            overlay_duration_beats: None,
        }
    }

    fn advance_cues(&mut self, seconds: f64, time: ClockSample) -> Result<()> {
        let Some(step) = self.cues.advance(self.cue_sample(time)) else {
            return Ok(());
        };
        let DueStep {
            bank,
            autoloop,
            transition,
            begin_beat,
            seed,
            palette,
        } = step;
        if palette.is_some() {
            self.phrase_start = None;
        }
        let transition = self.resolve_transition(&transition);
        if !self.is_playing(&bank, &autoloop) {
            let script = self.scripts.get(&bank, &autoloop)?;
            let behind = (time.beat_position - begin_beat).max(0.0);
            self.begin_base(
                seconds,
                time,
                &bank,
                &autoloop,
                script,
                &transition,
                begin_beat,
                time.animation_beats - behind,
                seed,
            )?;
        }
        if let Some(palette) = palette {
            match self.palettes.slots(&palette) {
                Ok(slots) => self.palette.retarget(
                    time.elapsed_musical_beats,
                    &palette,
                    slots,
                    transition.crossfade_beats(),
                ),
                Err(error) => self
                    .cues
                    .record_overlay_error(format!("cue palette not applied: {error:#}")),
            }
        }
        Ok(())
    }

    fn validate_cue(&self, cue: &CueSection, time: ClockSample) -> Result<()> {
        ensure!(
            (1..=MAX_ROTATION).contains(&cue.rotation.len()),
            "a cue needs 1..{MAX_ROTATION} steps"
        );
        ensure!(
            cue.overlays.len() <= MAX_OVERLAYS,
            "a cue carries at most {MAX_OVERLAYS} overlays"
        );
        self.palettes.slots(&cue.palette)?;
        if let Some(strength) = cue.drop_strength {
            ensure!(
                cue.intent == SectionIntent::Drop,
                "only a drop cue carries a drop strength"
            );
            validate_unit(strength, "drop strength")?;
        }
        for (index, step) in cue.rotation.iter().enumerate() {
            validate_identifier(&step.bank)?;
            validate_identifier(&step.autoloop)?;
            ensure!(
                step.bars >= 1 || index + 1 == cue.rotation.len(),
                "only the last step may hold with 0 bars"
            );
            validate_transition(
                &self.resolve_transition(&step.transition),
                time.elapsed_musical_beats,
            )?;
            self.load_script(&step.bank, &step.autoloop)?;
        }
        let bar = f64::from(time.beats_per_bar);
        for overlay in &cue.overlays {
            validate_identifier(&overlay.bank)?;
            validate_identifier(&overlay.autoloop)?;
            ensure!(
                overlay.duration_beats.is_finite()
                    && overlay.duration_beats > 0.0
                    && overlay.duration_beats <= MAX_SPAN_BEATS,
                "overlay length is outside the supported range"
            );
            ensure!(
                overlay.envelope.is_valid(),
                "overlay attack and release are outside the supported range"
            );
            let period = f64::from(overlay.every_bars) * bar;
            let begins = if overlay.every_bars == 0 {
                (-MAX_PREROLL_BEATS..=MAX_SPAN_BEATS).contains(&overlay.begin_beats)
            } else {
                (0.0..period).contains(&overlay.begin_beats)
            };
            ensure!(
                overlay.begin_beats.is_finite() && begins,
                "overlay begin is outside its pre-roll or repeat period"
            );
            ensure!(
                overlay.every_bars == 0
                    || overlay.duration_beats + overlay.envelope.release_beats <= period,
                "a repeating overlay's length and release exceed its period"
            );
            ensure!(
                !overlay.dip || (overlay.every_bars == 0 && overlay.begin_beats < 0.0),
                "a dip plays once before its section"
            );
            self.load_script(&overlay.bank, &overlay.autoloop)?;
        }
        Ok(())
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
        if let Err(error) = self.advance_cues(time_seconds, time) {
            self.stop_on_script_error(&error);
        }
        let frame = self.frame_context(time_seconds, time);
        let mut states = if self.script_error.is_some() {
            self.dark_states()
        } else {
            match self.render_baseline(&frame, time) {
                Ok(states) => states,
                Err(error) => {
                    self.stop_on_script_error(&error);
                    self.dark_states()
                }
            }
        };

        if self.script_error.is_none() {
            self.compose_overlays(&frame, time_seconds, time, &mut states);
            self.compose_overrides(time_seconds, time, &mut states);
            let master = perceptual_to_linear_master(self.master.level_at(time_seconds));
            for state in states.values_mut() {
                state.intensity = state.intensity.clamp(0.0, 1.0) * master;
                state.attribute_brightness = master;
            }
            for (group, level) in &self.group_masters {
                let level = perceptual_to_linear_master(*level);
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
        // Pause darkens overrides and static looks too, on purpose.
        if self.blackout || self.clock.paused() {
            extinguish(&mut states);
        }

        let mut encoded_states = states.clone();
        apply_software_strobe(
            &mut self.strobe_gates,
            time_seconds,
            time,
            self.clock.beats_per_second(),
            &mut encoded_states,
        );
        let rig = self
            .overrides
            .values()
            .filter_map(|active| active.posed.as_ref().map(|rig| (active.order, rig)))
            .max_by_key(|(order, _)| *order)
            .map_or(&self.rig, |(_, rig)| rig);
        let universes = match rig.encode(&encoded_states) {
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

    fn strobe_rate_cap_hz(&self) -> f64 {
        self.scaled_by_strobe_intensity(self.strobe_max_rate_hz)
    }

    fn scaled_by_strobe_intensity(&self, cap: f64) -> f64 {
        let strobe_scale = intensity::strobe_scale(self.intensity);
        if strobe_scale > 0.0 {
            cap * strobe_scale
        } else {
            0.0
        }
    }

    fn is_playing(&self, bank: &str, autoloop: &str) -> bool {
        self.active
            .as_ref()
            .is_some_and(|active| active.bank == bank && active.name == autoloop)
    }

    fn load_script(&self, bank: &str, autoloop: &str) -> Result<Rc<RefCell<LuaAutoloop>>> {
        self.scripts
            .get(bank, autoloop)
            .with_context(|| format!("cannot load autoloop {bank}/{autoloop}"))
    }

    fn limit_strobes(&mut self, seconds: f64, states: &mut FixtureStates) {
        let paused = self.clock.paused();
        let max_rate_hz = self.strobe_rate_cap_hz();
        let max_burst_seconds = self.scaled_by_strobe_intensity(self.strobe_max_burst_seconds);
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
            // Past its burst a strobe stays dark, not steady, until it is no longer asked for.
            if seconds - started >= max_burst_seconds {
                state.strobe_hz = 0.0;
                state.intensity = 0.0;
                state.attribute_brightness = 0.0;
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
            // A dark head skips the slew limit so it moves in black.
            if state.intensity <= 0.0 {
                continue;
            }
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
        let script = self.load_script(bank, autoloop)?;
        self.begin_base(
            seconds,
            time,
            bank,
            autoloop,
            script,
            transition,
            time.beat_position,
            time.animation_beats,
            0,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn begin_base(
        &mut self,
        seconds: f64,
        time: ClockSample,
        bank: &str,
        autoloop: &str,
        script: Rc<RefCell<LuaAutoloop>>,
        transition: &Transition,
        start_beat: f64,
        start_animation_beats: f64,
        seed: u32,
    ) -> Result<()> {
        let frozen_source = if matches!(transition, Transition::Crossfade { .. })
            && self.fade.is_some()
            && self.script_error.is_none()
        {
            match self.render_baseline(&self.frame_context(seconds, time), time) {
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
            start_beat,
            start_animation_beats,
            seed,
        });
        self.script_error = None;
        Ok(())
    }

    fn render_baseline(
        &mut self,
        frame: &RenderContext,
        time: ClockSample,
    ) -> Result<FixtureStates> {
        if self
            .fade
            .as_ref()
            .is_some_and(|fade| time.elapsed_musical_beats >= fade.start_beat + fade.duration_beats)
        {
            self.fade = None;
        }
        let palette = self.palette.slots_at(time.elapsed_musical_beats);
        let Some(active) = &mut self.active else {
            return Ok(self.dark_states());
        };
        let effects = active
            .render(frame, time, &palette)
            .with_context(|| format!("autoloop {}/{} failed", active.bank, active.name))?;
        let destination = states_for_effects(&self.rig, &effects)?;
        let Some(fade) = &mut self.fade else {
            return Ok(destination);
        };
        let source = match &mut fade.source {
            FadeSource::Frozen(states) => states.clone(),
            FadeSource::Running(previous) => {
                let effects = previous.render(frame, time, &palette)?;
                states_for_effects(&self.rig, &effects)?
            }
        };
        let amount =
            ((time.elapsed_musical_beats - fade.start_beat) / fade.duration_beats).clamp(0.0, 1.0);
        Ok(blend_states(&self.rig, source, destination, amount))
    }

    fn compose_overlays(
        &mut self,
        frame: &RenderContext,
        seconds: f64,
        time: ClockSample,
        states: &mut FixtureStates,
    ) {
        let instances = self.cues.instances();
        self.overlay_scripts
            .retain(|order, _| instances.iter().any(|instance| instance.order == *order));
        if instances.is_empty() {
            self.refused_overlay_strobes.clear();
            return;
        }
        let beat = time.elapsed_musical_beats;
        let sample = self.cue_sample(time);
        let one = self.cues.next_cue(&sample).map(|(_, at)| at);
        let mut layers = Vec::with_capacity(instances.len());
        let mut unloadable = Vec::new();
        for instance in instances {
            let Some(elapsed_beats) = instance.elapsed_beats(beat) else {
                continue;
            };
            let overlay = &instance.overlay;
            let script = match self.overlay_scripts.entry(instance.order) {
                Entry::Occupied(entry) => entry.into_mut(),
                Entry::Vacant(entry) => match self.scripts.get(&overlay.bank, &overlay.autoloop) {
                    Ok(script) => entry.insert(OverlayScript {
                        script,
                        intensity_htp: self.catalog.intensity_htp(&overlay.bank, &overlay.autoloop),
                    }),
                    Err(error) => {
                        unloadable.push((instance.order, error));
                        continue;
                    }
                },
            };
            layers.push(OverlayLayer {
                rank: (overlay.priority, instance.order),
                script: Rc::clone(&script.script),
                intensity_htp: script.intensity_htp,
                duration_beats: overlay.duration_beats,
                elapsed_beats,
                seed: instance.seed,
                level: instance.level(beat),
                holds_to_one: overlay.dip
                    && matches!(instance.state, InstanceState::Running(_))
                    && instance
                        .nominal_end(&sample)
                        .zip(one)
                        .is_some_and(|(end, one)| end + ONE_TOLERANCE_BEATS >= one),
            });
        }
        for (order, error) in unloadable {
            self.fail_overlay(order, &error);
        }
        layers.sort_by_key(|layer| layer.rank);
        let palette = self.palette.slots_at(beat);
        let max_burst_seconds = self.scaled_by_strobe_intensity(self.strobe_max_burst_seconds);
        let mut refused_strobes = BTreeSet::new();
        let mut held_dark = BTreeSet::new();
        for layer in &layers {
            if layer.level <= 0.0 {
                continue;
            }
            let cells = match self.render_overlay(layer, frame, &palette) {
                Ok(cells) => cells,
                Err(error) => {
                    self.fail_overlay(layer.rank.1, &error);
                    continue;
                }
            };
            for (id, values) in cells {
                if values.strobe_hz.is_some_and(|hz| hz > 0.0)
                    && (max_burst_seconds <= 0.0
                        || self.refused_overlay_strobes.contains(&id)
                        || self
                            .strobe_started_seconds
                            .get(&id)
                            .is_some_and(|started| seconds - started >= max_burst_seconds))
                {
                    refused_strobes.insert(id);
                    continue;
                }
                let Some(state) = states.get_mut(&id) else {
                    continue;
                };
                compose_layer(
                    self.rig.profile(&id).zip(self.cell_rests.get(&id)),
                    state,
                    &values,
                    layer.level,
                    layer.intensity_htp,
                );
                if layer.holds_to_one && !layer.intensity_htp && values.intensity.is_some() {
                    held_dark.insert(id);
                }
            }
        }
        self.refused_overlay_strobes = refused_strobes;
        if let Err(error) = self.move_in_black(frame, &sample, states, &held_dark) {
            self.cues
                .record_overlay_error(format!("move in black failed: {error:#}"));
        }
    }

    /// Never sets script_error: the supervisor would restart the show on it.
    fn fail_overlay(&mut self, order: u64, error: &anyhow::Error) {
        let message = self
            .cues
            .instances()
            .iter()
            .find(|instance| instance.order == order)
            .map(|instance| {
                format!(
                    "overlay {}/{} failed: {error:#}",
                    instance.overlay.bank, instance.overlay.autoloop
                )
            });
        if let Some(message) = message {
            self.cues.record_overlay_error(message);
        }
        self.cues.stop_row(order);
    }

    fn render_overlay(
        &self,
        layer: &OverlayLayer,
        frame: &RenderContext,
        palette: &PaletteSlots,
    ) -> Result<BTreeMap<String, PropertyValues>> {
        let effects = layer.script.borrow_mut().render(
            &RenderContext {
                autoloop_elapsed_beats: layer.elapsed_beats,
                // The envelope runs on musical beats; a speed change must not detach it.
                autoloop_speed_scaled_elapsed_beats: layer.elapsed_beats,
                overlay_duration_beats: Some(layer.duration_beats),
                seed: layer.seed,
                ..*frame
            },
            palette,
        )?;
        layer_values(&self.rig, &effects)
    }

    /// Only under a dip row, and only when the next cue cuts to a new loop.
    fn move_in_black(
        &self,
        frame: &RenderContext,
        sample: &CueSample,
        states: &mut FixtureStates,
        held_dark: &BTreeSet<String>,
    ) -> Result<()> {
        let overridden: BTreeSet<&str> = if held_dark.is_empty() {
            BTreeSet::new()
        } else {
            self.overrides
                .values()
                .flat_map(|active| &active.cell_ids)
                .chain(
                    self.releasing
                        .iter()
                        .flat_map(|releasing| &releasing.cell_ids),
                )
                .map(String::as_str)
                .collect()
        };
        let dark: Vec<&String> = held_dark
            .iter()
            .filter(|id| {
                !overridden.contains(id.as_str())
                    && states.get(*id).is_some_and(|state| state.intensity <= 0.0)
            })
            .collect();
        if dark.is_empty() {
            return Ok(());
        }
        let Some((cue, one)) = self.cues.next_cue(sample) else {
            return Ok(());
        };
        let first = &cue.rotation[0];
        let cuts_to_new_loop = self.resolve_transition(&first.transition) == Transition::Cut
            && !self.is_playing(&first.bank, &first.autoloop);
        if !cuts_to_new_loop {
            return Ok(());
        }
        let bar = f64::from(frame.beats_per_bar);
        let effects = self
            .scripts
            .get(&first.bank, &first.autoloop)?
            .borrow_mut()
            .render(
                &RenderContext {
                    beat_position: one,
                    bar_phase: one.rem_euclid(bar),
                    bar_index: (one / bar).floor() as u64,
                    autoloop_elapsed_beats: 0.0,
                    autoloop_speed_scaled_elapsed_beats: 0.0,
                    section_intent: Some(cue.intent),
                    section_elapsed_beats: Some(0.0),
                    next_intent: None,
                    beats_until_next: None,
                    drop_strength: cue.drop_strength,
                    next_drop_strength: None,
                    seed: cue.seed,
                    ..*frame
                },
                &self.palettes.slots(&cue.palette)?,
            )?;
        let cells = layer_values(&self.rig, &effects)?;
        for id in dark {
            let (Some(values), Some(state)) = (cells.get(id), states.get_mut(id)) else {
                continue;
            };
            let positions = PropertyValues {
                intensity: None,
                strobe_hz: None,
                ..values.clone()
            };
            replace_properties(&self.rig, id, state, &positions);
        }
        Ok(())
    }

    fn set_override(
        &mut self,
        seconds: f64,
        time: ClockSample,
        value: &LightingOverride,
    ) -> Result<()> {
        validate_identifier(&value.id)?;
        validate_properties(&value.values)?;
        ensure!(
            !((value.pan_sweep || value.aim_focus)
                && (value.values.pan.is_some() || value.values.tilt.is_some())),
            "a pan sweep or focus aim replaces pan and tilt"
        );
        ensure!(
            !(value.pan_sweep && value.aim_focus),
            "a pan sweep and a focus aim exclude each other"
        );
        let posed = match (&value.pose, &value.target) {
            (None, _) => None,
            (Some(pose), Target::Fixture(fixture)) => Some(self.rig.posed(fixture, pose)?),
            (Some(_), _) => anyhow::bail!("a pose needs a whole-fixture target"),
        };
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
                posed,
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
        let max_gate_seconds = self.scaled_by_strobe_intensity(self.strobe_max_burst_seconds);
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
                        special
                            .envelope
                            .level(value.progress.started_beat, None, beat),
                        value.progress,
                    )
                });
                (
                    (value.value.priority, value.order),
                    &value.cell_ids,
                    Some((&value.value, value.posed.as_ref().unwrap_or(&self.rig))),
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
                if let Some((value, rig)) = value {
                    replace_properties(&self.rig, id, state, &value.values);
                    if (value.pan_sweep || value.aim_focus)
                        && let Some((pan, tilt)) = rig.aim(id)
                    {
                        state.tilt = tilt;
                        state.pan = if value.pan_sweep {
                            pan_sweep_position(seconds)
                        } else {
                            pan
                        };
                    }
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
                && *duration_beats <= MAX_SPAN_BEATS,
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

fn bar_line_at_or_before(time: ClockSample) -> f64 {
    let bar = f64::from(time.beats_per_bar);
    (time.beat_position / bar).floor() * bar
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

fn pan_sweep_position(seconds: f64) -> f64 {
    let phase = (seconds / PAN_SWEEP_SECONDS).rem_euclid(1.0);
    1.0 - (2.0 * phase - 1.0).abs()
}

fn replace_properties(
    rig: &FixtureRig,
    id: &str,
    state: &mut FixtureState,
    values: &PropertyValues,
) {
    match rig.profile(id) {
        Some(profile) => profile.replace_attributes(&mut state.attributes, &values.attributes),
        None => state.attributes.extend(values.attributes.clone()),
    }
    apply_scalar_properties(state, values);
}

/// Keeps two keys on one channel, unlike `replace_properties`, so the encoder reports the conflict.
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

struct OverlayScript {
    script: Rc<RefCell<LuaAutoloop>>,
    intensity_htp: bool,
}

struct OverlayLayer {
    rank: (i32, u64),
    script: Rc<RefCell<LuaAutoloop>>,
    intensity_htp: bool,
    duration_beats: f64,
    elapsed_beats: f64,
    seed: u32,
    level: f64,
    holds_to_one: bool,
}

fn layer_values(rig: &FixtureRig, effects: &[Effect]) -> Result<BTreeMap<String, PropertyValues>> {
    let mut cells: BTreeMap<String, PropertyValues> = BTreeMap::new();
    for effect in effects {
        for id in rig.resolve_for_loop(&effect.target)? {
            let values = cells.entry(id).or_default();
            let set = &effect.values;
            values.intensity = set.intensity.or(values.intensity);
            values.colour = set.colour.or(values.colour);
            values.pan = set.pan.or(values.pan);
            values.tilt = set.tilt.or(values.tilt);
            values.strobe_hz = set.strobe_hz.or(values.strobe_hz);
            values.attributes.extend(set.attributes.clone());
        }
    }
    Ok(cells)
}

fn compose_layer(
    cell: Option<(&GdtfProfile, &BTreeMap<String, AttributeValue>)>,
    state: &mut FixtureState,
    values: &PropertyValues,
    level: f64,
    intensity_htp: bool,
) {
    let mut mix = level;
    if let Some(intensity) = values.intensity {
        if intensity_htp {
            let lit = intensity * level;
            let output = state.intensity.max(lit);
            mix = if output > 0.0 { lit / output } else { 0.0 };
            state.intensity = output;
        } else {
            state.intensity = lerp(state.intensity, intensity, level);
        }
    }
    if let Some(colour) = values.colour {
        state.colour = match cell {
            Some((profile, _)) => {
                let amount = if profile.wheel_only_colour() {
                    level
                } else {
                    mix
                };
                profile.blend_colour(state.colour, colour, amount)
            }
            None => state.colour.lerp(colour, mix),
        };
    }
    if let Some(pan) = values.pan {
        state.pan = lerp(state.pan, pan, level);
    }
    if let Some(tilt) = values.tilt {
        state.tilt = lerp(state.tilt, tilt, level);
    }
    if let Some(rate) = values.strobe_hz
        && level >= SNAP_POINT
    {
        state.strobe_hz = rate;
    }
    if values.attributes.is_empty() {
        return;
    }
    let Some((profile, rests)) = cell else {
        if level >= SNAP_POINT {
            state.attributes.extend(values.attributes.clone());
        }
        return;
    };
    let mut layer = BTreeMap::new();
    for (key, to) in &values.attributes {
        let below = state.attributes.get(key);
        let Some(from) = below.or_else(|| rests.get(key)) else {
            if level >= SNAP_POINT {
                layer.insert(key.clone(), to.clone());
            }
            continue;
        };
        let blended = profile.interpolate_attribute(key, from, to, level);
        if below.is_some() || blended != *from {
            layer.insert(key.clone(), blended);
        }
    }
    profile.replace_attributes(&mut state.attributes, &layer);
}

fn states_for_effects(rig: &FixtureRig, effects: &[Effect]) -> Result<FixtureStates> {
    let mut states = rig.empty_states();
    for effect in effects {
        for id in rig.resolve_for_loop(&effect.target)? {
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
            let profile = rig.profile(id).expect("validated cell state");
            to.attributes = profile.blend_attributes(&from.attributes, &to.attributes, amount);
            to.intensity = lerp(from.intensity, to.intensity, amount);
            to.colour = profile.blend_colour(from.colour, to.colour, amount);
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

#[derive(Default)]
struct StrobeGates {
    by_rate: BTreeMap<u64, StrobeGate>,
    rate_of_cell: BTreeMap<String, u64>,
    bar_index: Option<u64>,
}

#[derive(Clone, Copy, PartialEq)]
enum StrobeTimeline {
    Beats,
    Seconds,
}

/// A rate or tempo change re-anchors from the phase reached: a phase of time x rate would gate
/// at rate + time x d(rate)/dt and alias past the cap. A negative phase holds the gate closed.
#[derive(Clone, Copy)]
struct StrobeGate {
    timeline: StrobeTimeline,
    position: f64,
    cycles: f64,
    cycles_per_unit: f64,
    open: f64,
}

impl StrobeGate {
    fn cycles_at(&self, position: f64) -> f64 {
        self.cycles + self.cycles_per_unit * (position - self.position)
    }

    fn grid(timeline: StrobeTimeline, position: f64, cycles_per_unit: f64) -> f64 {
        match timeline {
            StrobeTimeline::Beats => (position * cycles_per_unit).rem_euclid(1.0),
            StrobeTimeline::Seconds => 0.0,
        }
    }

    fn realign(&mut self, position: f64) {
        let phase = self.cycles_at(position);
        let grid = Self::grid(self.timeline, position, self.cycles_per_unit);
        let since_flash = phase.rem_euclid(1.0);
        let offset = (since_flash - grid).rem_euclid(1.0);
        if phase < 0.0 || offset.min(1.0 - offset) <= STROBE_EDGE_CYCLES {
            return;
        }
        let relights = grid < self.open && since_flash >= self.open;
        self.position = position;
        self.cycles = if grid > since_flash {
            grid - 2.0
        } else if relights {
            grid - 1.0
        } else {
            grid
        };
    }
}

fn apply_software_strobe(
    gates: &mut StrobeGates,
    seconds: f64,
    time: ClockSample,
    beats_per_second: f64,
    states: &mut FixtureStates,
) {
    let (timeline, position, units_per_second) = if beats_per_second > 0.0 {
        (StrobeTimeline::Beats, time.beat_position, beats_per_second)
    } else {
        (StrobeTimeline::Seconds, seconds, 1.0)
    };
    if gates.bar_index.is_some_and(|bar| bar != time.bar_index) {
        for gate in gates.by_rate.values_mut() {
            if gate.timeline == timeline {
                gate.realign(position);
            }
        }
    }
    let mut next = StrobeGates {
        bar_index: Some(time.bar_index),
        ..StrobeGates::default()
    };
    for (id, state) in states.iter_mut() {
        let hz = state.strobe_hz;
        state.strobe_hz = 0.0;
        if hz <= 0.0 {
            continue;
        }
        let rate = hz.to_bits();
        let cycles_per_unit = hz / units_per_second;
        let open = (STROBE_FLASH_SECONDS * hz).min(MAX_STROBE_OPEN_FRACTION);
        let gate = *next.by_rate.entry(rate).or_insert_with(|| {
            let previous = gates.by_rate.get(&rate).or_else(|| {
                gates
                    .rate_of_cell
                    .get(id)
                    .and_then(|previous| gates.by_rate.get(previous))
            });
            match previous {
                Some(gate)
                    if gate.timeline == timeline && gate.cycles_per_unit == cycles_per_unit =>
                {
                    *gate
                }
                Some(gate) if gate.timeline == timeline => {
                    let cycles = gate.cycles_at(position);
                    StrobeGate {
                        timeline,
                        position,
                        cycles: if cycles < 0.0 {
                            cycles
                        } else {
                            cycles.rem_euclid(1.0)
                        },
                        cycles_per_unit,
                        open,
                    }
                }
                _ => StrobeGate {
                    timeline,
                    position,
                    cycles: StrobeGate::grid(timeline, position, cycles_per_unit),
                    cycles_per_unit,
                    open,
                },
            }
        });
        next.rate_of_cell.insert(id.clone(), rate);
        let phase = gate.cycles_at(position) + STROBE_EDGE_CYCLES;
        if phase < 0.0 || phase.rem_euclid(1.0) >= gate.open {
            state.intensity = 0.0;
            state.attribute_brightness = 0.0;
        }
    }
    *gates = next;
}
