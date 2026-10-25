//! A bounded network-to-audio handoff. Only admitted audio/light pairs enter this queue.

use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};

use crossbeam_queue::ArrayQueue;

use crate::audio::{AudioBlock, BLOCK_FRAMES, CHANNELS, SAMPLE_RATE};

pub const JITTER_BUFFER_MS: u32 = 500;
const PLAYBACK_QUEUE_BLOCKS: usize = 512;
const LIGHTING_QUEUE_UPDATES: usize = 512;
const CLOCK_CORRECTION_GAIN: f64 = 0.01;
const MAX_CLOCK_CORRECTION_RATIO: f64 = 0.001;
const CLOCK_CORRECTION_SMOOTHING: f64 = 0.02;

#[derive(Clone, Debug)]
pub struct PlaybackBlock {
    pub audio: AudioBlock,
    pub light_sequence_number: u32,
    pub channels: [u8; 512],
}

#[cfg(test)]
#[path = "../tests/rust/playout.rs"]
mod tests;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LightingUpdate {
    pub audio_frame_position: u64,
    pub sequence_number: u32,
    pub channels: [u8; 512],
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnqueueError {
    InvalidBlock,
    OutOfOrder,
    Gap,
    Full,
    Faulted,
}

impl fmt::Display for EnqueueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidBlock => {
                "audio block has invalid geometry, sample position or non-finite samples"
            }
            Self::OutOfOrder => "audio sample position overlaps an already admitted block",
            Self::Gap => "audio sample position skips frames in the reliable transport",
            Self::Full => "playback queue is full; audio and lighting were not admitted",
            Self::Faulted => "audio output has failed; restart the session",
        })
    }
}

impl std::error::Error for EnqueueError {}

#[derive(Default)]
pub(crate) struct PlayoutCounters {
    pub queue_overflow_frames: AtomicU64,
    pub underruns: AtomicU64,
    pub rebuffer_count: AtomicU64,
    pub buffered_frames: AtomicU64,
    pub drift_ppm: AtomicI32,
    pub lighting_overflows: AtomicU64,
    pub faulted: AtomicBool,
}

pub(crate) struct PlayoutSender {
    queue: Arc<ArrayQueue<PlaybackBlock>>,
    latest_sample_position: Arc<AtomicU64>,
    next_expected_sample_position: Option<u64>,
    pub counters: Arc<PlayoutCounters>,
    pub lighting: Arc<ArrayQueue<LightingUpdate>>,
}

impl PlayoutSender {
    pub fn enqueue(&mut self, block: PlaybackBlock) -> Result<(), EnqueueError> {
        if self.counters.faulted.load(Ordering::Acquire) {
            return Err(EnqueueError::Faulted);
        }
        let audio = &block.audio;
        let frames = usize::from(audio.frames);
        let end = audio
            .audio_frame_position
            .checked_add(u64::from(audio.frames));
        if frames == 0
            || frames > BLOCK_FRAMES
            || end.is_none_or(|position| position == u64::MAX)
            || audio.samples[..frames * CHANNELS]
                .iter()
                .any(|sample| !sample.is_finite())
        {
            return Err(EnqueueError::InvalidBlock);
        }
        if let Some(expected) = self.next_expected_sample_position {
            if audio.audio_frame_position < expected {
                return Err(EnqueueError::OutOfOrder);
            }
            if audio.audio_frame_position > expected {
                return Err(EnqueueError::Gap);
            }
        }
        let end = end.expect("validated frame range");
        if self.queue.push(block).is_err() {
            self.counters
                .queue_overflow_frames
                .fetch_add(frames as u64, Ordering::Relaxed);
            return Err(EnqueueError::Full);
        }
        self.next_expected_sample_position = Some(end);
        self.latest_sample_position.store(end, Ordering::Release);
        Ok(())
    }
}

pub(crate) struct Playout {
    queue: Arc<ArrayQueue<PlaybackBlock>>,
    lighting: Arc<ArrayQueue<LightingUpdate>>,
    latest_sample_position: Arc<AtomicU64>,
    pub counters: Arc<PlayoutCounters>,
    target_frames: u64,
    block: Option<PlaybackBlock>,
    block_offset: usize,
    next_sample_position: u64,
    current: [f32; CHANNELS],
    next: [f32; CHANNELS],
    current_position: u64,
    next_position: u64,
    phase: f64,
    ratio: f64,
    primed: bool,
    final_frame_pending: bool,
    has_started: bool,
    last_light_sequence: Option<u32>,
}

impl Playout {
    pub fn new(jitter_buffer_ms: u32) -> anyhow::Result<(Self, PlayoutSender)> {
        anyhow::ensure!(
            jitter_buffer_ms == JITTER_BUFFER_MS,
            "Radius requires a 500 ms playback buffer"
        );
        let queue = Arc::new(ArrayQueue::new(PLAYBACK_QUEUE_BLOCKS));
        let lighting = Arc::new(ArrayQueue::new(LIGHTING_QUEUE_UPDATES));
        let latest_sample_position = Arc::new(AtomicU64::new(0));
        let counters = Arc::new(PlayoutCounters::default());
        let sender = PlayoutSender {
            queue: queue.clone(),
            latest_sample_position: latest_sample_position.clone(),
            next_expected_sample_position: None,
            counters: counters.clone(),
            lighting: lighting.clone(),
        };
        Ok((
            Self {
                queue,
                lighting,
                latest_sample_position,
                counters,
                target_frames: u64::from(SAMPLE_RATE) * u64::from(jitter_buffer_ms) / 1000,
                block: None,
                block_offset: 0,
                next_sample_position: 0,
                current: [0.0; CHANNELS],
                next: [0.0; CHANNELS],
                current_position: 0,
                next_position: 0,
                phase: 0.0,
                ratio: 1.0,
                primed: false,
                final_frame_pending: false,
                has_started: false,
                last_light_sequence: None,
            },
            sender,
        ))
    }

    fn load_block(&mut self) -> bool {
        if self.block.is_none() {
            self.block = self.queue.pop();
            self.block_offset = 0;
            if let Some(block) = &self.block {
                self.next_sample_position = block.audio.audio_frame_position;
            }
        }
        self.block.is_some()
    }

    fn take_frame(&mut self) -> Option<([f32; CHANNELS], u64)> {
        if !self.load_block() {
            return None;
        }
        let block = self.block.as_ref()?;
        if self.last_light_sequence != Some(block.light_sequence_number) {
            let update = LightingUpdate {
                audio_frame_position: block.audio.audio_frame_position + self.block_offset as u64,
                sequence_number: block.light_sequence_number,
                channels: block.channels,
            };
            if self.lighting.push(update).is_err() {
                self.counters
                    .lighting_overflows
                    .fetch_add(1, Ordering::Relaxed);
                self.counters.faulted.store(true, Ordering::Release);
                return None;
            }
            self.last_light_sequence = Some(block.light_sequence_number);
        }
        let index = self.block_offset * CHANNELS;
        let frame = [block.audio.samples[index], block.audio.samples[index + 1]];
        let position = block.audio.audio_frame_position + self.block_offset as u64;
        self.next_sample_position = position + 1;
        self.block_offset += 1;
        if self.block_offset == usize::from(block.audio.frames) {
            self.block = None;
        }
        Some((frame, position))
    }

    pub fn begin_callback(&mut self) {
        if self.counters.faulted.load(Ordering::Acquire) {
            self.primed = false;
            return;
        }
        if !self.primed {
            if !self.load_block() {
                self.counters.buffered_frames.store(0, Ordering::Relaxed);
                return;
            }
            let fill = self
                .latest_sample_position
                .load(Ordering::Acquire)
                .saturating_sub(self.next_sample_position);
            self.counters.buffered_frames.store(fill, Ordering::Relaxed);
            if fill < self.target_frames {
                return;
            }
            self.last_light_sequence = None;
            let Some((current, current_position)) = self.take_frame() else {
                return;
            };
            let Some((next, next_position)) = self.take_frame() else {
                return;
            };
            self.current = current;
            self.current_position = current_position;
            self.next = next;
            self.next_position = next_position;
            self.phase = 0.0;
            self.ratio = 1.0;
            self.primed = true;
            self.final_frame_pending = false;
            if self.has_started {
                self.counters.rebuffer_count.fetch_add(1, Ordering::Relaxed);
            }
            self.has_started = true;
        }
        let fill = self
            .latest_sample_position
            .load(Ordering::Acquire)
            .saturating_sub(self.next_sample_position);
        self.counters.buffered_frames.store(fill, Ordering::Relaxed);
        let correction = ((fill as f64 - self.target_frames as f64) / f64::from(SAMPLE_RATE)
            * CLOCK_CORRECTION_GAIN)
            .clamp(-MAX_CLOCK_CORRECTION_RATIO, MAX_CLOCK_CORRECTION_RATIO);
        self.ratio += CLOCK_CORRECTION_SMOOTHING * (1.0 + correction - self.ratio);
        self.counters
            .drift_ppm
            .store(((self.ratio - 1.0) * 1_000_000.0) as i32, Ordering::Relaxed);
    }

    pub fn is_primed(&self) -> bool {
        self.primed && !self.counters.faulted.load(Ordering::Acquire)
    }

    /// Returns the source frame position represented by this output frame, or startup/rebuffer silence.
    pub fn frame(&mut self, output: &mut [f32]) -> Option<u64> {
        if !self.is_primed() {
            output.fill(0.0);
            return None;
        }
        for (channel, sample) in output.iter_mut().enumerate().take(CHANNELS) {
            *sample = ((1.0 - self.phase) * f64::from(self.current[channel])
                + self.phase * f64::from(self.next[channel])) as f32;
        }
        let position = self.current_position;
        self.phase += self.ratio;
        // The correction bound allows at most two source frames per output frame.
        while self.phase >= 1.0 {
            self.phase -= 1.0;
            if self.final_frame_pending {
                self.primed = false;
                self.final_frame_pending = false;
                break;
            }
            self.current = self.next;
            self.current_position = self.next_position;
            if let Some((next, next_position)) = self.take_frame() {
                self.next = next;
                self.next_position = next_position;
            } else {
                // The last lookahead frame has not reached the output yet. Emit it
                // before switching to rebuffer silence instead of dropping one frame.
                self.next = self.current;
                self.next_position = self.current_position;
                self.final_frame_pending = true;
                self.counters.underruns.fetch_add(1, Ordering::Relaxed);
            }
        }
        Some(position)
    }
}
