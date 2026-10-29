use std::io::{Read, Write};

use anyhow::{Context, Result, bail, ensure};
use prost::Message;

use crate::engine::Engine;
use crate::model::{
    AttributeValue, BeatFeatures, Command, CueSection, Envelope, FixturePose, LightingOverride,
    MovementRange, Overlay, OverrideLifetime, PropertyValues, Rgb, SectionIntent, SpecialFx, Step,
    Target, Transition,
};

pub const MAX_LOGIN_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_MESSAGE_BYTES: usize = 4 * 1024;
pub const MAX_ERROR_BYTES: usize = 1024;
pub const FRAME_HEADER_BYTES: usize = 5;

pub mod wire {
    include!(concat!(env!("OUT_DIR"), "/autolux.starfire.rs"));
}

pub fn max_payload_bytes(kind: wire::MessageType) -> usize {
    match kind {
        wire::MessageType::Login => MAX_LOGIN_BYTES,
        _ => MAX_MESSAGE_BYTES,
    }
}

fn check_payload_size(kind: wire::MessageType, length: usize, limit: usize) -> Result<()> {
    ensure!(
        length <= limit,
        "{kind:?} payload of {length} bytes exceeds the {limit}-byte limit"
    );
    Ok(())
}

// The frame carries the generated message itself; no Protobuf envelope is serialized.
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
                check_payload_size(kind, payload.len(), max_payload_bytes(kind))?;
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
    DiscardDelayedAudio,
    CueSection,
    ClearCues,
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
            Self::DiscardDelayedAudio(value) => value.command_id,
            Self::CueSection(value) => value.command_id,
            Self::ClearCues(value) => value.command_id,
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
            Self::DiscardDelayedAudio(value) => value.command_id = command_id,
            Self::CueSection(value) => value.command_id = command_id,
            Self::ClearCues(value) => value.command_id = command_id,
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

/// EOF is clean only between frames; a zero-length payload is a valid default-valued message.
fn read_frame(
    input: &mut impl Read,
    accepts: impl FnOnce(wire::MessageType) -> bool,
    logged_in: bool,
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
    let limit = if logged_in {
        MAX_MESSAGE_BYTES
    } else {
        max_payload_bytes(kind)
    };
    check_payload_size(kind, length, limit)?;
    let mut payload = vec![0; length];
    input
        .read_exact(&mut payload)
        .context("truncated message payload")?;
    Ok(Some((kind, payload)))
}

pub fn read_control(input: &mut impl Read) -> Result<Option<ClientMessage>> {
    read_session_control(input, false)
}

pub fn read_session_control(
    input: &mut impl Read,
    logged_in: bool,
) -> Result<Option<ClientMessage>> {
    let Some((kind, payload)) = read_frame(input, ClientMessage::accepts, logged_in)? else {
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
    let Some((kind, payload)) = read_frame(input, ServerMessage::accepts, false)? else {
        return Ok(None);
    };
    Ok(Some(ServerMessage::decode(kind, &payload)?))
}

fn transition(value: Option<&wire::Transition>) -> Transition {
    value.map_or(Transition::RendererDefault, |value| {
        Transition::from_crossfade_beats(value.crossfade_beats)
    })
}

fn envelope(value: Option<&wire::Envelope>) -> Envelope {
    value.map_or_else(Envelope::default, |value| Envelope {
        attack_beats: value.attack_beats,
        release_beats: value.release_beats,
    })
}

fn cue_section(engine: &Engine, value: &wire::CueSection) -> Result<CueSection> {
    let intent = match wire::SectionIntent::try_from(value.intent) {
        Ok(wire::SectionIntent::Atmospheric) => SectionIntent::Atmospheric,
        Ok(wire::SectionIntent::Breakdown) => SectionIntent::Breakdown,
        Ok(wire::SectionIntent::Buildup) => SectionIntent::Buildup,
        Ok(wire::SectionIntent::Drop) => SectionIntent::Drop,
        _ => bail!("a cue must name its section intent"),
    };
    let rotation = value
        .rotation
        .iter()
        .map(|step| {
            let (bank, autoloop) = engine.resolve_autoloop(&step.autoloop_id)?;
            Ok(Step {
                bank,
                autoloop,
                bars: step.bars,
                transition: transition(step.transition.as_ref()),
            })
        })
        .collect::<Result<_>>()?;
    let overlays = value
        .overlays
        .iter()
        .map(|overlay| {
            let (bank, autoloop) = engine.resolve_autoloop(&overlay.autoloop_id)?;
            Ok(Overlay {
                bank,
                autoloop,
                priority: overlay.priority,
                begin_beats: overlay.begin_beats,
                duration_beats: overlay.duration_beats,
                every_bars: overlay.every_bars,
                envelope: envelope(overlay.envelope.as_ref()),
                dip: overlay.dip,
            })
        })
        .collect::<Result<_>>()?;
    Ok(CueSection {
        start_beat_index: value.start_beat_index,
        intent,
        rotation,
        overlays,
        palette: value.palette.clone(),
        drop_strength: value.drop_strength,
        seed: value.seed,
    })
}

pub fn apply_command(engine: &mut Engine, at_seconds: f64, command: &ClientMessage) -> Result<()> {
    match command {
        ClientMessage::Login(_) => bail!("login is handled by the process host"),
        ClientMessage::Heartbeat(_) => Ok(()),
        ClientMessage::DiscardDelayedAudio(_) => Ok(()),
        ClientMessage::UpdateBeat(value) => engine.apply(
            at_seconds,
            &Command::Beat {
                beat_index: value.beat_index,
                bar_phase: value.bar_phase,
                beats_per_bar: value.beats_per_bar,
                bpm: value.tempo_bpm,
                features: value.features.map(|features| BeatFeatures {
                    onset: f64::from(features.onset),
                    bass: f64::from(features.bass),
                    mid: f64::from(features.mid),
                    high: f64::from(features.high),
                }),
            },
        ),
        ClientMessage::CueSection(value) => {
            let cue = cue_section(engine, value)?;
            engine.apply(
                at_seconds,
                &Command::CueSection {
                    value: Box::new(cue),
                },
            )
        }
        ClientMessage::ClearCues(_) => engine.apply(at_seconds, &Command::ClearCues),
        ClientMessage::PlayAutoloop(value) => {
            if value.autoloop_id.is_empty() {
                ensure!(
                    value.transition.is_none(),
                    "idle does not accept a transition"
                );
                ensure!(value.palette.is_empty(), "idle does not accept a palette");
                return engine.apply(at_seconds, &Command::Stop);
            }
            ensure!(
                !value.palette.is_empty(),
                "autoloop {} must name its palette",
                value.autoloop_id
            );
            let transition = transition(value.transition.as_ref());
            engine.play_autoloop(at_seconds, &value.autoloop_id, transition, &value.palette)
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
            let special = value.special_fx.as_ref().map(|special| {
                Box::new(SpecialFx {
                    colour_slot: special.colour_slot.clone(),
                    energy: f64::from(special.energy),
                    speed: f64::from(special.speed),
                    pulse_beats: special.pulse_beats,
                    pattern: special.pattern.map(f64::from),
                    envelope: envelope(special.envelope.as_ref()),
                })
            });
            ensure!(
                values != PropertyValues::default() || special.is_some(),
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
                        special_fx: special,
                        pan_sweep: value.pan_sweep,
                        aim_focus: value.aim_focus,
                        pose: value.pose.as_ref().map(|pose| {
                            Box::new(FixturePose {
                                matrix: pose.matrix.clone(),
                                pan: MovementRange::from_wire(&pose.pan),
                                tilt: MovementRange::from_wire(&pose.tilt),
                                focus: pose.focus.clone(),
                                universe: pose.universe,
                                address: pose.address,
                                profile_id: pose.profile_id.clone(),
                            })
                        }),
                    },
                },
            )
        }
        ClientMessage::UnsetActiveEffect(value) => engine.apply(
            at_seconds,
            &Command::ClearOverride {
                id: value.effect_id.clone(),
                instantaneous_release: value.instantaneous_release,
            },
        ),
    }
}
