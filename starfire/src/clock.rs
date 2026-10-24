//! Musical time is derived from supplied anchors; sampling never advances it.

use anyhow::{Result, ensure};

const SECONDS_PER_MINUTE: f64 = 60.0;
pub const MAX_TEMPO_BPM: f64 = 1_000.0;
pub const MAX_SPEED_MULTIPLIER: f64 = 8.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ClockSample {
    pub beat_position: f64,
    pub animation_beats: f64,
}

#[derive(Clone, Debug)]
pub struct MusicalClock {
    anchor_seconds: f64,
    anchor_beat: f64,
    anchor_animation_beats: f64,
    tempo_bpm: f64,
    speed_multiplier: f64,
    last_received_beat: Option<f64>,
}

impl Default for MusicalClock {
    fn default() -> Self {
        Self {
            anchor_seconds: 0.0,
            anchor_beat: 0.0,
            anchor_animation_beats: 0.0,
            tempo_bpm: 0.0,
            speed_multiplier: 1.0,
            last_received_beat: None,
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
        let sample = ClockSample {
            beat_position: self.anchor_beat + elapsed_beats,
            animation_beats: self.anchor_animation_beats + elapsed_beats * self.speed_multiplier,
        };
        ensure!(
            sample.beat_position.is_finite() && sample.animation_beats.is_finite(),
            "musical clock overflow"
        );
        Ok(sample)
    }

    pub fn set_beat_anchor(&mut self, time_seconds: f64, beat: f64, bpm: f64) -> Result<()> {
        ensure!(
            beat.is_finite() && beat >= 0.0,
            "beat position must be finite and nonnegative"
        );
        ensure!(
            bpm.is_finite() && bpm > 0.0 && bpm <= MAX_TEMPO_BPM,
            "tempo is outside the supported range"
        );
        if let Some(previous_beat) = self.last_received_beat {
            ensure!(
                beat >= previous_beat,
                "beat anchors must not move backwards"
            );
        }
        let before = self.sample_at(time_seconds)?;
        self.anchor_seconds = time_seconds;
        self.anchor_beat = beat;
        // Beat measurements correct musical phase; integrated animation stays continuous.
        self.anchor_animation_beats = before.animation_beats;
        self.tempo_bpm = bpm;
        self.last_received_beat = Some(beat);
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
