use super::*;

#[test]
fn delay_is_frame_exact_across_callback_boundaries_and_wraps() {
    for seconds in [0.0, 0.25, 14.0] {
        let mut delay = AudioDelay::new(seconds).unwrap();
        let lag = (seconds * f64::from(SAMPLE_RATE)).round() as usize;
        let mut position = 0;
        for frames in [1, 17, 128, 5000, 700_000, 2, 300_000] {
            for _ in 0..frames {
                let input = [position as f32 + 1.0, -(position as f32 + 1.0)];
                let expected = if position < lag {
                    [0.0; CHANNELS]
                } else {
                    [
                        (position - lag) as f32 + 1.0,
                        -((position - lag) as f32 + 1.0),
                    ]
                };
                assert_eq!(delay.frame(input), expected);
                position += 1;
            }
        }
    }
}

#[test]
fn delay_rejects_invalid_sizes_before_allocating() {
    for seconds in [-1.0, 121.0, f64::NAN, f64::INFINITY] {
        assert!(AudioDelay::new(seconds).is_err());
    }
}

fn assembler(capacity: usize) -> CaptureAssembler {
    CaptureAssembler {
        delay: AudioDelay::new(0.0).unwrap(),
        partial: AudioBlock::default(),
        clock: CaptureClock::new(),
        queue: Arc::new(ArrayQueue::new(capacity)),
        captured_frames: Arc::new(AtomicU64::new(0)),
        queue_overflow_frames: Arc::new(AtomicU64::new(0)),
        discontinuities: Arc::new(CaptureDiscontinuities::default()),
        counters: Arc::new(StreamCounters::new()),
    }
}

const PACKET_FRAMES: u64 = 441;

/// Simulates a device that stamps each packet with the capture time of its first frame.
/// Sample values carry the device frame index (plus one, so silence stays distinguishable).
struct Device {
    next_frame: u64,
}

impl Device {
    fn packet(&mut self, capture: &mut CaptureAssembler, frames: u64) {
        let input: Vec<f32> = (self.next_frame..self.next_frame + frames)
            .flat_map(|frame| [(frame + 1) as f32, -((frame + 1) as f32)])
            .collect();
        capture.capture(&input, CaptureClock::frame_nanos(self.next_frame));
        self.next_frame += frames;
    }

    /// The endpoint drops (positive) or repeats (negative) frames and flags the next packet.
    fn discontinuity(&mut self, capture: &CaptureAssembler, gap_frames: i64) {
        self.next_frame = self.next_frame.checked_add_signed(gap_frames).unwrap();
        capture
            .discontinuities
            .pending
            .store(true, Ordering::Release);
    }
}

fn delayed_assembler(delay_seconds: f64, capacity: usize) -> CaptureAssembler {
    let mut capture = assembler(capacity);
    capture.delay = AudioDelay::new(delay_seconds).unwrap();
    capture
}

/// Drains the queue, checking the transmitted timeline stays consecutive, and returns
/// (transmitted position, device frame index) for every non-silent frame.
fn drain(capture: &CaptureAssembler, next_position: &mut u64) -> Vec<(u64, u64)> {
    let mut audible = Vec::new();
    while let Some(block) = capture.queue.pop() {
        assert_eq!(block.audio_frame_position, *next_position);
        for frame in 0..usize::from(block.frames) {
            let value = block.samples[frame * CHANNELS];
            if value != 0.0 {
                audible.push((*next_position + frame as u64, value as u64 - 1));
            }
        }
        *next_position += u64::from(block.frames);
    }
    audible
}

#[test]
fn capture_overrun_is_bridged_with_silence_and_keeps_the_delay_exact() {
    let mut capture = delayed_assembler(0.25, 1024);
    let delay_frames = capture.delay.delay_frames();
    let mut device = Device { next_frame: 0 };
    let mut position = 0;
    let mut audible = Vec::new();
    for packet in 0..200 {
        if packet == 60 {
            device.discontinuity(&capture, 3_000);
        }
        device.packet(&mut capture, PACKET_FRAMES);
        audible.extend(drain(&capture, &mut position));
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    assert_eq!(capture.discontinuities.total.load(Ordering::Acquire), 1);
    assert_eq!(capture.discontinuities.last_gap_frames(), Some(3_000));
    // Every captured frame leaves exactly one show delay after its device time.
    assert!(audible.len() as u64 > 199 * PACKET_FRAMES - delay_frames);
    for (transmitted, device_frame) in audible {
        assert_eq!(transmitted, device_frame + delay_frames);
    }
}

#[test]
fn timestamp_jitter_at_a_resync_stays_within_one_device_period() {
    let mut capture = delayed_assembler(0.25, 1024);
    let delay_frames = capture.delay.delay_frames() as i64;
    let mut device = Device { next_frame: 0 };
    let mut position = 0;
    let mut audible = Vec::new();
    for packet in 0..100 {
        device.packet(&mut capture, PACKET_FRAMES);
        audible.extend(drain(&capture, &mut position));
        if packet == 30 {
            device.discontinuity(&capture, 2_000);
            // The packet after the gap carries a timestamp 3 ms late.
            let late = CaptureClock::frame_nanos(device.next_frame) + 3_000_000;
            let input: Vec<f32> = (device.next_frame..device.next_frame + PACKET_FRAMES)
                .flat_map(|frame| [(frame + 1) as f32, -((frame + 1) as f32)])
                .collect();
            capture.capture(&input, late);
            device.next_frame += PACKET_FRAMES;
        }
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    for (transmitted, device_frame) in audible {
        let error = transmitted as i64 - (device_frame as i64 + delay_frames);
        assert!(
            error.unsigned_abs() <= PACKET_FRAMES,
            "sync error {error} frames"
        );
    }
}

#[test]
fn repeated_device_audio_is_dropped_to_keep_the_timeline_exact() {
    let mut capture = delayed_assembler(0.25, 1024);
    let delay_frames = capture.delay.delay_frames();
    let mut device = Device { next_frame: 0 };
    let mut position = 0;
    let mut audible = Vec::new();
    for packet in 0..100 {
        if packet == 40 {
            device.discontinuity(&capture, -600);
        }
        device.packet(&mut capture, PACKET_FRAMES);
        audible.extend(drain(&capture, &mut position));
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    assert_eq!(capture.discontinuities.last_gap_frames(), Some(-600));
    let mut previous = None;
    for (transmitted, device_frame) in audible {
        assert_eq!(transmitted, device_frame + delay_frames);
        assert!(previous.is_none_or(|previous| device_frame > previous));
        previous = Some(device_frame);
    }
}

#[test]
fn three_discontinuities_within_ten_seconds_fault_but_spaced_ones_do_not() {
    let window_packets =
        (CAPTURE_DISCONTINUITY_WINDOW.as_secs() * u64::from(SAMPLE_RATE)).div_ceil(PACKET_FRAMES);
    for (spacing_packets, faults) in [(window_packets / 3, true), (window_packets / 2 + 1, false)] {
        let mut capture = delayed_assembler(0.25, 1024);
        let mut device = Device { next_frame: 0 };
        let mut position = 0;
        for packet in 0..spacing_packets * (CAPTURE_DISCONTINUITY_LIMIT as u64 - 1) + 2 {
            if packet % spacing_packets == 1 {
                device.discontinuity(&capture, 500);
            }
            device.packet(&mut capture, PACKET_FRAMES);
            drain(&capture, &mut position);
        }
        assert_eq!(
            capture.counters.faulted.load(Ordering::Acquire),
            faults,
            "spacing {spacing_packets} packets"
        );
        assert_eq!(
            capture.discontinuities.total.load(Ordering::Acquire),
            CAPTURE_DISCONTINUITY_LIMIT as u64
        );
        if faults {
            assert_eq!(
                capture.counters.error_message(),
                Some("capture discontinuities repeated too often")
            );
        }
    }
}

#[test]
fn a_gap_longer_than_the_playback_delay_faults_instead_of_drifting() {
    let mut capture = delayed_assembler(0.25, 1024);
    let delay_frames = capture.delay.delay_frames() as i64;
    let mut device = Device { next_frame: 0 };
    let mut position = 0;
    for _ in 0..10 {
        device.packet(&mut capture, PACKET_FRAMES);
        drain(&capture, &mut position);
    }
    device.discontinuity(&capture, delay_frames + 1);
    device.packet(&mut capture, PACKET_FRAMES);
    assert!(capture.counters.faulted.load(Ordering::Acquire));
    assert_eq!(
        capture.counters.error_message(),
        Some("a capture gap exceeded the playback delay")
    );
    assert_eq!(
        capture.discontinuities.last_gap_frames(),
        Some(delay_frames + 1)
    );
    assert!(
        capture.queue.is_empty(),
        "no audio may follow an unbridgeable gap"
    );
}

#[test]
fn a_gap_the_capture_queue_cannot_hold_faults_before_queueing_partial_audio() {
    let mut capture = delayed_assembler(14.0, 8);
    let mut device = Device { next_frame: 0 };
    device.packet(&mut capture, PACKET_FRAMES);
    device.discontinuity(&capture, 8 * BLOCK_FRAMES as i64);
    device.packet(&mut capture, PACKET_FRAMES);
    assert_eq!(
        capture.counters.error_message(),
        Some("a capture gap exceeded the capture queue headroom")
    );
    assert_eq!(capture.queue.len(), 3);
}

#[test]
fn capture_assembles_fixed_blocks_without_losing_callback_tails() {
    let mut capture = assembler(8);
    let mut position = 0;
    for frames in [1, 7, 333, 43] {
        let input: Vec<f32> = (position..position + frames)
            .flat_map(|frame| [frame as f32, -(frame as f32)])
            .collect();
        capture.capture(&input, CaptureClock::frame_nanos(position as u64));
        position += frames;
    }
    assert_eq!(capture.queue.len(), 3);
    for first in [0, 128, 256] {
        let block = capture.queue.pop().unwrap();
        assert_eq!(block.audio_frame_position, first);
        assert_eq!(usize::from(block.frames), BLOCK_FRAMES);
        for frame in 0..BLOCK_FRAMES {
            assert_eq!(
                &block.samples[frame * CHANNELS..frame * CHANNELS + CHANNELS],
                &[
                    (first as usize + frame) as f32,
                    -((first as usize + frame) as f32)
                ]
            );
        }
    }
}

#[test]
fn long_show_delay_emits_contiguous_silence_blocks_while_filling() {
    for seconds in [14.0, 60.0] {
        let mut capture = assembler(1);
        capture.delay = AudioDelay::new(seconds).unwrap();
        let delay_frames = (seconds * f64::from(SAMPLE_RATE)) as usize;
        for block_index in 0..delay_frames.div_ceil(BLOCK_FRAMES) + 1 {
            capture.capture(
                &[0.5; BLOCK_FRAMES * CHANNELS],
                CaptureClock::frame_nanos((block_index * BLOCK_FRAMES) as u64),
            );
            let block = capture
                .queue
                .pop()
                .expect("delay must emit blocks immediately");
            assert_eq!(
                block.audio_frame_position,
                (block_index * BLOCK_FRAMES) as u64
            );
            for frame in 0..BLOCK_FRAMES {
                let expected = if block_index * BLOCK_FRAMES + frame < delay_frames {
                    0.0
                } else {
                    0.5
                };
                assert_eq!(
                    &block.samples[frame * CHANNELS..(frame + 1) * CHANNELS],
                    &[expected; CHANNELS]
                );
            }
        }
        assert!(!capture.counters.faulted.load(Ordering::Acquire));
        assert_eq!(capture.queue_overflow_frames.load(Ordering::Relaxed), 0);
    }
}

#[test]
fn capture_overflow_latches_failure_instead_of_resuming_with_a_gap() {
    let mut capture = assembler(1);
    capture.capture(&[0.5; BLOCK_FRAMES * CHANNELS * 2], 0);
    assert_eq!(
        capture.queue_overflow_frames.load(Ordering::Relaxed),
        BLOCK_FRAMES as u64
    );
    assert!(capture.counters.faulted.load(Ordering::Acquire));
    capture.queue.pop();
    capture.capture(&[0.5; BLOCK_FRAMES * CHANNELS], 0);
    assert!(capture.queue.is_empty());
}

#[test]
fn queued_audio_retains_its_completion_time_across_later_callbacks() {
    let mut capture = assembler(2);
    capture.capture(&[0.5; CHANNELS], 0);
    let before_completion = Instant::now();
    capture.capture(
        &[0.5; (BLOCK_FRAMES - 1) * CHANNELS],
        CaptureClock::frame_nanos(1),
    );
    let after_completion = Instant::now();
    capture.capture(
        &[0.5; BLOCK_FRAMES * CHANNELS],
        CaptureClock::frame_nanos(BLOCK_FRAMES as u64),
    );
    let first = capture.queue.pop().unwrap();
    assert!(first.assembly_completed_at >= before_completion);
    assert!(first.assembly_completed_at <= after_completion);
    let second = capture.queue.pop().unwrap();
    assert!(second.assembly_completed_at >= after_completion);
    assert_eq!(first.audio_frame_position, 0);
    assert_eq!(second.audio_frame_position, BLOCK_FRAMES as u64);
}

#[test]
fn audible_clock_uses_callback_start_and_device_latency() {
    let mut history = OutputHistory::new();
    for _ in 0..100 {
        history.push(None);
    }
    for position in 0..100 {
        history.push(Some(position));
    }
    assert_eq!(history.audible_position(100, 20), None);
    assert_eq!(history.audible_position(120, 20), Some(0));
    assert_eq!(history.audible_position(150, 20), Some(30));
    assert_eq!(history.audible_position(150, 0), Some(50));
    assert_eq!(history.audible_position(10, 20), None);
    // The arbitrary source starting position must never be reduced by startup silence.
    history.push(Some(1_000_000));
    assert_eq!(history.audible_position(200, 0), Some(1_000_000));
}

#[test]
fn audible_history_never_reuses_overwritten_positions() {
    let mut history = OutputHistory::new();
    for position in 0..OUTPUT_HISTORY_FRAMES as u64 + 10 {
        history.push(Some(position));
    }
    assert_eq!(
        history.audible_position(history.output_frames, OUTPUT_HISTORY_FRAMES as u64 + 1),
        None
    );
    assert_eq!(
        history.audible_position(history.output_frames, 1),
        Some(history.output_frames - 1)
    );
}

#[test]
fn clock_distinguishes_startup_silence_from_valid_source_position_zero() {
    let clock = AudibleClock {
        sequence: AtomicU64::new(0),
        audio_frame_position: AtomicU64::new(0),
        position_valid: AtomicBool::new(false),
        device_latency_frames: AtomicU64::new(0),
        counters: Arc::new(StreamCounters::new()),
    };
    let before = clock.snapshot();
    assert!(!before.position_valid);
    assert_eq!(before.callback_age, None);
    clock.counters.callback();
    clock.publish(Some(0), 441);
    assert!(clock.snapshot().position_valid);
    assert_eq!(clock.snapshot().audio_frame_position, 0);
    clock.publish(None, 441);
    assert!(!clock.snapshot().position_valid);
}
