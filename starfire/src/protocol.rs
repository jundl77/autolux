//! Typed local control messages; framing is shared with the Python coordinator.

use std::io::{Read, Write};

use anyhow::{Context, Result, bail, ensure};
use prost::Message;

use crate::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Rgb, Target, Transition,
};

pub const SCHEMA_VERSION: u32 = 1;
pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
const LENGTH_BYTES: usize = 4;

pub mod wire {
    include!(concat!(env!("OUT_DIR"), "/autolux.starfire.v1.rs"));
}

/// A clean EOF is allowed only between complete messages.
pub fn read_control(input: &mut impl Read) -> Result<Option<wire::Control>> {
    let mut length = [0; LENGTH_BYTES];
    loop {
        match input.read(&mut length[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error).context("read control prefix"),
        }
    }
    input
        .read_exact(&mut length[1..])
        .context("truncated control prefix")?;
    let length = u32::from_be_bytes(length) as usize;
    ensure!(
        (1..=MAX_MESSAGE_BYTES).contains(&length),
        "control message length must be between 1 and {MAX_MESSAGE_BYTES} bytes"
    );
    let mut payload = vec![0; length];
    input
        .read_exact(&mut payload)
        .context("truncated control payload")?;
    let control = wire::Control::decode(payload.as_slice()).context("decode control protobuf")?;
    ensure!(
        control.schema_version == SCHEMA_VERSION,
        "unsupported control schema version"
    );
    ensure!(control.command.is_some(), "control requires a command");
    Ok(Some(control))
}

pub fn write_event(output: &mut impl Write, event: wire::renderer_event::Event) -> Result<()> {
    let message = wire::RendererEvent {
        schema_version: SCHEMA_VERSION,
        event: Some(event),
    };
    let payload = message.encode_to_vec();
    ensure!(
        payload.len() <= MAX_MESSAGE_BYTES,
        "renderer event exceeds byte limit"
    );
    output.write_all(&(payload.len() as u32).to_be_bytes())?;
    output.write_all(&payload)?;
    output.flush().context("flush renderer event")
}

/// Heartbeat has no musical meaning and therefore is absent from replay commands.
pub fn musical_command(control: wire::control::Command) -> Result<Option<Command>> {
    use wire::control::Command as Wire;
    Ok(Some(match control {
        Wire::Heartbeat(_) => return Ok(None),
        Wire::Beat(value) => Command::Beat {
            position_beats: value.position_beats,
            bpm: value.bpm,
        },
        Wire::Select(value) => Command::Select {
            bank: value.bank,
            sequence: value.sequence,
            transition: transition(value.transition)?,
        },
        Wire::SetOverride(value) => Command::SetOverride {
            value: lighting_override(value)?,
        },
        Wire::ClearOverride(value) => Command::ClearOverride { id: value.id },
        Wire::SetMaster(value) => Command::SetMaster {
            intensity: value.intensity,
        },
        Wire::SetSpeed(value) => Command::SetSpeed {
            multiplier: value.multiplier,
        },
        Wire::SetBlackout(value) => Command::SetBlackout {
            enabled: value.enabled,
        },
        Wire::SetNormalizedRmsLevel(value) => Command::SetNormalizedRmsLevel { level: value.level },
        Wire::Stop(_) => Command::Stop,
    }))
}

fn transition(value: Option<wire::Transition>) -> Result<Transition> {
    Ok(match value {
        None => Transition::Cut,
        Some(value) => match value.policy.context("transition requires a policy")? {
            wire::transition::Policy::Cut(_) => Transition::Cut,
            wire::transition::Policy::CrossfadeBeats(duration_beats) => {
                Transition::Crossfade { duration_beats }
            }
        },
    })
}

fn lighting_override(value: wire::LightingOverride) -> Result<LightingOverride> {
    let target = value.target.context("override requires a target")?;
    let target = match target
        .selection
        .context("override target requires a selection")?
    {
        wire::target::Selection::All(_) => Target::All,
        wire::target::Selection::Group(name) => Target::Group(name),
        wire::target::Selection::Fixture(name) => Target::Fixture(name),
    };
    let values = value.values.unwrap_or_default();
    let lifetime = match value.lifetime {
        None => OverrideLifetime::UntilCleared,
        Some(lifetime) => match lifetime
            .duration
            .context("override lifetime requires a duration")?
        {
            wire::lifetime::Duration::UntilCleared(_) => OverrideLifetime::UntilCleared,
            wire::lifetime::Duration::Beats(duration_beats) => {
                OverrideLifetime::Beats { duration_beats }
            }
            wire::lifetime::Duration::Seconds(duration_seconds) => {
                OverrideLifetime::Seconds { duration_seconds }
            }
        },
    };
    if values.intensity.is_none()
        && values.colour.is_none()
        && values.pan.is_none()
        && values.tilt.is_none()
        && values.strobe_hz.is_none()
        && value.intensity_multiplier.is_none()
    {
        bail!("override must change at least one property");
    }
    Ok(LightingOverride {
        id: value.id,
        target,
        values: PropertyValues {
            intensity: values.intensity,
            colour: values.colour.map(|colour| Rgb {
                red: colour.red,
                green: colour.green,
                blue: colour.blue,
            }),
            pan: values.pan,
            tilt: values.tilt,
            strobe_hz: values.strobe_hz,
        },
        intensity_multiplier: value.intensity_multiplier,
        lifetime,
        priority: value.priority,
    })
}
