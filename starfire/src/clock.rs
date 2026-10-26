//! Musical time is derived from supplied beat facts; sampling never advances it.

use anyhow::{Context, Result, ensure};

const SECONDS_PER_MINUTE: f64 = 60.0;
pub const MAX_TEMPO_BPM: f64 = 1_000.0;
pub const MAX_SPEED_MULTIPLIER: f64 = 8.0;
pub const MAX_BEATS_PER_BAR: u32 = 16;
const DEFAULT_BEATS_PER_BAR: u32 = 4;
// Grid beats stay exact in the f64 position sent to scripts.
const MAX_GRID_BEAT: u64 = 1 << 53;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ClockSample {
    /// Bar-aligned grid: `floor(beat_position) % beats_per_bar` is the beat in bar.
    pub beat_position: f64,
    pub elapsed_musical_beats: f64,
    pub animation_beats: f64,
    /// Continuous position inside the current bar, `0 <= bar_phase < beats_per_bar`.
    pub bar_phase: f64,
    pub bar_index: u64,
    pub beats_per_bar: u32,
}

/// One detected beat as reported by the controller's tracker.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BeatFact {
    pub beat_index: u64,
    /// Committed beat in bar, 0 = downbeat; `None` while the tracker has no phase.
    pub bar_phase: Option<u32>,
    pub beats_per_bar: u32,
    pub bpm: f64,
}

#[derive(Clone, Copy, Debug)]
struct ReceivedBeat {
    beat_index: u64,
    grid_beat: u64,
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
        let elapsed_beats =
            (time_seconds - self.anchor_seconds) * self.tempo_bpm / SECONDS_PER_MINUTE;
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
        // The subtraction is an exact multiple of the bar length, so the bar index
        // cannot disagree with bar_phase at a bar edge the way a plain division can.
        let bar_index = ((beat_position - bar_phase) / beats_per_bar).round();
        ensure!(bar_index <= MAX_GRID_BEAT as f64, "musical clock overflow");
        Ok(ClockSample {
            beat_position,
            elapsed_musical_beats,
            animation_beats,
            bar_phase,
            bar_index: bar_index as u64,
            beats_per_bar: self.beats_per_bar,
        })
    }

    /// Anchor the grid at a detected beat so `floor(position) % beats_per_bar` equals
    /// its bar phase.
    ///
    /// The grid advances by the beat-index delta, then a phase correction moves it
    /// forward to the next beat with the reported phase, never back: loops are pure
    /// functions of the position, so rewinding would replay or reverse their motion.
    /// A beat without a phase keeps plain counting, which preserves the last alignment.
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
                last.grid_beat
                    .checked_add(beat.beat_index - last.beat_index)
                    .context("musical clock overflow")?
            }
            None => beat.beat_index,
        };
        let grid_beat = match beat.bar_phase {
            Some(phase) => {
                let length = u64::from(beat.beats_per_bar);
                counted
                    .checked_add((u64::from(phase) + length - counted % length) % length)
                    .context("musical clock overflow")?
            }
            None => counted,
        };
        ensure!(grid_beat <= MAX_GRID_BEAT, "musical clock overflow");
        let before = self.sample_at(time_seconds)?;
        self.anchor_seconds = time_seconds;
        self.anchor_beat = grid_beat as f64;
        self.anchor_elapsed_musical_beats = before.elapsed_musical_beats;
        // Beat measurements correct musical phase; integrated animation stays continuous.
        self.anchor_animation_beats = before.animation_beats;
        self.tempo_bpm = beat.bpm;
        self.beats_per_bar = beat.beats_per_bar;
        self.last_received = Some(ReceivedBeat {
            beat_index: beat.beat_index,
            grid_beat,
        });
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

pub(crate) fn validate_time(time_seconds: f64) -> Result<()> {
    ensure!(
        time_seconds.is_finite() && time_seconds >= 0.0,
        "time must be finite and nonnegative"
    );
    Ok(())
}
