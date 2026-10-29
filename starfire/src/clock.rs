use anyhow::{Context, Result, ensure};

pub const SECONDS_PER_MINUTE: f64 = 60.0;
pub const MAX_TEMPO_BPM: f64 = 1_000.0;
pub const MAX_SPEED_MULTIPLIER: f64 = 8.0;
pub const MAX_BEATS_PER_BAR: u32 = 16;
const DEFAULT_BEATS_PER_BAR: u32 = 4;
const MAX_F64_EXACT_GRID_BEAT: u64 = 1 << 53;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ClockSample {
    pub beat_position: f64,
    pub elapsed_musical_beats: f64,
    pub animation_beats: f64,
    pub bar_phase: f64,
    pub bar_index: u64,
    pub beats_per_bar: u32,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BeatFact {
    pub beat_index: u64,
    pub bar_phase: Option<u32>,
    pub beats_per_bar: u32,
    pub bpm: f64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ReceivedBeat {
    pub beat_index: u64,
    pub grid_beat: u64,
}

#[derive(Clone, Debug)]
pub struct MusicalClock {
    anchor_seconds: f64,
    anchor_beat: f64,
    anchor_elapsed_musical_beats: f64,
    anchor_animation_beats: f64,
    tempo_bpm: f64,
    speed_multiplier: f64,
    beats_per_bar: u32,
    last_received: Option<ReceivedBeat>,
    paused: bool,
}

impl Default for MusicalClock {
    fn default() -> Self {
        Self {
            anchor_seconds: 0.0,
            anchor_beat: 0.0,
            anchor_elapsed_musical_beats: 0.0,
            anchor_animation_beats: 0.0,
            tempo_bpm: 0.0,
            speed_multiplier: 1.0,
            beats_per_bar: DEFAULT_BEATS_PER_BAR,
            last_received: None,
            paused: false,
        }
    }
}

impl MusicalClock {
    pub fn sample_at(&self, time_seconds: f64) -> Result<ClockSample> {
        validate_time(time_seconds)?;
        ensure!(
            time_seconds >= self.anchor_seconds,
            "time precedes the musical clock anchor"
        );
        let elapsed_beats = if self.paused {
            0.0
        } else {
            (time_seconds - self.anchor_seconds) * self.tempo_bpm / SECONDS_PER_MINUTE
        };
        let beat_position = self.anchor_beat + elapsed_beats;
        let elapsed_musical_beats = self.anchor_elapsed_musical_beats + elapsed_beats;
        let animation_beats = self.anchor_animation_beats + elapsed_beats * self.speed_multiplier;
        ensure!(
            beat_position.is_finite()
                && elapsed_musical_beats.is_finite()
                && animation_beats.is_finite(),
            "musical clock overflow"
        );
        let beats_per_bar = f64::from(self.beats_per_bar);
        let bar_phase = beat_position.rem_euclid(beats_per_bar);
        let bar_start_beat = beat_position - bar_phase;
        let bar_index = (bar_start_beat / beats_per_bar).round();
        ensure!(
            bar_index <= MAX_F64_EXACT_GRID_BEAT as f64,
            "musical clock overflow"
        );
        Ok(ClockSample {
            beat_position,
            elapsed_musical_beats,
            animation_beats,
            bar_phase,
            bar_index: bar_index as u64,
            beats_per_bar: self.beats_per_bar,
        })
    }

    pub fn set_beat(&mut self, time_seconds: f64, beat: BeatFact) -> Result<()> {
        ensure!(
            beat.bpm.is_finite() && beat.bpm > 0.0 && beat.bpm <= MAX_TEMPO_BPM,
            "tempo is outside the supported range"
        );
        ensure!(
            (1..=MAX_BEATS_PER_BAR).contains(&beat.beats_per_bar),
            "beats per bar must be between 1 and {MAX_BEATS_PER_BAR}"
        );
        ensure!(
            beat.bar_phase
                .is_none_or(|phase| phase < beat.beats_per_bar),
            "bar phase must be below beats per bar"
        );
        let counted = match self.last_received {
            Some(last) => {
                ensure!(
                    beat.beat_index > last.beat_index,
                    "beat indices must increase"
                );
                ensure!(
                    beat.beats_per_bar == self.beats_per_bar,
                    "beats per bar is fixed for the session ({})",
                    self.beats_per_bar
                );
                last.grid_beat
                    .checked_add(beat.beat_index - last.beat_index)
                    .context("musical clock overflow")?
            }
            None => 0,
        };
        let grid_beat = match beat.bar_phase {
            Some(phase) => advance_forward_to_bar_phase(counted, phase, beat.beats_per_bar)?,
            None => counted,
        };
        ensure!(
            grid_beat <= MAX_F64_EXACT_GRID_BEAT,
            "musical clock overflow"
        );
        let received = ReceivedBeat {
            beat_index: beat.beat_index,
            grid_beat,
        };
        if self.paused {
            self.tempo_bpm = beat.bpm;
            self.beats_per_bar = beat.beats_per_bar;
            self.last_received = Some(received);
            return Ok(());
        }
        let before = self.sample_at(time_seconds)?;
        self.anchor_seconds = time_seconds;
        self.anchor_beat = grid_beat as f64;
        self.anchor_elapsed_musical_beats = before.elapsed_musical_beats;
        self.anchor_animation_beats = before.animation_beats;
        self.tempo_bpm = beat.bpm;
        self.beats_per_bar = beat.beats_per_bar;
        self.last_received = Some(received);
        Ok(())
    }

    pub fn last_beat(&self) -> Option<ReceivedBeat> {
        self.last_received
    }

    pub fn tempo_bpm(&self) -> f64 {
        self.tempo_bpm
    }

    pub fn beats_per_second(&self) -> f64 {
        if self.paused {
            0.0
        } else {
            self.tempo_bpm / SECONDS_PER_MINUTE
        }
    }

    pub fn paused(&self) -> bool {
        self.paused
    }

    pub fn set_paused(&mut self, time_seconds: f64, paused: bool) -> Result<()> {
        let before = self.sample_at(time_seconds)?;
        self.anchor_seconds = time_seconds;
        self.anchor_beat = before.beat_position;
        self.anchor_elapsed_musical_beats = before.elapsed_musical_beats;
        self.anchor_animation_beats = before.animation_beats;
        self.paused = paused;
        Ok(())
    }

    pub fn set_speed(&mut self, time_seconds: f64, multiplier: f64) -> Result<()> {
        ensure!(
            multiplier.is_finite() && (0.0..=MAX_SPEED_MULTIPLIER).contains(&multiplier),
            "animation speed is outside the supported range"
        );
        let before = self.sample_at(time_seconds)?;
        self.anchor_seconds = time_seconds;
        self.anchor_beat = before.beat_position;
        self.anchor_elapsed_musical_beats = before.elapsed_musical_beats;
        self.anchor_animation_beats = before.animation_beats;
        self.speed_multiplier = multiplier;
        Ok(())
    }
}

fn advance_forward_to_bar_phase(grid_beat: u64, phase: u32, beats_per_bar: u32) -> Result<u64> {
    let length = u64::from(beats_per_bar);
    grid_beat
        .checked_add((u64::from(phase) + length - grid_beat % length) % length)
        .context("musical clock overflow")
}

pub(crate) fn validate_time(time_seconds: f64) -> Result<()> {
    ensure!(
        time_seconds.is_finite() && time_seconds >= 0.0,
        "time must be finite and nonnegative"
    );
    Ok(())
}
