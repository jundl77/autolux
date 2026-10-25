//! Generated message types, bounded framing and command dispatch.

use std::io::{Read, Write};

use anyhow::{Context, Result, bail, ensure};
use prost::Message;

use crate::engine::Engine;
use crate::model::{Command, Transition};

pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
pub const MAX_ERROR_BYTES: usize = 1024;
const LENGTH_BYTES: usize = 4;

pub mod wire {
    include!(concat!(env!("OUT_DIR"), "/autolux.starfire.rs"));
}

pub fn error_message(error: &anyhow::Error) -> String {
    let mut message = format!("{error:#}");
    if message.len() > MAX_ERROR_BYTES {
        message.truncate(message.floor_char_boundary(MAX_ERROR_BYTES));
    }
    message
}

/// A clean EOF is allowed only between complete messages.
pub fn read_control(input: &mut impl Read) -> Result<Option<wire::RendererCommand>> {
    let mut length = [0; LENGTH_BYTES];
    loop {
        match input.read(&mut length[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error).context("read command prefix"),
        }
    }
    input
        .read_exact(&mut length[1..])
        .context("truncated command prefix")?;
    let length = u32::from_be_bytes(length) as usize;
    ensure!(
        (1..=MAX_MESSAGE_BYTES).contains(&length),
        "invalid command message length"
    );
    let mut payload = vec![0; length];
    input
        .read_exact(&mut payload)
        .context("truncated command payload")?;
    let command =
        wire::RendererCommand::decode(payload.as_slice()).context("decode command protobuf")?;
    ensure!(command.command_id > 0, "command ID must be positive");
    ensure!(
        command.command.is_some(),
        "renderer command requires an operation"
    );
    Ok(Some(command))
}

pub fn write_event(output: &mut impl Write, message: &wire::RendererEvent) -> Result<()> {
    let payload = message.encode_to_vec();
    ensure!(
        payload.len() <= MAX_MESSAGE_BYTES,
        "renderer event exceeds byte limit"
    );
    output.write_all(&(payload.len() as u32).to_be_bytes())?;
    output.write_all(&payload)?;
    output.flush().context("flush renderer event")
}

/// Decode and validate against the state left by preceding accepted commands.
pub fn apply_command(
    engine: &mut Engine,
    at_seconds: f64,
    command: &wire::renderer_command::Command,
) -> Result<()> {
    use wire::renderer_command::Command as Wire;
    match command {
        Wire::Login(_) => bail!("login is handled by the process host"),
        Wire::Heartbeat(_) => Ok(()),
        Wire::UpdateBeat(value) => engine.apply(
            at_seconds,
            &Command::Beat {
                position_beats: value.position_beats,
                bpm: value.tempo_bpm,
            },
        ),
        Wire::PlaySequence(value) => {
            let Some(sequence) = value.sequence.as_ref() else {
                ensure!(
                    value.transition.is_none(),
                    "idle does not accept a transition"
                );
                return engine.apply(at_seconds, &Command::Stop);
            };
            engine.apply(
                at_seconds,
                &Command::Select {
                    bank: sequence.bank_id.clone(),
                    sequence: sequence.sequence_id.clone(),
                    transition: transition(value.transition.as_ref())?,
                },
            )
        }
        Wire::SetParameter(value) => engine.set_parameter(at_seconds, value),
        Wire::SetActiveEffect(value) => engine.set_active_effect(at_seconds, value),
        Wire::UnsetActiveEffect(value) => engine.apply(
            at_seconds,
            &Command::ClearOverride {
                id: value.effect_id.clone(),
            },
        ),
    }
}

fn transition(value: Option<&wire::Transition>) -> Result<Transition> {
    Ok(match value {
        None => Transition::Cut,
        Some(value) => match value
            .policy
            .as_ref()
            .context("transition requires a policy")?
        {
            wire::transition::Policy::Cut(_) => Transition::Cut,
            wire::transition::Policy::CrossfadeBeats(duration_beats) => Transition::Crossfade {
                duration_beats: *duration_beats,
            },
        },
    })
}
