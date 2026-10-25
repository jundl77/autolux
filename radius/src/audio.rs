//! Explicit-device WASAPI audio. Callbacks own their mutable DSP state and use fixed queues.
//! CPAL and Windows schedule the callback threads; worker CPU affinity does not control them.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use crossbeam_queue::ArrayQueue;

pub use crate::playout::{EnqueueError, JITTER_BUFFER_MS, LightingUpdate, PlaybackBlock};
use crate::playout::{Playout, PlayoutSender};

pub const SAMPLE_RATE: u32 = 44_100;
pub const CHANNELS: usize = 2;
pub const BLOCK_FRAMES: usize = 128;
pub const SHOW_DELAY_SECONDS: f64 = 14.0;
const CAPTURE_QUEUE_BLOCKS: usize = 256;
const OUTPUT_HISTORY_FRAMES: usize = 131_072;
const SILENCE_POSITION: u64 = u64::MAX;
const STREAM_OPEN_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Debug)]
pub struct AudioBlock {
    pub audio_frame_position: u64,
    /// TX completion time after the show-delay ring, retained through network backpressure.
    pub assembly_completed_at: Instant,
    pub frames: u16,
    pub samples: [f32; BLOCK_FRAMES * CHANNELS],
}

impl Default for AudioBlock {
    fn default() -> Self {
        Self {
            audio_frame_position: 0,
            assembly_completed_at: Instant::now(),
            frames: 0,
            samples: [0.0; BLOCK_FRAMES * CHANNELS],
        }
    }
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct AudioDevice {
    pub id: String,
    pub name: String,
    pub input: bool,
    pub output: bool,
}

fn audio_host() -> Result<cpal::Host> {
    #[cfg(windows)]
    {
        cpal::host_from_id(cpal::HostId::Wasapi).context("open WASAPI audio host")
    }
    #[cfg(not(windows))]
    {
        anyhow::bail!("Radius hardware audio currently requires Windows WASAPI")
    }
}

pub fn list_devices() -> Result<Vec<AudioDevice>> {
    audio_host()?
        .devices()?
        .map(|device| {
            let description = device.description()?;
            Ok(AudioDevice {
                id: device.id()?.id().to_owned(),
                name: description.name().to_owned(),
                input: description.supports_input(),
                output: description.supports_output(),
            })
        })
        .collect()
}

fn select_device(device_id: &str, input: bool) -> Result<cpal::Device> {
    ensure!(
        !device_id.is_empty() && device_id != "default",
        "an explicit audio endpoint ID is required"
    );
    let host = audio_host()?;
    let device = host
        .devices()?
        .find(|device| {
            device
                .id()
                .is_ok_and(|id| id.id() == device_id || id.to_string() == device_id)
        })
        .with_context(|| format!("configured audio endpoint {device_id:?} was not found"))?;
    let description = device.description()?;
    ensure!(
        if input {
            description.supports_input()
        } else {
            description.supports_output()
        },
        "audio endpoint {device_id:?} does not support {}",
        if input { "capture" } else { "playback" }
    );
    Ok(device)
}

fn stream_config() -> cpal::StreamConfig {
    cpal::StreamConfig {
        channels: CHANNELS as u16,
        sample_rate: SAMPLE_RATE,
        buffer_size: cpal::BufferSize::Default,
    }
}

/// Resolves the exact endpoint and verifies a stereo Float32 44.1 kHz stream can be opened.
/// No stream is started and no audio is captured or played.
pub fn check_device(device_id: &str, input: bool) -> Result<AudioDevice> {
    let device = select_device(device_id, input)?;
    let description = device.description()?;
    let _stream = if input {
        device.build_input_stream::<f32, _, _>(
            stream_config(),
            |_, _| {},
            |_| {},
            Some(STREAM_OPEN_TIMEOUT),
        )
    } else {
        device.build_output_stream::<f32, _, _>(
            stream_config(),
            |output, _| output.fill(0.0),
            |_| {},
            Some(STREAM_OPEN_TIMEOUT),
        )
    }
    .context("open stereo Float32 44100 Hz stream on configured endpoint")?;
    Ok(AudioDevice {
        id: device.id()?.id().to_owned(),
        name: description.name().to_owned(),
        input: description.supports_input(),
        output: description.supports_output(),
    })
}

struct AudioDelay {
    samples: Box<[[f32; CHANNELS]]>,
    offset: usize,
}

impl AudioDelay {
    fn new(seconds: f64) -> Result<Self> {
        ensure!(
            seconds.is_finite() && (0.0..=120.0).contains(&seconds),
            "audio show delay must be finite and within 0..120 seconds"
        );
        Ok(Self {
            samples: vec![[0.0; CHANNELS]; (seconds * f64::from(SAMPLE_RATE)).round() as usize]
                .into_boxed_slice(),
            offset: 0,
        })
    }

    fn frame(&mut self, mut input: [f32; CHANNELS]) -> [f32; CHANNELS] {
        if !self.samples.is_empty() {
            std::mem::swap(&mut input, &mut self.samples[self.offset]);
            self.offset += 1;
            if self.offset == self.samples.len() {
                self.offset = 0;
            }
        }
        input
    }
}

struct StreamCounters {
    epoch: Instant,
    callbacks: AtomicU64,
    last_callback_micros: AtomicU64,
    device_xruns: AtomicU64,
    stream_errors: AtomicU64,
    last_error_code: AtomicU32,
    faulted: AtomicBool,
}

impl StreamCounters {
    fn new() -> Self {
        Self {
            epoch: Instant::now(),
            callbacks: AtomicU64::new(0),
            last_callback_micros: AtomicU64::new(0),
            device_xruns: AtomicU64::new(0),
            stream_errors: AtomicU64::new(0),
            last_error_code: AtomicU32::new(0),
            faulted: AtomicBool::new(false),
        }
    }

    fn callback(&self) {
        self.last_callback_micros
            .store(self.epoch.elapsed().as_micros() as u64, Ordering::Relaxed);
        self.callbacks.fetch_add(1, Ordering::Release);
    }

    fn callback_age(&self) -> Option<Duration> {
        (self.callbacks.load(Ordering::Acquire) != 0).then(|| {
            self.epoch.elapsed().saturating_sub(Duration::from_micros(
                self.last_callback_micros.load(Ordering::Relaxed),
            ))
        })
    }

    fn error(&self, error: cpal::Error) {
        if error.kind() == cpal::ErrorKind::Xrun {
            self.device_xruns.fetch_add(1, Ordering::Relaxed);
        } else {
            let code = match error.kind() {
                cpal::ErrorKind::DeviceNotAvailable => 1,
                cpal::ErrorKind::StreamInvalidated => 2,
                cpal::ErrorKind::DeviceChanged => 3,
                cpal::ErrorKind::PermissionDenied => 4,
                cpal::ErrorKind::RealtimeDenied => 5,
                _ => 6,
            };
            self.last_error_code.store(code, Ordering::Relaxed);
            self.stream_errors.fetch_add(1, Ordering::Relaxed);
        }
        // Capture xruns invalidate the sample timeline; output xruns invalidate audible timing.
        self.faulted.store(true, Ordering::Release);
    }

    fn error_message(&self) -> Option<&'static str> {
        match self.last_error_code.load(Ordering::Relaxed) {
            1 => Some("audio endpoint disconnected"),
            2 => Some("audio stream invalidated"),
            3 => Some("audio endpoint changed"),
            4 => Some("audio endpoint permission denied"),
            5 => Some("audio realtime scheduling denied"),
            6 => Some("audio backend failed"),
            _ if self.device_xruns.load(Ordering::Relaxed) != 0 => {
                Some("audio device overrun/underrun invalidated the sample timeline")
            }
            _ => None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CaptureMetrics {
    pub callbacks: u64,
    pub captured_frames: u64,
    pub queue_overflow_frames: u64,
    pub device_xruns: u64,
    pub stream_errors: u64,
    pub last_callback_age: Option<Duration>,
    pub queued_frames: usize,
    pub faulted: bool,
}

struct CaptureAssembler {
    delay: AudioDelay,
    partial: AudioBlock,
    queue: Arc<ArrayQueue<AudioBlock>>,
    captured_frames: Arc<AtomicU64>,
    queue_overflow_frames: Arc<AtomicU64>,
    counters: Arc<StreamCounters>,
}

impl CaptureAssembler {
    fn capture(&mut self, input: &[f32]) {
        self.counters.callback();
        if self.counters.faulted.load(Ordering::Acquire) {
            return;
        }
        if !input.len().is_multiple_of(CHANNELS) {
            self.counters.stream_errors.fetch_add(1, Ordering::Relaxed);
            self.counters.faulted.store(true, Ordering::Release);
            return;
        }
        for frame in input.as_chunks::<CHANNELS>().0 {
            let frame = self.delay.frame([frame[0], frame[1]]);
            let offset = usize::from(self.partial.frames) * CHANNELS;
            self.partial.samples[offset..offset + CHANNELS].copy_from_slice(&frame);
            self.partial.frames += 1;
            self.captured_frames.fetch_add(1, Ordering::Relaxed);
            if usize::from(self.partial.frames) == BLOCK_FRAMES {
                let next_position = self.partial.audio_frame_position + BLOCK_FRAMES as u64;
                let mut block = std::mem::take(&mut self.partial);
                self.partial.audio_frame_position = next_position;
                block.assembly_completed_at = Instant::now();
                if self.queue.push(block).is_err() {
                    self.queue_overflow_frames
                        .fetch_add(BLOCK_FRAMES as u64, Ordering::Relaxed);
                    self.counters.faulted.store(true, Ordering::Release);
                    return;
                }
            }
        }
    }
}

pub struct CaptureStream {
    _stream: cpal::Stream,
    queue: Arc<ArrayQueue<AudioBlock>>,
    counters: Arc<StreamCounters>,
    captured_frames: Arc<AtomicU64>,
    queue_overflow_frames: Arc<AtomicU64>,
}

impl CaptureStream {
    pub fn start(device_id: &str, show_delay_seconds: f64) -> Result<Self> {
        let delay = AudioDelay::new(show_delay_seconds)?;
        let device = select_device(device_id, true)?;
        let counters = Arc::new(StreamCounters::new());
        let queue = Arc::new(ArrayQueue::new(CAPTURE_QUEUE_BLOCKS));
        let captured_frames = Arc::new(AtomicU64::new(0));
        let queue_overflow_frames = Arc::new(AtomicU64::new(0));
        let mut assembler = CaptureAssembler {
            delay,
            partial: AudioBlock::default(),
            queue: queue.clone(),
            captured_frames: captured_frames.clone(),
            queue_overflow_frames: queue_overflow_frames.clone(),
            counters: counters.clone(),
        };
        let errors = counters.clone();
        let stream = device
            .build_input_stream::<f32, _, _>(
                stream_config(),
                move |input, _| assembler.capture(input),
                move |error| errors.error(error),
                Some(STREAM_OPEN_TIMEOUT),
            )
            .context("open capture: stereo Float32 44100 Hz")?;
        stream.play().context("start capture")?;
        Ok(Self {
            _stream: stream,
            queue,
            counters,
            captured_frames,
            queue_overflow_frames,
        })
    }

    pub fn pop(&self) -> Option<AudioBlock> {
        self.queue.pop()
    }

    pub fn last_error(&self) -> Option<&'static str> {
        self.counters.error_message().or_else(|| {
            (self.queue_overflow_frames.load(Ordering::Relaxed) != 0)
                .then_some("capture queue overflowed; the consumer stopped keeping up")
        })
    }

    pub fn metrics(&self) -> CaptureMetrics {
        CaptureMetrics {
            callbacks: self.counters.callbacks.load(Ordering::Acquire),
            captured_frames: self.captured_frames.load(Ordering::Relaxed),
            queue_overflow_frames: self.queue_overflow_frames.load(Ordering::Relaxed),
            device_xruns: self.counters.device_xruns.load(Ordering::Relaxed),
            stream_errors: self.counters.stream_errors.load(Ordering::Relaxed),
            last_callback_age: self.counters.callback_age(),
            queued_frames: self.queue.len() * BLOCK_FRAMES,
            faulted: self.counters.faulted.load(Ordering::Acquire),
        }
    }
}

#[derive(Clone, Debug)]
pub struct AudibleSnapshot {
    pub audio_frame_position: u64,
    /// The source position currently reaching the device is known. Zero is a valid position.
    /// False during startup/rebuffer silence, failed output, or an inconsistent clock read.
    pub position_valid: bool,
    pub callback_age: Option<Duration>,
    pub device_latency_frames: u64,
}

pub struct AudibleClock {
    sequence: AtomicU64,
    audio_frame_position: AtomicU64,
    position_valid: AtomicBool,
    device_latency_frames: AtomicU64,
    counters: Arc<StreamCounters>,
}

impl AudibleClock {
    fn publish(&self, audio_frame_position: Option<u64>, device_latency_frames: u64) {
        self.sequence.fetch_add(1, Ordering::SeqCst);
        if let Some(position) = audio_frame_position {
            self.audio_frame_position.store(position, Ordering::SeqCst);
        }
        self.position_valid
            .store(audio_frame_position.is_some(), Ordering::SeqCst);
        self.device_latency_frames
            .store(device_latency_frames, Ordering::SeqCst);
        self.sequence.fetch_add(1, Ordering::SeqCst);
    }

    pub fn snapshot(&self) -> AudibleSnapshot {
        // A stalled writer must never hold the DMX worker in a spin loop.
        for _ in 0..3 {
            let before = self.sequence.load(Ordering::SeqCst);
            if before & 1 != 0 {
                continue;
            }
            let snapshot = AudibleSnapshot {
                audio_frame_position: self.audio_frame_position.load(Ordering::SeqCst),
                position_valid: self.position_valid.load(Ordering::SeqCst)
                    && !self.counters.faulted.load(Ordering::Acquire),
                callback_age: self.counters.callback_age(),
                device_latency_frames: self.device_latency_frames.load(Ordering::SeqCst),
            };
            if before == self.sequence.load(Ordering::SeqCst) {
                return snapshot;
            }
        }
        AudibleSnapshot {
            audio_frame_position: self.audio_frame_position.load(Ordering::Acquire),
            position_valid: false,
            callback_age: self.counters.callback_age(),
            device_latency_frames: self.device_latency_frames.load(Ordering::Acquire),
        }
    }
}

struct OutputHistory {
    positions: Box<[u64]>,
    output_frames: u64,
}

impl OutputHistory {
    fn new() -> Self {
        Self {
            positions: vec![SILENCE_POSITION; OUTPUT_HISTORY_FRAMES].into_boxed_slice(),
            output_frames: 0,
        }
    }

    fn push(&mut self, position: Option<u64>) {
        self.positions[self.output_frames as usize % OUTPUT_HISTORY_FRAMES] =
            position.unwrap_or(SILENCE_POSITION);
        self.output_frames += 1;
    }

    fn audible_position(&self, callback_start_frame: u64, pending_frames: u64) -> Option<u64> {
        // Timestamp describes the *first* frame about to be submitted, not the end of the callback.
        let frame = callback_start_frame.checked_sub(pending_frames)?;
        if frame >= self.output_frames || self.output_frames - frame > OUTPUT_HISTORY_FRAMES as u64
        {
            return None;
        }
        let position = self.positions[frame as usize % OUTPUT_HISTORY_FRAMES];
        (position != SILENCE_POSITION).then_some(position)
    }
}

#[derive(Debug, Clone)]
pub struct PlaybackMetrics {
    pub callbacks: u64,
    pub stream_errors: u64,
    pub device_xruns: u64,
    pub queue_overflow_frames: u64,
    pub underruns: u64,
    pub rebuffer_count: u64,
    pub buffered_frames: u64,
    pub drift_ppm: i32,
    pub lighting_overflows: u64,
    pub last_callback_age: Option<Duration>,
    pub faulted: bool,
}

pub struct PlaybackStream {
    _stream: cpal::Stream,
    sender: PlayoutSender,
    clock: Arc<AudibleClock>,
    counters: Arc<StreamCounters>,
}

impl PlaybackStream {
    pub fn start(device_id: &str, jitter_buffer_ms: u32) -> Result<Self> {
        let (mut playout, sender) = Playout::new(jitter_buffer_ms)?;
        let device = select_device(device_id, false)?;
        let counters = Arc::new(StreamCounters::new());
        let clock = Arc::new(AudibleClock {
            sequence: AtomicU64::new(0),
            audio_frame_position: AtomicU64::new(0),
            position_valid: AtomicBool::new(false),
            device_latency_frames: AtomicU64::new(0),
            counters: counters.clone(),
        });
        let callback_clock = clock.clone();
        let callback_counters = counters.clone();
        let errors = counters.clone();
        let mut history = OutputHistory::new();
        let stream = device
            .build_output_stream::<f32, _, _>(
                stream_config(),
                move |output, info| {
                    callback_counters.callback();
                    if callback_counters.faulted.load(Ordering::Acquire) {
                        playout.counters.faulted.store(true, Ordering::Release);
                    }
                    let timestamp = info.timestamp();
                    let pending_frames = (timestamp
                        .playback
                        .duration_since(timestamp.callback)
                        .as_secs_f64()
                        * f64::from(SAMPLE_RATE))
                    .ceil() as u64;
                    let callback_start_frame = history.output_frames;
                    playout.begin_callback();
                    for frame in output.as_chunks_mut::<CHANNELS>().0 {
                        history.push(playout.frame(frame));
                    }
                    let audible = history.audible_position(callback_start_frame, pending_frames);
                    callback_clock.publish(
                        if playout.counters.faulted.load(Ordering::Acquire) {
                            None
                        } else {
                            audible
                        },
                        pending_frames,
                    );
                },
                move |error| errors.error(error),
                Some(STREAM_OPEN_TIMEOUT),
            )
            .context("open playback: stereo Float32 44100 Hz")?;
        stream.play().context("start playback")?;
        Ok(Self {
            _stream: stream,
            sender,
            clock,
            counters,
        })
    }

    pub fn enqueue(&mut self, block: PlaybackBlock) -> Result<(), EnqueueError> {
        if self.counters.faulted.load(Ordering::Acquire) {
            return Err(EnqueueError::Faulted);
        }
        self.sender.enqueue(block)
    }

    pub fn clock(&self) -> Arc<AudibleClock> {
        self.clock.clone()
    }

    pub fn take_light(&self) -> Option<LightingUpdate> {
        self.sender.lighting.pop()
    }

    pub fn lighting_updates(&self) -> Arc<ArrayQueue<LightingUpdate>> {
        self.sender.lighting.clone()
    }

    pub fn last_error(&self) -> Option<&'static str> {
        self.counters.error_message()
    }

    pub fn metrics(&self) -> PlaybackMetrics {
        let playout = &self.sender.counters;
        PlaybackMetrics {
            callbacks: self.counters.callbacks.load(Ordering::Acquire),
            stream_errors: self.counters.stream_errors.load(Ordering::Relaxed),
            device_xruns: self.counters.device_xruns.load(Ordering::Relaxed),
            queue_overflow_frames: playout.queue_overflow_frames.load(Ordering::Relaxed),
            underruns: playout.underruns.load(Ordering::Relaxed),
            rebuffer_count: playout.rebuffer_count.load(Ordering::Relaxed),
            buffered_frames: playout.buffered_frames.load(Ordering::Relaxed),
            drift_ppm: playout.drift_ppm.load(Ordering::Relaxed),
            lighting_overflows: playout.lighting_overflows.load(Ordering::Relaxed),
            last_callback_age: self.counters.callback_age(),
            faulted: self.counters.faulted.load(Ordering::Acquire)
                || playout.faulted.load(Ordering::Acquire),
        }
    }
}

#[cfg(test)]
#[path = "../tests/rust/audio.rs"]
mod tests;
