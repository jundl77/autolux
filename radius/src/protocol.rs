use anyhow::{Result, bail, ensure};
use prost::Message as ProstMessage;
use std::time::Duration;

pub mod wire {
    include!(concat!(env!("OUT_DIR"), "/autolux.radius.rs"));
}

pub const MAX_MESSAGE_BYTES: usize = 1200;
pub const MAX_ERROR_BYTES: usize = 512;
pub const HEADER_BYTES: usize = 5;
pub const SAMPLE_RATE: u32 = 44_100;
pub const CHANNELS: usize = 2;
pub const BLOCK_FRAMES: usize = 128;
pub const DMX_CHANNELS: usize = 512;
pub const BUFFER_MILLISECONDS: u32 = 500;
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
pub const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(30);
pub const LOGIN_TIMEOUT: Duration = Duration::from_secs(15);
pub const METRICS_INTERVAL: Duration = Duration::from_secs(1);
pub const LOGIN_COMMAND_ID: u64 = 1;

#[derive(Clone, Debug, PartialEq)]
pub enum Message {
    Login(wire::Login),
    LoginReply(wire::LoginReply),
    ErrorReply(wire::ErrorReply),
    AudioFrame(wire::AudioFrame),
    LightFrame(wire::LightFrame),
    Heartbeat(wire::Heartbeat),
    Metrics(wire::Metrics),
}

impl Message {
    pub fn run_id(&self) -> u64 {
        match self {
            Self::Login(v) => v.run_id,
            Self::LoginReply(v) => v.run_id,
            Self::ErrorReply(v) => v.run_id,
            Self::AudioFrame(v) => v.run_id,
            Self::LightFrame(v) => v.run_id,
            Self::Heartbeat(v) => v.run_id,
            Self::Metrics(v) => v.run_id,
        }
    }

    pub fn validate(&self, run_id: u64) -> Result<()> {
        ensure!(
            self.run_id() != 0 && self.run_id() == run_id,
            "Radius run_id mismatch"
        );
        match self {
            Self::AudioFrame(v) => {
                let samples = &v.interleaved_samples;
                ensure!(
                    !samples.is_empty()
                        && samples.len() <= BLOCK_FRAMES * CHANNELS
                        && samples.len().is_multiple_of(CHANNELS),
                    "invalid audio sample count"
                );
                ensure!(
                    samples.iter().all(|v| v.is_finite()),
                    "nonfinite audio sample"
                );
                ensure!(
                    v.audio_frame_position
                        .checked_add((samples.len() / CHANNELS) as u64)
                        .is_some(),
                    "audio position overflow"
                );
            }
            Self::LightFrame(v) => ensure!(
                v.channels.len() == DMX_CHANNELS,
                "light state must contain 512 channels"
            ),
            Self::ErrorReply(v) => {
                ensure!(v.message.len() <= MAX_ERROR_BYTES, "oversized Radius error")
            }
            _ => {}
        }
        Ok(())
    }

    pub fn encode(&self) -> Result<Vec<u8>> {
        use wire::MessageType as T;
        let (kind, payload) = match self {
            Self::Login(v) => (T::Login, v.encode_to_vec()),
            Self::LoginReply(v) => (T::LoginReply, v.encode_to_vec()),
            Self::ErrorReply(v) => (T::ErrorReply, v.encode_to_vec()),
            Self::AudioFrame(v) => (T::AudioFrame, v.encode_to_vec()),
            Self::LightFrame(v) => (T::LightFrame, v.encode_to_vec()),
            Self::Heartbeat(v) => (T::Heartbeat, v.encode_to_vec()),
            Self::Metrics(v) => (T::Metrics, v.encode_to_vec()),
        };
        ensure!(
            payload.len() + HEADER_BYTES <= MAX_MESSAGE_BYTES,
            "Radius message exceeds resource bound"
        );
        let mut bytes = Vec::with_capacity(HEADER_BYTES + payload.len());
        bytes.push(kind as u8);
        bytes.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        bytes.extend_from_slice(&payload);
        Ok(bytes)
    }

    pub fn decode(kind: u8, bytes: &[u8]) -> Result<Self> {
        use wire::MessageType as T;
        ensure!(
            bytes.len() + HEADER_BYTES <= MAX_MESSAGE_BYTES,
            "oversized Radius message"
        );
        Ok(match T::try_from(i32::from(kind))? {
            T::Login => Self::Login(wire::Login::decode(bytes)?),
            T::LoginReply => Self::LoginReply(wire::LoginReply::decode(bytes)?),
            T::ErrorReply => Self::ErrorReply(wire::ErrorReply::decode(bytes)?),
            T::AudioFrame => Self::AudioFrame(wire::AudioFrame::decode(bytes)?),
            T::LightFrame => Self::LightFrame(wire::LightFrame::decode(bytes)?),
            T::Heartbeat => Self::Heartbeat(wire::Heartbeat::decode(bytes)?),
            T::Metrics => Self::Metrics(wire::Metrics::decode(bytes)?),
            T::Unspecified => bail!("unspecified Radius message type"),
        })
    }
}

pub fn validate_login(login: &wire::Login, run_id: u64) -> Result<()> {
    ensure!(
        login.run_id == run_id && run_id != 0,
        "Radius run_id mismatch"
    );
    ensure!(login.command_id != 0, "Login command_id must be nonzero");
    ensure!(
        login.protocol_major == wire::ProtocolVersion::ProtocolMajor as u32,
        "incompatible Radius protocol major"
    );
    ensure!(
        login.sample_rate_hz == SAMPLE_RATE && login.channel_count == CHANNELS as u32,
        "Radius requires 44100 Hz stereo"
    );
    ensure!(
        login.jitter_buffer_ms == BUFFER_MILLISECONDS,
        "Radius requires a 500 ms receiver buffer"
    );
    ensure!(
        !login.lighting_enabled || login.audio_enabled,
        "lighting requires the audible audio clock"
    );
    Ok(())
}

pub fn login_reply(login: &wire::Login) -> wire::LoginReply {
    wire::LoginReply {
        command_id: login.command_id,
        run_id: login.run_id,
        protocol_major: wire::ProtocolVersion::ProtocolMajor as u32,
        protocol_minor: login
            .protocol_minor
            .min(wire::ProtocolVersion::ProtocolMinor as u32),
        max_message_bytes: MAX_MESSAGE_BYTES as u32,
    }
}
