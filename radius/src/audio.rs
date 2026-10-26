//! Explicit-device WASAPI audio. Callbacks own their mutable DSP state and use fixed queues.
//! CPAL and Windows schedule the callback threads; worker CPU affinity does not control them.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU32, AtomicU64, Ordering};
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
/// Shared-mode WASAPI capture ring. Callbacks still fire every device period, so this adds
/// no latency; it lets a capture thread stalled by CPU load catch up without losing audio.
/// The default minimal ring overruns after one missed period (about 10 ms).
const CAPTURE_DEVICE_BUFFER_FRAMES: u32 = SAMPLE_RATE / 5;
/// This many capture discontinuities inside the window mean the input cannot keep up.
pub const CAPTURE_DISCONTINUITY_LIMIT: usize = 3;
pub const CAPTURE_DISCONTINUITY_WINDOW: Duration = Duration::from_secs(10);
const NANOS_PER_SECOND: f64 = 1e9;
/// Unflagged timestamp deviations below this are scheduling jitter, not lost audio.
const CAPTURE_TIMESTAMP_DEAD_BAND_FRAMES: u64 = SAMPLE_RATE as u64 / 200;

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

fn capture_stream_config() -> cpal::StreamConfig {
    cpal::StreamConfig {
        buffer_size: cpal::BufferSize::Fixed(CAPTURE_DEVICE_BUFFER_FRAMES),
        ..stream_config()
    }
}

/// Resolves the exact endpoint and verifies a stereo Float32 44.1 kHz stream can be opened.
/// No stream is started and no audio is captured or played.
pub fn check_device(device_id: &str, input: bool) -> Result<AudioDevice> {
    let device = select_device(device_id, input)?;
    let description = device.description()?;
    let _stream = if input {
        device.build_input_stream::<f32, _, _>(
            capture_stream_config(),
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

struct StreamCounters {
    epoch: Instant,
    callbacks: AtomicU64,
    last_callback_micros: AtomicU64,
    device_xruns: AtomicU64,
    stream_errors: AtomicU64,
    /// Elevated audio-thread scheduling was refused; audio continues at normal priority.
    realtime_denied: AtomicU64,
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
            realtime_denied: AtomicU64::new(0),
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

    /// Playback errors and non-xrun capture errors. Capture xruns are bridged by
    /// `CaptureAssembler`; output xruns invalidate audible timing.
    fn error(&self, error: cpal::Error) {
        let code = match error.kind() {
            cpal::ErrorKind::RealtimeDenied => {
                self.realtime_denied.fetch_add(1, Ordering::Relaxed);
                return;
            }
            cpal::ErrorKind::Xrun => {
                self.device_xruns.fetch_add(1, Ordering::Relaxed);
                PLAYBACK_XRUN
            }
            kind => {
                self.stream_errors.fetch_add(1, Ordering::Relaxed);
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
        self.last_error_code.store(code, Ordering::Relaxed);
        self.faulted.store(true, Ordering::Release);
    }

    fn error_message(&self) -> Option<&'static str> {
        match self.last_error_code.load(Ordering::Relaxed) {
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

#[derive(Debug, Clone)]
pub struct CaptureMetrics {
    pub callbacks: u64,
    /// Frames delivered by the device, including any later dropped as repeats.
    pub device_frames: u64,
    pub queue_overflow_frames: u64,
    pub device_xruns: u64,
    pub discontinuities: u64,
    /// Device frames lost (positive, filled with silence) or repeated (negative, dropped)
    /// at the latest discontinuity. None until one occurs.
    pub last_gap_frames: Option<i64>,
    pub resync_silence_frames: u64,
    pub resync_dropped_frames: u64,
    pub stream_errors: u64,
    pub realtime_denied: u64,
    pub last_callback_age: Option<Duration>,
    pub queued_frames: usize,
    pub faulted: bool,
}

/// Written by the capture error and data callbacks, which WASAPI runs on one thread.
#[derive(Default)]
struct CaptureDiscontinuities {
    /// The endpoint flagged the next packet as discontinuous with the previous one.
    pending: AtomicBool,
    total: AtomicU64,
    last_gap_frames: AtomicI64,
    silence_frames: AtomicU64,
    dropped_frames: AtomicU64,
}

impl CaptureDiscontinuities {
    fn last_gap_frames(&self) -> Option<i64> {
        (self.total.load(Ordering::Acquire) != 0)
            .then(|| self.last_gap_frames.load(Ordering::Relaxed))
    }
}

/// Device timing of one capture callback. Device timestamps share the QPC epoch, so one
/// (device time, `Instant`) pair per callback maps every frame onto the monotonic clock.
#[derive(Clone, Copy)]
struct CaptureTiming {
    /// Device timestamp of the first frame in the packet; zero when the device gave none.
    captured_at_nanos: u128,
    callback_nanos: u128,
    callback_at: Instant,
}

impl CaptureTiming {
    fn instant(&self, device_nanos: u128) -> Instant {
        let offset = |nanos: u128| Duration::from_nanos(u64::try_from(nanos).unwrap_or(u64::MAX));
        if device_nanos <= self.callback_nanos {
            self.callback_at
                .checked_sub(offset(self.callback_nanos - device_nanos))
                .unwrap_or(self.callback_at)
        } else {
            self.callback_at
                .checked_add(offset(device_nanos - self.callback_nanos))
                .unwrap_or(self.callback_at)
        }
    }
}

/// Keeps the transmitted sample count equal to elapsed device time across capture overruns,
/// so the show delay and every downstream frame position stay exact.
struct CaptureClock {
    /// Expected device timestamp of the next captured frame.
    next_frame_nanos: Option<u128>,
    /// Device timestamps of the latest discontinuities, oldest overwritten first.
    recent_discontinuity_nanos: [Option<u128>; CAPTURE_DISCONTINUITY_LIMIT - 1],
    next_recent_slot: usize,
    /// Stale frames still to drop after the device repeated audio.
    pending_drop_frames: u64,
}

impl CaptureClock {
    fn new() -> Self {
        Self {
            next_frame_nanos: None,
            recent_discontinuity_nanos: [None; CAPTURE_DISCONTINUITY_LIMIT - 1],
            next_recent_slot: 0,
            pending_drop_frames: 0,
        }
    }

    fn frame_nanos(frames: u64) -> u128 {
        (frames as f64 * NANOS_PER_SECOND / f64::from(SAMPLE_RATE)).round() as u128
    }

    /// Device frames between the expected and the actual first frame of this packet.
    fn gap_frames(&self, captured_at_nanos: u128) -> Option<i64> {
        let expected = self.next_frame_nanos?;
        let signed_nanos = captured_at_nanos as f64 - expected as f64;
        Some((signed_nanos * f64::from(SAMPLE_RATE) / NANOS_PER_SECOND).round() as i64)
    }

    /// Records one discontinuity and reports whether the window limit is now reached.
    fn repeats_too_often(&mut self, captured_at_nanos: u128) -> bool {
        let window = CAPTURE_DISCONTINUITY_WINDOW.as_nanos();
        let recent = self
            .recent_discontinuity_nanos
            .iter()
            .flatten()
            .filter(|&&at| captured_at_nanos.saturating_sub(at) < window)
            .count();
        self.recent_discontinuity_nanos[self.next_recent_slot] = Some(captured_at_nanos);
        self.next_recent_slot = (self.next_recent_slot + 1) % self.recent_discontinuity_nanos.len();
        recent + 1 >= CAPTURE_DISCONTINUITY_LIMIT
    }
}

struct CaptureAssembler {
    delay: AudioDelay,
    partial: AudioBlock,
    clock: CaptureClock,
    queue: Arc<ArrayQueue<AudioBlock>>,
    device_frames: Arc<AtomicU64>,
    queue_overflow_frames: Arc<AtomicU64>,
    discontinuities: Arc<CaptureDiscontinuities>,
    counters: Arc<StreamCounters>,
}

impl CaptureAssembler {
    fn capture(&mut self, input: &[f32], timing: CaptureTiming) {
        self.counters.callback();
        if self.counters.faulted.load(Ordering::Acquire) {
            return;
        }
        if !input.len().is_multiple_of(CHANNELS) {
            self.counters.stream_errors.fetch_add(1, Ordering::Relaxed);
            self.counters.fault(PARTIAL_FRAME);
            return;
        }
        let input = input.as_chunks::<CHANNELS>().0;
        self.device_frames
            .fetch_add(input.len() as u64, Ordering::Relaxed);
        let flagged = self.discontinuities.pending.swap(false, Ordering::AcqRel);
        let Ok(silence_frames) = self.resynchronize(flagged, timing, input.len()) else {
            return;
        };
        let captured_at = timing.captured_at_nanos;
        if captured_at != 0 {
            self.clock.next_frame_nanos =
                Some(captured_at + CaptureClock::frame_nanos(input.len() as u64));
        }

        // Silence covers the device time between the expected and the actual first frame.
        for index in 0..silence_frames {
            let device_nanos =
                captured_at.saturating_sub(CaptureClock::frame_nanos(silence_frames - index));
            if !self.emit([0.0; CHANNELS], timing, device_nanos) {
                return;
            }
        }
        let dropped = self.clock.pending_drop_frames.min(input.len() as u64) as usize;
        self.clock.pending_drop_frames -= dropped as u64;
        for (index, frame) in input.iter().enumerate().skip(dropped) {
            let device_nanos = captured_at + CaptureClock::frame_nanos(index as u64);
            if !self.emit([frame[0], frame[1]], timing, device_nanos) {
                return;
            }
        }
    }

    /// Compares this packet's device timestamp with the expected one. A flagged packet, or
    /// one off by more than the jitter dead band, is a discontinuity: returns the silence
    /// frames that keep the timeline exact, or faults when it cannot be bridged.
    fn resynchronize(
        &mut self,
        flagged: bool,
        timing: CaptureTiming,
        input_frames: usize,
    ) -> Result<u64, ()> {
        let captured_at = timing.captured_at_nanos;
        if captured_at == 0 {
            if flagged && self.clock.next_frame_nanos.is_some() {
                self.counters.fault(CAPTURE_GAP_UNMEASURED);
                return Err(());
            }
            return Ok(0);
        }
        // The first packet has no timeline to break.
        let Some(gap_frames) = self.clock.gap_frames(captured_at) else {
            return Ok(0);
        };
        if !flagged && gap_frames.unsigned_abs() <= CAPTURE_TIMESTAMP_DEAD_BAND_FRAMES {
            return Ok(0);
        }
        self.discontinuities
            .last_gap_frames
            .store(gap_frames, Ordering::Relaxed);
        self.discontinuities.total.fetch_add(1, Ordering::Release);
        if self.clock.repeats_too_often(captured_at) {
            self.counters.fault(CAPTURE_REPEATED_DISCONTINUITY);
            return Err(());
        }
        // Silence enters the delay ring, so the delay never limits a gap; the bounded
        // capture queue must hold the catch-up burst. Repeats use the same ceiling.
        let queue_frames = self.queue.capacity() as u64 * BLOCK_FRAMES as u64;
        if gap_frames < 0 {
            if gap_frames.unsigned_abs() > queue_frames {
                self.counters.fault(CAPTURE_GAP_EXCEEDS_QUEUE);
                return Err(());
            }
            self.clock.pending_drop_frames += gap_frames.unsigned_abs();
            self.discontinuities
                .dropped_frames
                .fetch_add(gap_frames.unsigned_abs(), Ordering::Relaxed);
            return Ok(0);
        }
        let silence_frames = gap_frames as u64;
        let free_frames = (self.queue.capacity() - self.queue.len()) as u64 * BLOCK_FRAMES as u64;
        let needed_frames = u64::from(self.partial.frames) + silence_frames + input_frames as u64;
        if needed_frames > free_frames {
            self.counters.fault(CAPTURE_GAP_EXCEEDS_QUEUE);
            return Err(());
        }
        self.discontinuities
            .silence_frames
            .fetch_add(silence_frames, Ordering::Relaxed);
        Ok(silence_frames)
    }

    /// Delays one frame and queues each completed block, stamped with the monotonic time
    /// its last frame was captured. A catch-up backlog therefore keeps the light state of
    /// its own moment, and stamps stay monotonic across callbacks.
    fn emit(&mut self, frame: [f32; CHANNELS], timing: CaptureTiming, device_nanos: u128) -> bool {
        let frame = self.delay.frame(frame);
        let offset = usize::from(self.partial.frames) * CHANNELS;
        self.partial.samples[offset..offset + CHANNELS].copy_from_slice(&frame);
        self.partial.frames += 1;
        if usize::from(self.partial.frames) < BLOCK_FRAMES {
            return true;
        }
        let next_position = self.partial.audio_frame_position + BLOCK_FRAMES as u64;
        let mut block = std::mem::take(&mut self.partial);
        self.partial.audio_frame_position = next_position;
        block.assembly_completed_at = if timing.captured_at_nanos == 0 {
            timing.callback_at
        } else {
            timing.instant(device_nanos)
        };
        if self.queue.push(block).is_err() {
            self.queue_overflow_frames
                .fetch_add(BLOCK_FRAMES as u64, Ordering::Relaxed);
            self.counters.fault(CAPTURE_QUEUE_OVERFLOW);
            return false;
        }
        true
    }
}

pub struct CaptureStream {
    _stream: cpal::Stream,
    queue: Arc<ArrayQueue<AudioBlock>>,
    counters: Arc<StreamCounters>,
    device_frames: Arc<AtomicU64>,
    queue_overflow_frames: Arc<AtomicU64>,
    discontinuities: Arc<CaptureDiscontinuities>,
}

impl CaptureStream {
    pub fn start(device_id: &str, show_delay_seconds: f64) -> Result<Self> {
        let delay = AudioDelay::new(show_delay_seconds)?;
        let device = select_device(device_id, true)?;
        let counters = Arc::new(StreamCounters::new());
        let queue = Arc::new(ArrayQueue::new(CAPTURE_QUEUE_BLOCKS));
        let device_frames = Arc::new(AtomicU64::new(0));
        let queue_overflow_frames = Arc::new(AtomicU64::new(0));
        let discontinuities = Arc::new(CaptureDiscontinuities::default());
        let mut assembler = CaptureAssembler {
            delay,
            partial: AudioBlock::default(),
            clock: CaptureClock::new(),
            queue: queue.clone(),
            device_frames: device_frames.clone(),
            queue_overflow_frames: queue_overflow_frames.clone(),
            discontinuities: discontinuities.clone(),
            counters: counters.clone(),
        };
        let errors = counters.clone();
        let flagged = discontinuities.clone();
        let stream = device
            .build_input_stream::<f32, _, _>(
                capture_stream_config(),
                move |input, info| {
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
                move |error| {
                    // WASAPI reports a capture xrun just before delivering the packet after it.
                    if error.kind() == cpal::ErrorKind::Xrun {
                        errors.device_xruns.fetch_add(1, Ordering::Relaxed);
                        flagged.pending.store(true, Ordering::Release);
                    } else {
                        errors.error(error);
                    }
                },
                Some(STREAM_OPEN_TIMEOUT),
            )
            .context("open capture: stereo Float32 44100 Hz")?;
        stream.play().context("start capture")?;
        Ok(Self {
            _stream: stream,
            queue,
            counters,
            device_frames,
            queue_overflow_frames,
            discontinuities,
        })
    }

    pub fn pop(&self) -> Option<AudioBlock> {
        self.queue.pop()
    }

    pub fn last_error(&self) -> Option<&'static str> {
        self.counters.error_message()
    }

    pub fn metrics(&self) -> CaptureMetrics {
        CaptureMetrics {
            callbacks: self.counters.callbacks.load(Ordering::Acquire),
            device_frames: self.device_frames.load(Ordering::Relaxed),
            queue_overflow_frames: self.queue_overflow_frames.load(Ordering::Relaxed),
            device_xruns: self.counters.device_xruns.load(Ordering::Relaxed),
            discontinuities: self.discontinuities.total.load(Ordering::Acquire),
            last_gap_frames: self.discontinuities.last_gap_frames(),
            resync_silence_frames: self.discontinuities.silence_frames.load(Ordering::Relaxed),
            resync_dropped_frames: self.discontinuities.dropped_frames.load(Ordering::Relaxed),
            stream_errors: self.counters.stream_errors.load(Ordering::Relaxed),
            realtime_denied: self.counters.realtime_denied.load(Ordering::Relaxed),
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
    pub realtime_denied: u64,
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
            realtime_denied: self.counters.realtime_denied.load(Ordering::Relaxed),
            last_callback_age: self.counters.callback_age(),
            faulted: self.counters.faulted.load(Ordering::Acquire)
                || playout.faulted.load(Ordering::Acquire),
        }
    }
}

#[cfg(test)]
#[path = "../tests/rust/audio.rs"]
mod tests;
