//! Deterministic playback of recorded control input through the real renderer.

use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::engine::{Engine, RenderedFrame};
use crate::fixtures::FixtureRig;
use crate::model::{DMX_CHANNELS, MAX_FRAME_RATE_HZ, TimedCommand};
use crate::palette::{PaletteDefinition, PaletteTable};
use crate::script::AutoloopCatalog;

pub const REPLAY_SCHEMA_VERSION: u32 = 1;
pub const MAX_REPLAY_INPUT_BYTES: u64 = 8 * 1024 * 1024;
pub const MAX_REPLAY_EVENTS: usize = 100_000;
pub const MAX_REPLAY_FRAMES: usize = 100_000;
pub const MAX_REPLAY_FIXTURE_SAMPLES: usize = 500_000;
pub const MAX_REPLAY_DMX_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_REPLAY_DIAGNOSTIC_BYTES: usize = 8 * 1024 * 1024;
const CAPTURE_MAGIC: &[u8; 8] = b"STARFIRE";
const CAPTURE_VERSION: u32 = 1;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Replay {
    pub schema_version: u32,
    pub duration_seconds: f64,
    pub frame_rate_hz: u32,
    pub palettes: Vec<PaletteDefinition>,
    pub events: Vec<TimedCommand>,
}

impl Replay {
    pub fn load(path: &Path) -> Result<Self> {
        let mut bytes = Vec::new();
        File::open(path)
            .with_context(|| format!("open replay {}", path.display()))?
            .take(MAX_REPLAY_INPUT_BYTES + 1)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= MAX_REPLAY_INPUT_BYTES,
            "replay input exceeds byte limit"
        );
        let replay: Self = serde_json::from_slice(&bytes).context("decode replay input")?;
        replay.validate()?;
        Ok(replay)
    }

    pub fn validate(&self) -> Result<usize> {
        ensure!(
            self.schema_version == REPLAY_SCHEMA_VERSION,
            "unsupported replay schema"
        );
        ensure!(
            self.duration_seconds.is_finite() && self.duration_seconds >= 0.0,
            "replay duration must be finite and nonnegative"
        );
        ensure!(
            (1..=MAX_FRAME_RATE_HZ).contains(&self.frame_rate_hz),
            "invalid frame rate"
        );
        PaletteTable::new(&self.palettes)?;
        let last_frame = (self.duration_seconds * f64::from(self.frame_rate_hz)).ceil();
        ensure!(
            last_frame < MAX_REPLAY_FRAMES as f64,
            "replay exceeds frame limit"
        );
        ensure!(
            self.events.len() <= MAX_REPLAY_EVENTS,
            "replay exceeds event limit"
        );
        let mut previous_time = 0.0;
        for event in &self.events {
            ensure!(
                event.at_seconds.is_finite()
                    && event.at_seconds >= previous_time
                    && event.at_seconds <= self.duration_seconds,
                "replay events must be finite, ordered and inside the duration"
            );
            previous_time = event.at_seconds;
        }
        Ok(last_frame as usize + 1)
    }
}

pub struct ReplayResult {
    pub frames: Vec<RenderedFrame>,
}

pub fn render_replay(
    replay: &Replay,
    rig: FixtureRig,
    catalog: AutoloopCatalog,
) -> Result<ReplayResult> {
    let frame_count = replay.validate()?;
    let states = rig.empty_states();
    ensure!(
        frame_count.saturating_mul(states.len()) <= MAX_REPLAY_FIXTURE_SAMPLES,
        "replay exceeds fixture-sample memory budget"
    );
    let universe_count = rig.encode(&states)?.len();
    ensure!(
        frame_count
            .saturating_mul(universe_count)
            .saturating_mul(DMX_CHANNELS)
            <= MAX_REPLAY_DMX_BYTES,
        "replay exceeds DMX memory budget"
    );
    let mut engine = Engine::new(rig, catalog, PaletteTable::new(&replay.palettes)?)?;
    let mut events = replay.events.iter().peekable();
    let mut frames = Vec::with_capacity(frame_count);
    let mut diagnostic_bytes: usize = 0;
    for frame_index in 0..frame_count {
        let time_seconds =
            (frame_index as f64 / f64::from(replay.frame_rate_hz)).min(replay.duration_seconds);
        while events
            .peek()
            .is_some_and(|event| event.at_seconds <= time_seconds)
        {
            let event = events.next().expect("peeked event exists");
            engine.apply(event.at_seconds, &event.command)?;
        }
        let frame = engine.render_at(time_seconds)?;
        diagnostic_bytes =
            diagnostic_bytes.saturating_add(frame.script_error.as_ref().map_or(0, String::len));
        ensure!(
            diagnostic_bytes <= MAX_REPLAY_DIAGNOSTIC_BYTES,
            "replay exceeds diagnostic memory budget"
        );
        frames.push(frame);
    }
    Ok(ReplayResult { frames })
}

/// Hash the actual complete output: supplied frame time, universe and DMX bytes.
pub fn dmx_digest(frames: &[RenderedFrame]) -> String {
    let mut hash = Sha256::new();
    for frame in frames {
        hash.update(frame.time_seconds.to_le_bytes());
        for (universe, channels) in &frame.dmx_frames_by_universe {
            hash.update(universe.to_le_bytes());
            hash.update(channels);
        }
    }
    format!("{:x}", hash.finalize())
}

/// Capture format: magic, u32 version, u64 frame count; then each frame's f64
/// seconds, f64 beat position, u16 universe count and (u16 universe, 512 bytes).
/// Integer and float fields are little endian. No wall-clock or machine fields.
pub fn write_capture(path: &Path, frames: &[RenderedFrame]) -> Result<()> {
    let mut output =
        File::create(path).with_context(|| format!("create DMX capture {}", path.display()))?;
    output.write_all(CAPTURE_MAGIC)?;
    output.write_all(&CAPTURE_VERSION.to_le_bytes())?;
    output.write_all(&(frames.len() as u64).to_le_bytes())?;
    for frame in frames {
        let universe_count = u16::try_from(frame.dmx_frames_by_universe.len())?;
        output.write_all(&frame.time_seconds.to_le_bytes())?;
        output.write_all(&frame.beat_position.to_le_bytes())?;
        output.write_all(&universe_count.to_le_bytes())?;
        for (universe, channels) in &frame.dmx_frames_by_universe {
            ensure!(
                channels.len() == DMX_CHANNELS,
                "invalid capture DMX frame length"
            );
            output.write_all(&universe.to_le_bytes())?;
            output.write_all(channels)?;
        }
    }
    output.flush()?;
    Ok(())
}
