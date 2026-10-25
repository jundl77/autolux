use crate::protocol::{BUFFER_MILLISECONDS, SAMPLE_RATE};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    net::{IpAddr, SocketAddr},
    path::{Path, PathBuf},
};

fn default_port() -> u16 {
    19002
}
fn default_bind() -> String {
    "0.0.0.0".into()
}
fn enabled() -> bool {
    true
}
fn default_buffer() -> u32 {
    BUFFER_MILLISECONDS
}
fn default_rate() -> u32 {
    SAMPLE_RATE
}
fn any_core() -> i32 {
    -1
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ReceiverSettings {
    #[serde(default = "enabled")]
    pub audio_enabled: bool,
    #[serde(default = "enabled")]
    pub lighting_enabled: bool,
    #[serde(default = "default_buffer")]
    pub jitter_buffer_ms: u32,
}

impl Default for ReceiverSettings {
    fn default() -> Self {
        Self {
            audio_enabled: true,
            lighting_enabled: true,
            jitter_buffer_ms: BUFFER_MILLISECONDS,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TxConfig {
    pub peer_address: String,
    #[serde(default = "default_port")]
    pub bus_port: u16,
    pub run_id: u64,
    pub audio_device_id: String,
    #[serde(default)]
    pub health_file: Option<PathBuf>,
    #[serde(default)]
    pub playback_delay_seconds: f64,
    #[serde(default = "enabled")]
    pub audio_enabled: bool,
    #[serde(default = "any_core")]
    pub network_core: i32,
    #[serde(default)]
    pub receiver: ReceiverSettings,
}

impl TxConfig {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.run_id != 0 && self.bus_port != 0,
            "nonzero run_id and TCP port required"
        );
        self.peer_address
            .parse::<IpAddr>()
            .context("invalid receiver IP address")?;
        ensure!(
            self.playback_delay_seconds.is_finite()
                && (0.0..=60.0).contains(&self.playback_delay_seconds),
            "analysis delay must be 0..60 seconds"
        );
        ensure!(
            self.receiver.jitter_buffer_ms == BUFFER_MILLISECONDS,
            "receiver buffer must be 500 ms"
        );
        ensure!(
            !self.receiver.lighting_enabled || self.receiver.audio_enabled,
            "lighting requires receiver audio playback"
        );
        ensure!(
            self.audio_enabled == self.receiver.audio_enabled,
            "capture and playback must be enabled together"
        );
        ensure!(
            !self.audio_enabled || !self.audio_device_id.is_empty(),
            "capture endpoint ID is required"
        );
        Ok(())
    }

    pub fn address(&self) -> Result<SocketAddr> {
        Ok(SocketAddr::new(self.peer_address.parse()?, self.bus_port))
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RxConfig {
    pub peer_address: String,
    #[serde(default = "default_bind")]
    pub bind_address: String,
    #[serde(default = "default_port")]
    pub bus_port: u16,
    pub run_id: u64,
    #[serde(default)]
    pub audio_device_id: String,
    #[serde(default)]
    pub dmx_serial: Option<String>,
    #[serde(default = "enabled")]
    pub audio_enabled: bool,
    #[serde(default = "enabled")]
    pub dmx_enabled: bool,
    #[serde(default = "default_buffer")]
    pub jitter_buffer_ms: u32,
    #[serde(default = "default_rate")]
    pub sample_rate: u32,
    #[serde(default = "any_core")]
    pub network_core: i32,
    #[serde(default = "any_core")]
    pub dmx_core: i32,
}

impl RxConfig {
    pub fn from_file(path: &Path) -> Result<Self> {
        let bytes = std::fs::read_to_string(path)?;
        let value: Self = serde_json::from_str(bytes.trim_start_matches('\u{feff}'))?;
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.run_id != 0 && self.bus_port != 0,
            "nonzero run_id and TCP port required"
        );
        self.peer_address
            .parse::<IpAddr>()
            .context("invalid transmitter IP address")?;
        self.bind_address
            .parse::<IpAddr>()
            .context("invalid receiver bind IP address")?;
        ensure!(
            self.sample_rate == SAMPLE_RATE && self.jitter_buffer_ms == BUFFER_MILLISECONDS,
            "receiver requires 44100 Hz audio and 500 ms buffering"
        );
        ensure!(
            !self.audio_enabled || !self.audio_device_id.is_empty(),
            "output endpoint ID required"
        );
        ensure!(
            !self.dmx_enabled || self.audio_enabled,
            "lighting requires receiver audio playback"
        );
        Ok(())
    }

    pub fn address(&self) -> Result<SocketAddr> {
        Ok(SocketAddr::new(self.bind_address.parse()?, self.bus_port))
    }
}

pub fn pin_worker(core: i32) -> Result<()> {
    if core < 0 {
        return Ok(());
    }
    let available = core_affinity::get_core_ids().context("cannot inspect CPU affinity")?;
    let selected = available
        .into_iter()
        .find(|id| id.id == core as usize)
        .context("configured worker CPU is not available")?;
    ensure!(
        core_affinity::set_for_current(selected),
        "cannot set worker CPU affinity"
    );
    Ok(())
}
