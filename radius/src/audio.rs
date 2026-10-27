//! Explicit-device WASAPI audio. Callbacks own their mutable DSP state and use fixed queues.
//! CPAL and Windows schedule the callback threads; worker CPU affinity does not control them.

use std::sync::Arc;
use std::sync::atomic::Ordering::{AcqRel, Acquire, Relaxed, Release, SeqCst};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU8, AtomicU32, AtomicU64};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use crossbeam_queue::ArrayQueue;

pub use crate::playout::{EnqueueError, JITTER_BUFFER_MS, LightingUpdate, PlaybackBlock};
use crate::playout::{Playout, PlayoutSender};
use crate::realtime::{self, Scheduling};

pub const SAMPLE_RATE: u32 = 44_100;
pub const CHANNELS: usize = 2;
pub const BLOCK_FRAMES: usize = 128;
pub const SHOW_DELAY_SECONDS: f64 = 14.0;
const CAPTURE_QUEUE_BLOCKS: usize = (SAMPLE_RATE as usize / 2).div_ceil(BLOCK_FRAMES);
const OUTPUT_HISTORY_FRAMES: usize = 131_072;
const SILENCE_POSITION: u64 = u64::MAX;
const DISCARD_FADE_FRAMES: usize = SAMPLE_RATE as usize / 100;
const STREAM_OPEN_TIMEOUT: Duration = Duration::from_secs(5);
const STALL_TOLERANT_CAPTURE_BUFFER_FRAMES: u32 = SAMPLE_RATE / 5;
pub const CAPTURE_DISCONTINUITY_LIMIT: usize = 3;
pub const CAPTURE_DISCONTINUITY_WINDOW: Duration = Duration::from_secs(10);
const NANOS_PER_SECOND: f64 = 1e9;
const CAPTURE_JITTER_DEAD_BAND_FRAMES: u64 = SAMPLE_RATE as u64 / 200;

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

fn stream_config(input: bool) -> cpal::StreamConfig {
    cpal::StreamConfig {
        channels: CHANNELS as u16,
        sample_rate: SAMPLE_RATE,
        buffer_size: match input {
            true => cpal::BufferSize::Fixed(STALL_TOLERANT_CAPTURE_BUFFER_FRAMES),
            false => cpal::BufferSize::Default,
        },
    }
}

/// Resolves the exact endpoint and verifies a stereo Float32 44.1 kHz stream can be opened.
/// No stream is started and no audio is captured or played.
pub fn check_device(device_id: &str, input: bool) -> Result<AudioDevice> {
    let device = select_device(device_id, input)?;
    let description = device.description()?;
    let _stream = if input {
        device.build_input_stream::<f32, _, _>(
            stream_config(true),
            |_, _| {},
            |_| {},
            Some(STREAM_OPEN_TIMEOUT),
        )
    } else {
        device.build_output_stream::<f32, _, _>(
            stream_config(false),
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
    discarded_frames_remaining: usize,
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
            discarded_frames_remaining: 0,
        })
    }

    fn discard_buffered(&mut self) {
        self.discarded_frames_remaining = self.samples.len();
    }

    fn frame(&mut self, mut input: [f32; CHANNELS]) -> [f32; CHANNELS] {
        if !self.samples.is_empty() {
            std::mem::swap(&mut input, &mut self.samples[self.offset]);
            self.offset += 1;
            if self.offset == self.samples.len() {
                self.offset = 0;
            }
        }
        if self.discarded_frames_remaining == 0 {
            return input;
        }
        let discarded = self.samples.len() - self.discarded_frames_remaining;
        self.discarded_frames_remaining -= 1;
        let gain = 1.0 - (discarded + 1) as f32 / DISCARD_FADE_FRAMES as f32;
        input.map(|sample| sample * gain.max(0.0))
    }
}

const ENDPOINT_DISCONNECTED: u32 = 1;
const STREAM_INVALIDATED: u32 = 2;
const ENDPOINT_CHANGED: u32 = 3;
const PERMISSION_DENIED: u32 = 4;
const BACKEND_FAILED: u32 = 6;
const CAPTURE_REPEATED_DISCONTINUITY: u32 = 7;
const CAPTURE_GAP_EXCEEDS_QUEUE: u32 = 9;
const CAPTURE_QUEUE_OVERFLOW: u32 = 10;
const PARTIAL_FRAME: u32 = 11;
const PLAYBACK_XRUN: u32 = 12;
const CAPTURE_GAP_UNMEASURED: u32 = 13;

struct Epoch(Instant);

impl Default for Epoch {
    fn default() -> Self {
        Self(Instant::now())
    }
}

#[derive(Default)]
struct StreamCounters {
    epoch: Epoch,
    callbacks: AtomicU64,
    last_callback_micros: AtomicU64,
    device_xruns: AtomicU64,
    stream_errors: AtomicU64,
    priority_denied: AtomicU64,
    last_error_code: AtomicU32,
    faulted: AtomicBool,
    bridges_xruns: bool,
    xrun_pending: AtomicBool,
}

impl StreamCounters {
    fn callback(&self) {
        self.last_callback_micros
            .store(self.epoch.0.elapsed().as_micros() as u64, Relaxed);
        self.callbacks.fetch_add(1, Release);
    }

    fn callback_age(&self) -> Option<Duration> {
        (self.callbacks.load(Acquire) != 0).then(|| {
            self.epoch.0.elapsed().saturating_sub(Duration::from_micros(
                self.last_callback_micros.load(Relaxed),
            ))
        })
    }

    fn error(&self, error: cpal::Error) {
        let code = match error.kind() {
            cpal::ErrorKind::RealtimeDenied => {
                self.priority_denied.fetch_add(1, Relaxed);
                return;
            }
            cpal::ErrorKind::Xrun => {
                self.device_xruns.fetch_add(1, Relaxed);
                if self.bridges_xruns {
                    self.xrun_pending.store(true, Release);
                    return;
                }
                PLAYBACK_XRUN
            }
            kind => {
                self.stream_errors.fetch_add(1, Relaxed);
                match kind {
                    cpal::ErrorKind::DeviceNotAvailable => ENDPOINT_DISCONNECTED,
                    cpal::ErrorKind::StreamInvalidated => STREAM_INVALIDATED,
                    cpal::ErrorKind::DeviceChanged => ENDPOINT_CHANGED,
                    cpal::ErrorKind::PermissionDenied => PERMISSION_DENIED,
                    _ => BACKEND_FAILED,
                }
            }
        };
        self.fault(code);
    }

    fn fault(&self, code: u32) {
        self.last_error_code.store(code, Relaxed);
        self.faulted.store(true, Release);
    }

    fn error_message(&self) -> Option<&'static str> {
        match self.last_error_code.load(Relaxed) {
            ENDPOINT_DISCONNECTED => Some("audio endpoint disconnected"),
            STREAM_INVALIDATED => Some("audio stream invalidated"),
            ENDPOINT_CHANGED => Some("audio endpoint changed"),
            PERMISSION_DENIED => Some("audio endpoint permission denied"),
            BACKEND_FAILED => Some("audio backend failed"),
            CAPTURE_REPEATED_DISCONTINUITY => Some("capture discontinuities repeated too often"),
            CAPTURE_GAP_EXCEEDS_QUEUE => Some("a capture gap exceeded the capture queue headroom"),
            CAPTURE_GAP_UNMEASURED => {
                Some("a capture discontinuity arrived without a device timestamp")
            }
            CAPTURE_QUEUE_OVERFLOW => {
                Some("capture queue overflowed; the consumer stopped keeping up")
            }
            PARTIAL_FRAME => Some("audio callback delivered a partial stereo frame"),
            PLAYBACK_XRUN => Some("audio output underrun invalidated audible timing"),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct CaptureMetrics {
    pub callbacks: u64,
    pub device_frames: u64,
    pub queue_overflow_frames: u64,
    pub device_xruns: u64,
    pub discontinuities: u64,
    pub last_gap_frames: Option<i64>,
    pub resync_silence_frames: u64,
    pub resync_dropped_frames: u64,
    pub stream_errors: u64,
    pub capture_priority_denied: u64,
    /// None until the first capture callback has run.
    pub capture_scheduling: Option<Scheduling>,
    pub last_callback_age: Option<Duration>,
    pub queued_frames: usize,
    pub faulted: bool,
}

#[derive(Default)]
struct CaptureCounters {
    stream: StreamCounters,
    device_frames: AtomicU64,
    queue_overflow_frames: AtomicU64,
    discard_requested: AtomicBool,
    discontinuities: AtomicU64,
    last_gap_frames: AtomicI64,
    silence_frames: AtomicU64,
    dropped_frames: AtomicU64,
    scheduling: AtomicU8,
}

const SCHEDULING_UNKNOWN: u8 = 0;
const SCHEDULING: [Scheduling; 3] = [
    Scheduling::Mmcss,
    Scheduling::TimeCritical,
    Scheduling::Denied,
];

impl CaptureCounters {
    fn record_scheduling(&self, scheduling: Scheduling) {
        if scheduling == Scheduling::Denied {
            self.stream.priority_denied.fetch_add(1, Relaxed);
        }
        let code = SCHEDULING.iter().position(|&s| s == scheduling).unwrap() as u8 + 1;
        self.scheduling.store(code, Release);
    }

    fn metrics(&self, queued_frames: usize) -> CaptureMetrics {
        let discontinuities = self.discontinuities.load(Acquire);
        CaptureMetrics {
            callbacks: self.stream.callbacks.load(Acquire),
            device_frames: self.device_frames.load(Relaxed),
            queue_overflow_frames: self.queue_overflow_frames.load(Relaxed),
            device_xruns: self.stream.device_xruns.load(Relaxed),
            discontinuities,
            last_gap_frames: (discontinuities != 0).then(|| self.last_gap_frames.load(Relaxed)),
            resync_silence_frames: self.silence_frames.load(Relaxed),
            resync_dropped_frames: self.dropped_frames.load(Relaxed),
            stream_errors: self.stream.stream_errors.load(Relaxed),
            capture_priority_denied: self.stream.priority_denied.load(Relaxed),
            capture_scheduling: match self.scheduling.load(Acquire) {
                SCHEDULING_UNKNOWN => None,
                code => Some(SCHEDULING[usize::from(code - 1)]),
            },
            last_callback_age: self.stream.callback_age(),
            queued_frames,
            faulted: self.stream.faulted.load(Acquire),
        }
    }
}

fn frame_nanos(frames: u64) -> u128 {
    (frames as f64 * NANOS_PER_SECOND / f64::from(SAMPLE_RATE)).round() as u128
}

#[derive(Clone, Copy)]
struct CaptureTiming {
    captured_at_nanos: u128,
    callback_nanos: u128,
    callback_at: Instant,
}

impl CaptureTiming {
    fn instant(&self, device_nanos: u128) -> Instant {
        if self.captured_at_nanos == 0 {
            return self.callback_at;
        }
        let after = |a: u128, b: u128| {
            Duration::from_nanos(u64::try_from(a.saturating_sub(b)).unwrap_or(u64::MAX))
        };
        let before = self
            .callback_at
            .checked_sub(after(self.callback_nanos, device_nanos));
        before
            .and_then(|at| at.checked_add(after(device_nanos, self.callback_nanos)))
            .unwrap_or(self.callback_at)
    }
}

#[derive(Default)]
struct CaptureTimeline {
    next_frame_nanos: Option<u128>,
    recent_discontinuities: [Option<u128>; CAPTURE_DISCONTINUITY_LIMIT - 1],
    repeated_frames: u64,
}

impl CaptureTimeline {
    fn gap_frames(&mut self, at: u128, frames: usize, flagged: bool) -> Result<Option<i64>, u32> {
        if at == 0 {
            return match flagged && self.next_frame_nanos.is_some() {
                true => Err(CAPTURE_GAP_UNMEASURED),
                false => Ok(None),
            };
        }
        let next = at + frame_nanos(frames as u64);
        let Some(expected) = self.next_frame_nanos.replace(next) else {
            return Ok(None);
        };
        let gap_nanos = at as f64 - expected as f64;
        let gap = (gap_nanos * f64::from(SAMPLE_RATE) / NANOS_PER_SECOND).round() as i64;
        Ok((flagged || gap.unsigned_abs() > CAPTURE_JITTER_DEAD_BAND_FRAMES).then_some(gap))
    }

    fn repeats_too_often(&mut self, captured_at: u128) -> bool {
        let window = CAPTURE_DISCONTINUITY_WINDOW.as_nanos();
        let recent = self
            .recent_discontinuities
            .iter()
            .flatten()
            .filter(|&&at| captured_at.saturating_sub(at) < window)
            .count();
        self.recent_discontinuities.rotate_right(1);
        self.recent_discontinuities[0] = Some(captured_at);
        recent + 1 >= CAPTURE_DISCONTINUITY_LIMIT
    }

    fn skip_repeated(&mut self, frames: usize) -> usize {
        let skipped = self.repeated_frames.min(frames as u64);
        self.repeated_frames -= skipped;
        skipped as usize
    }
}

struct CaptureAssembler {
    delay: AudioDelay,
    partial: AudioBlock,
    timeline: CaptureTimeline,
    queue: Arc<ArrayQueue<AudioBlock>>,
    counters: Arc<CaptureCounters>,
}

impl CaptureAssembler {
    fn new(delay: AudioDelay) -> Self {
        Self {
            delay,
            partial: AudioBlock::default(),
            timeline: CaptureTimeline::default(),
            queue: Arc::new(ArrayQueue::new(CAPTURE_QUEUE_BLOCKS)),
            counters: Arc::new(CaptureCounters {
                stream: StreamCounters {
                    bridges_xruns: true,
                    ..Default::default()
                },
                ..Default::default()
            }),
        }
    }

    fn capture(&mut self, input: &[f32], timing: CaptureTiming) {
        self.counters.stream.callback();
        if self.counters.stream.faulted.load(Acquire) {
            return;
        }
        if self.counters.discard_requested.swap(false, AcqRel) {
            self.delay.discard_buffered();
        }
        if let Err(code) = self.assemble(input, timing) {
            self.counters.stream.fault(code);
        }
    }

    fn assemble(&mut self, input: &[f32], timing: CaptureTiming) -> Result<(), u32> {
        if !input.len().is_multiple_of(CHANNELS) {
            self.counters.stream.stream_errors.fetch_add(1, Relaxed);
            return Err(PARTIAL_FRAME);
        }
        let input = input.as_chunks::<CHANNELS>().0;
        self.counters
            .device_frames
            .fetch_add(input.len() as u64, Relaxed);
        let at = timing.captured_at_nanos;
        let silence = self.resynchronize(at, input.len())?;
        let skipped = self.timeline.skip_repeated(input.len());
        let silent = |index| {
            (
                [0.0; CHANNELS],
                at.saturating_sub(frame_nanos(silence - index)),
            )
        };
        let audio_frames = input
            .iter()
            .enumerate()
            .skip(skipped)
            .map(|(index, &frame)| (frame, at + frame_nanos(index as u64)));
        (0..silence)
            .map(silent)
            .chain(audio_frames)
            .try_for_each(|(frame, device_nanos)| self.emit(frame, timing, device_nanos))
    }

    fn resynchronize(&mut self, captured_at: u128, frames: usize) -> Result<u64, u32> {
        let flagged = self.counters.stream.xrun_pending.swap(false, AcqRel);
        let Some(gap) = self.timeline.gap_frames(captured_at, frames, flagged)? else {
            return Ok(0);
        };
        self.counters.last_gap_frames.store(gap, Relaxed);
        self.counters.discontinuities.fetch_add(1, Release);
        if self.timeline.repeats_too_often(captured_at) {
            return Err(CAPTURE_REPEATED_DISCONTINUITY);
        }
        let queue_frames = |blocks: usize| (blocks * BLOCK_FRAMES) as u64;
        let (needed, headroom, counter) = if gap < 0 {
            let capacity = queue_frames(self.queue.capacity());
            (gap.unsigned_abs(), capacity, &self.counters.dropped_frames)
        } else {
            let free = queue_frames(self.queue.capacity() - self.queue.len());
            let needed = u64::from(self.partial.frames) + gap as u64 + frames as u64;
            (needed, free, &self.counters.silence_frames)
        };
        if needed > headroom {
            return Err(CAPTURE_GAP_EXCEEDS_QUEUE);
        }
        counter.fetch_add(gap.unsigned_abs(), Relaxed);
        self.timeline.repeated_frames += gap.min(0).unsigned_abs();
        Ok(gap.max(0) as u64)
    }

    fn emit(&mut self, frame: [f32; CHANNELS], timing: CaptureTiming, at: u128) -> Result<(), u32> {
        let frame = self.delay.frame(frame);
        let offset = usize::from(self.partial.frames) * CHANNELS;
        self.partial.samples[offset..offset + CHANNELS].copy_from_slice(&frame);
        self.partial.frames += 1;
        if usize::from(self.partial.frames) < BLOCK_FRAMES {
            return Ok(());
        }
        let next_position = self.partial.audio_frame_position + BLOCK_FRAMES as u64;
        let mut block = std::mem::take(&mut self.partial);
        self.partial.audio_frame_position = next_position;
        block.assembly_completed_at = timing.instant(at);
        self.queue.push(block).map_err(|_| {
            self.counters
                .queue_overflow_frames
                .fetch_add(BLOCK_FRAMES as u64, Relaxed);
            CAPTURE_QUEUE_OVERFLOW
        })
    }
}

pub struct CaptureStream {
    _stream: cpal::Stream,
    queue: Arc<ArrayQueue<AudioBlock>>,
    counters: Arc<CaptureCounters>,
}

impl CaptureStream {
    pub fn start(device_id: &str, show_delay_seconds: f64) -> Result<Self> {
        let mut assembler = CaptureAssembler::new(AudioDelay::new(show_delay_seconds)?);
        let device = select_device(device_id, true)?;
        let (queue, counters) = (assembler.queue.clone(), assembler.counters.clone());
        let errors = counters.clone();
        let mut promoted = false;
        let stream = device
            .build_input_stream::<f32, _, _>(
                stream_config(true),
                move |input, info| {
                    // cpal owns the callback thread, so the first callback is the first chance.
                    if !std::mem::replace(&mut promoted, true) {
                        assembler
                            .counters
                            .record_scheduling(realtime::promote_current_thread());
                    }
                    let timestamp = info.timestamp();
                    assembler.capture(
                        input,
                        CaptureTiming {
                            captured_at_nanos: timestamp.capture.as_nanos(),
                            callback_nanos: timestamp.callback.as_nanos(),
                            callback_at: Instant::now(),
                        },
                    );
                },
                move |error| errors.stream.error(error),
                Some(STREAM_OPEN_TIMEOUT),
            )
            .context("open capture: stereo Float32 44100 Hz")?;
        stream.play().context("start capture")?;
        Ok(Self {
            _stream: stream,
            queue,
            counters,
        })
    }

    pub fn pop(&self) -> Option<AudioBlock> {
        self.queue.pop()
    }

    pub fn discard_delayed_audio(&self) {
        self.counters.discard_requested.store(true, Release);
    }

    pub fn last_error(&self) -> Option<&'static str> {
        self.counters.stream.error_message()
    }

    pub fn metrics(&self) -> CaptureMetrics {
        self.counters.metrics(self.queue.len() * BLOCK_FRAMES)
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
        self.sequence.fetch_add(1, SeqCst);
        if let Some(position) = audio_frame_position {
            self.audio_frame_position.store(position, SeqCst);
        }
        self.position_valid
            .store(audio_frame_position.is_some(), SeqCst);
        self.device_latency_frames
            .store(device_latency_frames, SeqCst);
        self.sequence.fetch_add(1, SeqCst);
    }

    pub fn snapshot(&self) -> AudibleSnapshot {
        // A stalled writer must never hold the DMX worker in a spin loop.
        for _ in 0..3 {
            let before = self.sequence.load(SeqCst);
            if before & 1 != 0 {
                continue;
            }
            let snapshot = AudibleSnapshot {
                audio_frame_position: self.audio_frame_position.load(SeqCst),
                position_valid: self.position_valid.load(SeqCst)
                    && !self.counters.faulted.load(Acquire),
                callback_age: self.counters.callback_age(),
                device_latency_frames: self.device_latency_frames.load(SeqCst),
            };
            if before == self.sequence.load(SeqCst) {
                return snapshot;
            }
        }
        AudibleSnapshot {
            audio_frame_position: self.audio_frame_position.load(Acquire),
            position_valid: false,
            callback_age: self.counters.callback_age(),
            device_latency_frames: self.device_latency_frames.load(Acquire),
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
    pub priority_denied: u64,
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
        let counters = Arc::new(StreamCounters::default());
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
                stream_config(false),
                move |output, info| {
                    callback_counters.callback();
                    if callback_counters.faulted.load(Acquire) {
                        playout.counters.faulted.store(true, Release);
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
                        if playout.counters.faulted.load(Acquire) {
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
        if self.counters.faulted.load(Acquire) {
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
            callbacks: self.counters.callbacks.load(Acquire),
            stream_errors: self.counters.stream_errors.load(Relaxed),
            device_xruns: self.counters.device_xruns.load(Relaxed),
            queue_overflow_frames: playout.queue_overflow_frames.load(Relaxed),
            underruns: playout.underruns.load(Relaxed),
            rebuffer_count: playout.rebuffer_count.load(Relaxed),
            buffered_frames: playout.buffered_frames.load(Relaxed),
            drift_ppm: playout.drift_ppm.load(Relaxed),
            lighting_overflows: playout.lighting_overflows.load(Relaxed),
            priority_denied: self.counters.priority_denied.load(Relaxed),
            last_callback_age: self.counters.callback_age(),
            faulted: self.counters.faulted.load(Acquire) || playout.faulted.load(Acquire),
        }
    }
}

#[cfg(test)]
#[path = "../tests/rust/audio.rs"]
mod tests;
