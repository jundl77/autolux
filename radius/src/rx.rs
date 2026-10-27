use crate::{
    audio::{AudibleClock, AudibleSnapshot, AudioBlock, PlaybackStream},
    config::{RxConfig, pin_worker},
    enttec::EnttecOutput,
    playout::{LightingUpdate, PlaybackBlock},
    protocol::{self, Message, wire},
    transport::Connection,
};
use anyhow::{Context, Result, bail, ensure};
use crossbeam_queue::ArrayQueue;
use std::{
    io::ErrorKind,
    net::{IpAddr, TcpListener},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const POLL_INTERVAL: Duration = Duration::from_millis(1);
const DMX_POLL_INTERVAL: Duration = Duration::from_millis(5);
const DMX_FRAME_INTERVAL: Duration = Duration::from_millis(25);
const OUTPUT_START_TIMEOUT: Duration = Duration::from_secs(10);
const CALLBACK_STALE_TIMEOUT: Duration = Duration::from_secs(1);
const HEALTH_ERROR_LOG_INTERVAL: Duration = Duration::from_secs(30);
// Longer than the transmitter's 2 s stall limit, so one stall logs once.
const GAP_LOG_INTERVAL: Duration = Duration::from_secs(5);

/// Resolves lighting before admission. TCP guarantees each reference targets the
/// most recent preceding light state; no independently advancing lighting clock.
#[derive(Default)]
pub struct MediaReceiver {
    light: Option<wire::LightFrame>,
    audio_sequence: Option<u32>,
    next_audio_position: Option<u64>,
    pub audio_gaps_total: u64,
    pub lost_frames_total: u64,
}

/// An admitted AudioFrame, preceded by silence for audio the transmitter dropped.
#[derive(Debug)]
pub struct AdmittedAudio {
    pub silence_frames: u64,
    pub block: PlaybackBlock,
}

impl AdmittedAudio {
    /// Silence is generated lazily so the playback queue bound, not this span, limits memory.
    pub fn blocks(self) -> impl Iterator<Item = PlaybackBlock> {
        let end = self.block.audio.audio_frame_position;
        let start = end - self.silence_frames;
        let template = PlaybackBlock {
            audio: AudioBlock::default(),
            ..self.block.clone()
        };
        (start..end)
            .step_by(protocol::BLOCK_FRAMES)
            .map(move |position| {
                let mut silence = template.clone();
                silence.audio.audio_frame_position = position;
                silence.audio.frames = (end - position).min(protocol::BLOCK_FRAMES as u64) as u16;
                silence
            })
            .chain(std::iter::once(self.block))
    }
}

impl MediaReceiver {
    pub fn cache_light(&mut self, light: wire::LightFrame) -> Result<()> {
        ensure!(
            light.channels.len() == protocol::DMX_CHANNELS,
            "invalid light channel count"
        );
        if let Some(previous) = &self.light {
            ensure!(
                light.sequence_number == previous.sequence_number.wrapping_add(1),
                "light sequence is not consecutive"
            );
        }
        self.light = Some(light);
        Ok(())
    }

    /// A sequence gap means the transmitter dropped its oldest audio under backpressure;
    /// the missing span must be consistent with the dropped packet count.
    pub fn resolve_audio(&mut self, audio: wire::AudioFrame) -> Result<AdmittedAudio> {
        Message::AudioFrame(audio.clone()).validate(audio.run_id)?;
        let light = self
            .light
            .as_ref()
            .context("audio arrived before its light state")?;
        ensure!(
            light.run_id == audio.run_id && light.sequence_number == audio.light_sequence_number,
            "audio references an unavailable light state"
        );
        let missing_packets = self.audio_sequence.map_or(0, |previous| {
            u64::from(audio.sequence_number.wrapping_sub(previous.wrapping_add(1)))
        });
        let silence_frames = match self.next_audio_position {
            Some(expected) => audio
                .audio_frame_position
                .checked_sub(expected)
                .context("audio sample timeline moved backwards")?,
            None => 0,
        };
        ensure!(
            (missing_packets..=missing_packets * protocol::BLOCK_FRAMES as u64)
                .contains(&silence_frames),
            "audio sequence gap of {missing_packets} packets does not match a {silence_frames}-frame timeline gap"
        );
        if missing_packets != 0 {
            self.audio_gaps_total += 1;
            self.lost_frames_total += silence_frames;
        }
        let frames = audio.interleaved_samples.len() / protocol::CHANNELS;
        let mut block = AudioBlock {
            audio_frame_position: audio.audio_frame_position,
            frames: frames as u16,
            ..Default::default()
        };
        block.samples[..audio.interleaved_samples.len()]
            .copy_from_slice(&audio.interleaved_samples);
        self.audio_sequence = Some(audio.sequence_number);
        self.next_audio_position = Some(audio.audio_frame_position + frames as u64);
        Ok(AdmittedAudio {
            silence_frames,
            block: PlaybackBlock {
                audio: block,
                light_sequence_number: light.sequence_number,
                channels: light.channels.as_slice().try_into()?,
            },
        })
    }
}

#[derive(Default)]
struct DmxMetrics {
    frames: u64,
    lateness_seconds: f64,
    connected: bool,
    error: Option<String>,
}

struct LightingPlayback {
    current: [u8; protocol::DMX_CHANNELS],
    pending: Option<LightingUpdate>,
    lateness_seconds: f64,
}

impl LightingPlayback {
    fn new() -> Self {
        Self {
            current: [0; protocol::DMX_CHANNELS],
            pending: None,
            lateness_seconds: 0.0,
        }
    }

    fn frame(
        &mut self,
        audible: &AudibleSnapshot,
        updates: &ArrayQueue<LightingUpdate>,
    ) -> [u8; protocol::DMX_CHANNELS] {
        if !audible.position_valid
            || audible
                .callback_age
                .is_none_or(|age| age > CALLBACK_STALE_TIMEOUT)
        {
            return [0; protocol::DMX_CHANNELS];
        }
        loop {
            if self.pending.is_none() {
                self.pending = updates.pop();
            }
            let Some(update) = self.pending.as_ref() else {
                break;
            };
            if update.audio_frame_position > audible.audio_frame_position {
                break;
            }
            self.current = update.channels;
            self.lateness_seconds = (audible.audio_frame_position - update.audio_frame_position)
                as f64
                / f64::from(protocol::SAMPLE_RATE);
            self.pending = None;
        }
        self.current
    }
}

struct DmxWorker {
    stop: Arc<AtomicBool>,
    metrics: Arc<Mutex<DmxMetrics>>,
    worker: Option<JoinHandle<()>>,
}

impl DmxWorker {
    fn start(
        config: &RxConfig,
        clock: Arc<AudibleClock>,
        updates: Arc<ArrayQueue<LightingUpdate>>,
    ) -> Result<Self> {
        let stop = Arc::new(AtomicBool::new(false));
        let metrics = Arc::new(Mutex::new(DmxMetrics::default()));
        let worker_stop = stop.clone();
        let worker_metrics = metrics.clone();
        let config = config.clone();
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let worker = thread::Builder::new()
            .name("radius-dmx".into())
            .spawn(move || {
                let result = (|| -> Result<()> {
                    pin_worker(config.dmx_core)?;
                    let mut output = EnttecOutput::open(config.dmx_serial.as_deref())?;
                    worker_metrics.lock().unwrap().connected = true;
                    let _ = ready_tx.send(Ok(()));
                    let mut lighting = LightingPlayback::new();
                    let mut sent = [0; protocol::DMX_CHANNELS];
                    let mut last_write = Instant::now() - DMX_FRAME_INTERVAL;
                    while !worker_stop.load(Ordering::Acquire) {
                        let audible = clock.snapshot();
                        let current = lighting.frame(&audible, &updates);
                        worker_metrics.lock().unwrap().lateness_seconds = lighting.lateness_seconds;
                        if current != sent && last_write.elapsed() >= DMX_FRAME_INTERVAL {
                            output.write_frame(&current)?;
                            sent = current;
                            last_write = Instant::now();
                            worker_metrics.lock().unwrap().frames += 1;
                        }
                        thread::sleep(DMX_POLL_INTERVAL);
                    }
                    Ok(())
                })();
                let mut metrics = worker_metrics.lock().unwrap();
                metrics.connected = false;
                if let Err(error) = result {
                    let message = format!("{error:#}");
                    let _ = ready_tx.try_send(Err(message.clone()));
                    metrics.error = Some(message);
                }
            })?;
        let mut result = Self {
            stop,
            metrics,
            worker: Some(worker),
        };
        match ready_rx.recv_timeout(OUTPUT_START_TIMEOUT) {
            Ok(Ok(())) => Ok(result),
            failure => {
                result.stop.store(true, Ordering::Release);
                if let Some(worker) = result.worker.take() {
                    let _ = worker.join();
                }
                bail!("DMX output startup failed: {failure:?}")
            }
        }
    }
}

impl Drop for DmxWorker {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

pub fn run_receiver(
    config: RxConfig,
    stop: Arc<AtomicBool>,
    ready_file: Option<&Path>,
    health_file: Option<&Path>,
) -> Result<()> {
    config.validate()?;
    pin_worker(config.network_core)?;
    let listener = TcpListener::bind(config.address()?).context("bind Radius TCP receiver")?;
    listener.set_nonblocking(true)?;
    if let Some(path) = ready_file {
        std::fs::write(path, "ready\n")?;
    }
    let allowed_peer: IpAddr = config.peer_address.parse()?;
    while !stop.load(Ordering::Acquire) {
        match listener.accept() {
            Ok((stream, peer)) if peer.ip() == allowed_peer => {
                let mut connection = Connection::new(stream)?;
                let login = match receive_login(&config, &stop, &mut connection) {
                    Ok(Some(login)) => login,
                    Ok(None) => return Ok(()),
                    Err(error) => {
                        eprintln!("Radius rejected connection: {error:#}");
                        continue;
                    }
                };
                let result = run_connected(&config, &stop, &mut connection, login, health_file);
                if let Err(error) = &result {
                    let mut message = format!("{error:#}");
                    while message.len() > protocol::MAX_ERROR_BYTES {
                        message.pop();
                    }
                    let _ = connection.send(&Message::ErrorReply(wire::ErrorReply {
                        command_id: 0,
                        run_id: config.run_id,
                        message,
                    }));
                }
                return result;
            }
            Ok(_) => {}
            Err(error) if error.kind() == ErrorKind::WouldBlock => thread::sleep(POLL_INTERVAL),
            Err(error) => return Err(error).context("accept Radius client"),
        }
    }
    Ok(())
}

fn receive_login(
    config: &RxConfig,
    stop: &AtomicBool,
    connection: &mut Connection,
) -> Result<Option<wire::Login>> {
    let started = Instant::now();
    while !stop.load(Ordering::Acquire) {
        ensure!(
            started.elapsed() < protocol::LOGIN_TIMEOUT,
            "Radius client did not log in within 15 seconds"
        );
        let messages = connection.poll()?;
        if messages.is_empty() {
            thread::sleep(POLL_INTERVAL);
            continue;
        }
        ensure!(
            messages.len() == 1,
            "Login must complete before media starts"
        );
        let Message::Login(login) = messages.into_iter().next().unwrap() else {
            bail!("first Radius message must be Login");
        };
        if let Err(error) = protocol::validate_login(&login, config.run_id) {
            connection.send(&Message::ErrorReply(wire::ErrorReply {
                command_id: login.command_id,
                run_id: config.run_id,
                message: error.to_string(),
            }))?;
            return Err(error);
        }
        return Ok(Some(login));
    }
    Ok(None)
}

fn run_connected(
    config: &RxConfig,
    stop: &AtomicBool,
    connection: &mut Connection,
    login: wire::Login,
    health_file: Option<&Path>,
) -> Result<()> {
    ensure!(
        login.audio_enabled == config.audio_enabled && login.lighting_enabled == config.dmx_enabled,
        "Login outputs differ from receiver deployment settings"
    );
    let mut playback = if login.audio_enabled {
        Some(PlaybackStream::start(&config.audio_device_id)?)
    } else {
        None
    };
    let dmx = if login.lighting_enabled {
        let playback = playback.as_ref().context("lighting requires audio")?;
        Some(DmxWorker::start(
            config,
            playback.clock(),
            playback.lighting_updates(),
        )?)
    } else {
        None
    };
    ensure!(
        connection.send(&Message::LoginReply(protocol::login_reply(&login)))?,
        "cannot enqueue LoginReply"
    );
    let mut receiver = MediaReceiver::default();
    let mut heartbeat_received = Instant::now();
    let mut heartbeat_sent = Instant::now();
    let mut heartbeats_received = 0;
    let mut heartbeats_sent = 0;
    let mut metrics_at = Instant::now() - protocol::METRICS_INTERVAL;
    let mut reported_priority_denied = false;
    let mut audio_at = None;
    let mut audio_max_gap: f64 = 0.0;
    let mut audio_peak: f32 = 0.0;
    let mut health_error_at: Option<Instant> = None;
    let mut gap_logged_at: Option<Instant> = None;
    let output_started = Instant::now();
    while !stop.load(Ordering::Acquire) {
        for message in connection.poll()? {
            message.validate(config.run_id)?;
            match message {
                Message::Heartbeat(_) => {
                    heartbeat_received = Instant::now();
                    heartbeats_received += 1;
                }
                Message::LightFrame(light) => receiver.cache_light(light)?,
                Message::AudioFrame(audio) if playback.is_some() => {
                    audio_peak = audio
                        .interleaved_samples
                        .iter()
                        .fold(audio_peak, |peak, value| peak.max(value.abs()));
                    let admitted = receiver.resolve_audio(audio)?;
                    if admitted.silence_frames != 0
                        && gap_logged_at.is_none_or(|at| at.elapsed() >= GAP_LOG_INTERVAL)
                    {
                        eprintln!(
                            "Radius transmitter dropped audio under backpressure: {:.0} ms filled with silence ({} gaps total)",
                            admitted.silence_frames as f64 * 1000.0
                                / f64::from(protocol::SAMPLE_RATE),
                            receiver.audio_gaps_total
                        );
                        gap_logged_at = Some(Instant::now());
                    }
                    let playback = playback.as_mut().unwrap();
                    for block in admitted.blocks() {
                        playback.enqueue(block)?;
                    }
                    if let Some(at) = audio_at {
                        audio_max_gap =
                            audio_max_gap.max(Instant::now().duration_since(at).as_secs_f64());
                    }
                    audio_at = Some(Instant::now());
                }
                _ => bail!("unexpected Radius client message after Login"),
            }
        }
        ensure!(
            heartbeat_received.elapsed() < protocol::HEARTBEAT_TIMEOUT,
            "Radius transmitter heartbeat timed out after 30 seconds"
        );
        if heartbeat_sent.elapsed() >= protocol::HEARTBEAT_INTERVAL
            && connection.send(&Message::Heartbeat(wire::Heartbeat {
                run_id: config.run_id,
            }))?
        {
            heartbeat_sent = Instant::now();
            heartbeats_sent += 1;
        }
        if let Some(playback) = &playback {
            if dmx.is_none() {
                while playback.take_light().is_some() {}
            }
            let stats = playback.metrics();
            ensure!(
                !stats.faulted,
                "audio output failed: {}; check endpoint and RDP audio redirection",
                playback.last_error().unwrap_or("playback timeline invalid")
            );
            ensure!(
                output_started.elapsed() < CALLBACK_STALE_TIMEOUT
                    || stats
                        .last_callback_age
                        .is_some_and(|age| age < CALLBACK_STALE_TIMEOUT),
                "Radius audio output stopped producing callbacks"
            );
            if stats.priority_denied != 0 && !reported_priority_denied {
                eprintln!(
                    "Radius audio output runs without realtime scheduling; underruns are more likely under load"
                );
                reported_priority_denied = true;
            }
        }
        if let Some(dmx) = &dmx {
            let stats = dmx.metrics.lock().unwrap();
            ensure!(
                stats.error.is_none(),
                "DMX output failed: {:?}",
                stats.error
            );
        }
        if metrics_at.elapsed() >= protocol::METRICS_INTERVAL {
            let mut metrics = wire::Metrics {
                run_id: config.run_id,
                last_processed_command_id: login.command_id,
                up: true,
                link_up: true,
                heartbeat_age_seconds: heartbeat_received.elapsed().as_secs_f64(),
                heartbeats_sent,
                heartbeats_received,
                buffer_target_seconds: f64::from(login.jitter_buffer_ms) / 1000.0,
                audio_input_age_seconds: audio_at.map(|at| at.elapsed().as_secs_f64()),
                audio_max_gap_seconds: audio_at
                    .map(|at| audio_max_gap.max(at.elapsed().as_secs_f64())),
                audio_signal_peak: audio_peak,
                lost_frames_total: receiver.lost_frames_total,
                audio_gaps_total: receiver.audio_gaps_total,
                ..Default::default()
            };
            if let Some(playback) = &playback {
                let stats = playback.metrics();
                metrics.audio_buffer_seconds =
                    stats.buffered_frames as f64 / f64::from(protocol::SAMPLE_RATE);
                metrics.underruns_total = stats.underruns;
                metrics.device_underruns_total = stats.device_xruns;
                metrics.overflow_frames_total = stats.queue_overflow_frames;
                metrics.clock_correction_ppm = stats.drift_ppm;
                metrics.rebuffer_count = stats.rebuffer_count;
                metrics.playing = playback.clock().snapshot().position_valid;
            }
            if let Some(dmx) = &dmx {
                let stats = dmx.metrics.lock().unwrap();
                metrics.dmx_connected = stats.connected;
                metrics.dmx_frames_total = stats.frames;
                metrics.dmx_lateness_seconds = stats.lateness_seconds;
            }
            ensure!(
                connection.send(&Message::Metrics(metrics))?,
                "Radius client is not reading receiver metrics"
            );
            if let Some(path) = health_file
                && let Err(error) = std::fs::write(
                    path,
                    serde_json::to_vec(&serde_json::json!({"run_id":config.run_id,"rx":metrics}))?,
                )
                && health_error_at.is_none_or(|at| at.elapsed() >= HEALTH_ERROR_LOG_INTERVAL)
            {
                eprintln!("Radius receiver telemetry unavailable: {error}");
                health_error_at = Some(Instant::now());
            }
            metrics_at = Instant::now();
            audio_max_gap = 0.0;
            audio_peak = 0.0;
        }
        thread::sleep(POLL_INTERVAL);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn temporary_blackout_preserves_the_held_look_and_future_updates() {
        let updates = ArrayQueue::new(4);
        updates
            .push(LightingUpdate {
                audio_frame_position: 100,
                sequence_number: 1,
                channels: [17; protocol::DMX_CHANNELS],
            })
            .unwrap();
        updates
            .push(LightingUpdate {
                audio_frame_position: 200,
                sequence_number: 2,
                channels: [99; protocol::DMX_CHANNELS],
            })
            .unwrap();
        let mut lighting = LightingPlayback::new();
        let mut audible = AudibleSnapshot {
            audio_frame_position: 100,
            position_valid: true,
            callback_age: Some(Duration::ZERO),
        };
        assert_eq!(
            lighting.frame(&audible, &updates),
            [17; protocol::DMX_CHANNELS]
        );
        audible.position_valid = false;
        assert_eq!(
            lighting.frame(&audible, &updates),
            [0; protocol::DMX_CHANNELS]
        );
        audible.position_valid = true;
        assert_eq!(
            lighting.frame(&audible, &updates),
            [17; protocol::DMX_CHANNELS]
        );
        audible.audio_frame_position = 200;
        assert_eq!(
            lighting.frame(&audible, &updates),
            [99; protocol::DMX_CHANNELS]
        );
        audible.callback_age = Some(CALLBACK_STALE_TIMEOUT + Duration::from_millis(1));
        assert_eq!(
            lighting.frame(&audible, &updates),
            [0; protocol::DMX_CHANNELS]
        );
    }
}
