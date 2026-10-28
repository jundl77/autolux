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

pub fn render_replay(
    replay: &Replay,
    rig: FixtureRig,
    catalog: AutoloopCatalog,
) -> Result<Vec<RenderedFrame>> {
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
    Ok(frames)
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

pub const PREVIEW_KIND: &str = "starfire_preview";
const PREVIEW_DECIMALS: f64 = 1000.0;

pub struct PreviewSummary {
    pub frames: usize,
    pub script_errors: std::collections::BTreeSet<String>,
}

/// The offline preview as JSON lines: a header naming the rig's cells, groups and bar length,
/// then one frame per line at `rate_hz`, each cell `[i, r, g, b, strobe_hz, direction, gobo]`
/// in header order. Intensity and strobe are the output after masters and limits. Direction
/// (a unit vector in the MVR scene frame) and gobo leave the tail when absent; a missing
/// direction before a gobo is null. Frames stream out as they render, so a long recording
/// holds no frames in memory.
pub fn write_preview(
    replay: &Replay,
    rig: FixtureRig,
    catalog: AutoloopCatalog,
    rate_hz: u32,
    output: &mut impl Write,
) -> Result<PreviewSummary> {
    use crate::model::Command;
    use serde_json::{Value, json};
    use std::collections::{BTreeMap, BTreeSet};

    // Loops, crossfades and limits all run on time, not frame count, so rendering at the
    // preview rate shows what the 40 Hz show does at the same instants.
    let replay = Replay {
        frame_rate_hz: rate_hz,
        ..replay.clone()
    };
    let frame_count = replay.validate()?;
    let cells = rig.cell_ids();
    let index: BTreeMap<&str, usize> = cells
        .iter()
        .enumerate()
        .map(|(position, id)| (id.as_str(), position))
        .collect();
    let beats_per_bar = replay.events.iter().find_map(|event| match event.command {
        Command::Beat { beats_per_bar, .. } => Some(beats_per_bar),
        _ => None,
    });
    let header = json!({
        "kind": PREVIEW_KIND,
        "frame_rate_hz": rate_hz,
        "duration_seconds": replay.duration_seconds,
        "frames": frame_count,
        "beats_per_bar": beats_per_bar,
        "cells": cells.iter().map(|id| {
            let [x, y, z] = rig.spatial().position(id).unwrap_or_default().map(|mm| preview_round(mm / 1000.0));
            json!({"id": id, "fixture": rig.uuid_of_cell(id), "x": x, "y": y, "z": z})
        }).collect::<Vec<_>>(),
        "groups": rig.groups().iter().map(|(group, members)| {
            let members: Vec<usize> = members.iter().filter_map(|id| index.get(id.as_str()).copied()).collect();
            (group.clone(), members)
        }).collect::<BTreeMap<_, _>>(),
    });
    serde_json::to_writer(&mut *output, &header)?;
    output.write_all(b"\n")?;

    let mut engine = Engine::new(rig.clone(), catalog, PaletteTable::new(&replay.palettes)?)?;
    let mut events = replay.events.iter().peekable();
    let mut script_errors = BTreeSet::new();
    for frame_index in 0..frame_count {
        let time_seconds = (frame_index as f64 / f64::from(rate_hz)).min(replay.duration_seconds);
        while events
            .peek()
            .is_some_and(|event| event.at_seconds <= time_seconds)
        {
            let event = events.next().expect("peeked event exists");
            engine.apply(event.at_seconds, &event.command)?;
        }
        let frame = engine.render_at(time_seconds)?;
        let states = &frame.fixture_properties_before_strobe;
        let values: Vec<Value> = cells
            .iter()
            .map(|id| preview_cell(&states[id], None))
            .collect();
        let mut line = json!({
            "t": preview_round(frame.time_seconds),
            "beat": preview_round(frame.beat_position),
            "bank": frame.active_bank,
            "autoloop": frame.active_autoloop,
            "c": values,
        });
        if let Some(error) = frame.script_error {
            // A faulted loop renders dark; the flag keeps that from reading as restraint.
            line["fault"] = Value::Bool(true);
            script_errors.insert(error);
        }
        serde_json::to_writer(&mut *output, &line)?;
        output.write_all(b"\n")?;
    }
    output.flush()?;
    Ok(PreviewSummary {
        frames: frame_count,
        script_errors,
    })
}

fn preview_cell(
    state: &crate::model::FixtureState,
    direction: Option<[f64; 3]>,
) -> serde_json::Value {
    use crate::model::AttributeValue;
    use serde_json::{Value, json};

    let mut cell = [
        state.intensity,
        state.colour.red,
        state.colour.green,
        state.colour.blue,
        state.strobe_hz,
    ]
    .map(|value| json!(preview_round(value)))
    .to_vec();
    // GDTF names gobo wheels Gobo1, Gobo2, …; a selected slot is its ChannelSet's name.
    let gobo = state
        .attributes
        .iter()
        .find_map(|(attribute, value)| match value {
            AttributeValue::Preset(name)
                if attribute.strip_prefix("Gobo").is_some_and(|wheel| {
                    !wheel.is_empty() && wheel.bytes().all(|byte| byte.is_ascii_digit())
                }) =>
            {
                Some(name.clone())
            }
            _ => None,
        });
    if direction.is_some() || gobo.is_some() {
        cell.push(direction.map_or(Value::Null, |vector| json!(vector.map(preview_round))));
    }
    if let Some(gobo) = gobo {
        cell.push(json!(gobo));
    }
    Value::Array(cell)
}

/// Three decimals, with -0 written as 0.
fn preview_round(value: f64) -> f64 {
    (value * PREVIEW_DECIMALS).round() / PREVIEW_DECIMALS + 0.0
}
