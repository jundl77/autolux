//! Standalone generated messages with bounded, typed stream framing.

use std::io::{Read, Write};

use anyhow::{Context, Result, bail, ensure};
use prost::Message;

use crate::engine::Engine;
use crate::model::{
    AttributeValue, Command, LightingOverride, OverrideLifetime, PropertyValues, Rgb, Target,
    Transition,
};

/// Contract value from the frame comment in `proto/starfire/starfire.proto`. Login
/// carries the rig and whole Lua library, so it sets the size; the bound still caps
/// the payload buffer allocated from an untrusted length prefix.
pub const MAX_MESSAGE_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_ERROR_BYTES: usize = 1024;
pub const FRAME_HEADER_BYTES: usize = 5;

pub mod wire {
    include!(concat!(env!("OUT_DIR"), "/autolux.starfire.rs"));
}

// These enums dispatch in-process only. The frame carries the generated message
// directly, with its generated MessageType; no Protobuf envelope is serialized.
macro_rules! messages {
    ($name:ident { $($variant:ident),+ $(,)? }) => {
        #[derive(Clone, Debug, PartialEq)]
        pub enum $name { $($variant(wire::$variant)),+ }

        impl $name {
            fn accepts(kind: wire::MessageType) -> bool {
                matches!(kind, $(wire::MessageType::$variant)|+)
            }

            fn decode(kind: wire::MessageType, payload: &[u8]) -> Result<Self> {
                Ok(match kind {
                    $(wire::MessageType::$variant => Self::$variant(wire::$variant::decode(payload)?)),+,
                    _ => bail!("message type is invalid in this direction"),
                })
            }

            pub fn write(&self, output: &mut impl Write) -> Result<()> {
                let (kind, payload) = match self {
                    $(Self::$variant(value) => (wire::MessageType::$variant, value.encode_to_vec())),+
                };
                ensure!(payload.len() <= MAX_MESSAGE_BYTES, "message exceeds byte limit");
                output.write_all(&[u8::try_from(kind as i32)?])?;
                output.write_all(&(payload.len() as u32).to_be_bytes())?;
                output.write_all(&payload)?;
                output.flush().context("flush protocol message")
            }
        }
    };
}

messages!(ClientMessage {
    Login,
    PlayAutoloop,
    SetActiveEffect,
    UnsetActiveEffect,
    UpdateBeat,
    SetParameter,
    Heartbeat
});
messages!(ServerMessage {
    LoginReply,
    ErrorReply,
    Heartbeat,
    Metrics
});

impl ClientMessage {
    pub fn command_id(&self) -> Option<u64> {
        Some(match self {
            Self::Login(value) => value.command_id,
            Self::PlayAutoloop(value) => value.command_id,
            Self::SetActiveEffect(value) => value.command_id,
            Self::UnsetActiveEffect(value) => value.command_id,
            Self::UpdateBeat(value) => value.command_id,
            Self::SetParameter(value) => value.command_id,
            Self::Heartbeat(_) => return None,
        })
    }

    pub fn set_command_id(&mut self, command_id: u64) {
        match self {
            Self::Login(value) => value.command_id = command_id,
            Self::PlayAutoloop(value) => value.command_id = command_id,
            Self::SetActiveEffect(value) => value.command_id = command_id,
            Self::UnsetActiveEffect(value) => value.command_id = command_id,
            Self::UpdateBeat(value) => value.command_id = command_id,
            Self::SetParameter(value) => value.command_id = command_id,
            Self::Heartbeat(_) => {}
        }
    }
}

impl ServerMessage {
    pub fn command_id(&self) -> Option<u64> {
        Some(match self {
            Self::LoginReply(value) => value.command_id,
            Self::ErrorReply(value) => value.command_id,
            Self::Heartbeat(_) | Self::Metrics(_) => return None,
        })
    }
}

pub fn error_message(error: &anyhow::Error) -> String {
    let mut message = format!("{error:#}");
    if message.len() > MAX_ERROR_BYTES {
        message.truncate(message.floor_char_boundary(MAX_ERROR_BYTES));
    }
    message
}

/// A clean EOF is allowed only between complete frames. Zero-length Protobuf
/// payloads are valid for default-valued messages, such as server heartbeats.
fn read_frame(
    input: &mut impl Read,
    accepts: impl FnOnce(wire::MessageType) -> bool,
) -> Result<Option<(wire::MessageType, Vec<u8>)>> {
    let mut header = [0; FRAME_HEADER_BYTES];
    loop {
        match input.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error).context("read frame type"),
        }
    }
    input
        .read_exact(&mut header[1..])
        .context("truncated frame header")?;
    let kind = wire::MessageType::try_from(i32::from(header[0])).context("unknown message type")?;
    ensure!(accepts(kind), "message type is invalid in this direction");
    let length = u32::from_be_bytes(header[1..].try_into()?) as usize;
    ensure!(length <= MAX_MESSAGE_BYTES, "invalid message length");
    let mut payload = vec![0; length];
    input
        .read_exact(&mut payload)
        .context("truncated message payload")?;
    Ok(Some((kind, payload)))
}

pub fn read_control(input: &mut impl Read) -> Result<Option<ClientMessage>> {
    let Some((kind, payload)) = read_frame(input, ClientMessage::accepts)? else {
        return Ok(None);
    };
    let command = ClientMessage::decode(kind, &payload)?;
    ensure!(
        command.command_id().is_none_or(|id| id > 0),
        "client command ID must be positive"
    );
    Ok(Some(command))
}

pub fn read_event(input: &mut impl Read) -> Result<Option<ServerMessage>> {
    let Some((kind, payload)) = read_frame(input, ServerMessage::accepts)? else {
        return Ok(None);
    };
    Ok(Some(ServerMessage::decode(kind, &payload)?))
}

pub fn write_event(output: &mut impl Write, message: &ServerMessage) -> Result<()> {
    message.write(output)
}

/// Validate against the state left by preceding accepted commands.
pub fn apply_command(engine: &mut Engine, at_seconds: f64, command: &ClientMessage) -> Result<()> {
    match command {
        ClientMessage::Login(_) => bail!("login is handled by the process host"),
        ClientMessage::Heartbeat(_) => Ok(()),
        ClientMessage::UpdateBeat(value) => engine.apply(
            at_seconds,
            &Command::Beat {
                position_beats: value.position_beats,
                bpm: value.tempo_bpm,
            },
        ),
        ClientMessage::PlayAutoloop(value) => {
            if value.autoloop_id.is_empty() {
                ensure!(
                    value.transition.is_none(),
                    "idle does not accept a transition"
                );
                return engine.apply(at_seconds, &Command::Stop);
            }
            // A present Transition without crossfade_beats is an explicit cut.
            let transition = match &value.transition {
                None => Transition::RendererDefault,
                Some(value) => value
                    .crossfade_beats
                    .map(|duration_beats| Transition::Crossfade { duration_beats })
                    .unwrap_or(Transition::Cut),
            };
            engine.play_autoloop(at_seconds, &value.autoloop_id, transition)
        }
        ClientMessage::SetParameter(value) => engine.set_parameter(
            at_seconds,
            value.parameter.as_ref().context("parameter is required")?,
        ),
        ClientMessage::SetActiveEffect(value) => {
            let values = PropertyValues {
                intensity: value.intensity,
                colour: value.colour.as_ref().map(|colour| Rgb {
                    red: colour.red,
                    green: colour.green,
                    blue: colour.blue,
                }),
                pan: value.pan,
                tilt: value.tilt,
                strobe_hz: value.strobe_hz,
                attributes: value
                    .attributes
                    .iter()
                    .map(|(name, value)| {
                        let value = match value
                            .value
                            .as_ref()
                            .context("GDTF attribute value is missing")?
                        {
                            wire::set_active_effect::attribute_value::Value::Number(number) => {
                                AttributeValue::Number(*number)
                            }
                            wire::set_active_effect::attribute_value::Value::Preset(preset) => {
                                AttributeValue::Preset(preset.clone())
                            }
                        };
                        Ok((name.clone(), value))
                    })
                    .collect::<Result<_>>()?,
            };
            ensure!(
                values != PropertyValues::default(),
                "effect must set at least one property"
            );
            let target = match &value.target {
                None => Target::All,
                Some(wire::set_active_effect::Target::Group(group)) => Target::Group(group.clone()),
                Some(wire::set_active_effect::Target::Fixture(fixture)) => {
                    Target::Fixture(fixture.clone())
                }
            };
            let lifetime = value
                .duration_seconds
                .map(|duration_seconds| OverrideLifetime::Seconds { duration_seconds })
                .unwrap_or_default();
            engine.apply(
                at_seconds,
                &Command::SetOverride {
                    value: LightingOverride {
                        id: value.effect_id.clone(),
                        target,
                        values,
                        intensity_multiplier: None,
                        lifetime,
                        priority: value.priority,
                    },
                },
            )
        }
        ClientMessage::UnsetActiveEffect(value) => engine.apply(
            at_seconds,
            &Command::ClearOverride {
                id: value.effect_id.clone(),
            },
        ),
    }
}
