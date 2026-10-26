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
        device_frames: Arc::new(AtomicU64::new(0)),
        queue_overflow_frames: Arc::new(AtomicU64::new(0)),
        discontinuities: Arc::new(CaptureDiscontinuities::default()),
        counters: Arc::new(StreamCounters::new()),
        discard_requested: Arc::new(AtomicBool::new(false)),
    }
}

const PACKET_FRAMES: u64 = 441;
/// Device timestamps are nonzero in practice; zero means the device supplied none.
const DEVICE_EPOCH_NANOS: u128 = 1_000_000_000;

impl CaptureAssembler {
    /// Delivers `input` as if the callback ran the moment its last frame was captured.
    fn capture_at(&mut self, input: &[f32], first_frame_nanos: u128) {
        let frames = (input.len() / CHANNELS) as u64;
        let captured_at_nanos = DEVICE_EPOCH_NANOS + first_frame_nanos;
        self.capture(
            input,
            CaptureTiming {
                captured_at_nanos,
                callback_nanos: captured_at_nanos
                    + CaptureClock::frame_nanos(frames.saturating_sub(1)),
                callback_at: Instant::now(),
            },
        );
    }
}

fn stereo_ramp(first_frame: u64, frames: u64) -> Vec<f32> {
    (first_frame..first_frame + frames)
        .flat_map(|frame| [(frame + 1) as f32, -((frame + 1) as f32)])
        .collect()
}

/// Simulates a device that stamps each packet with the capture time of its first frame.
/// Sample values carry the device frame index (plus one, so silence stays distinguishable).
struct Device {
    next_frame: u64,
}

impl Device {
    fn packet(&mut self, capture: &mut CaptureAssembler, frames: u64) {
        let input = stereo_ramp(self.next_frame, frames);
        capture.capture_at(&input, CaptureClock::frame_nanos(self.next_frame));
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
    let delay_frames = capture.delay.samples.len() as u64;
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
    let delay_frames = capture.delay.samples.len() as i64;
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
            capture.capture_at(&stereo_ramp(device.next_frame, PACKET_FRAMES), late);
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
    let delay_frames = capture.delay.samples.len() as u64;
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
fn a_gap_longer_than_a_short_playback_delay_is_still_bridged_exactly() {
    // Silence enters the delay ring, so only the capture queue bounds a bridgeable gap.
    let mut capture = delayed_assembler(0.01, 1024);
    let delay_frames = capture.delay.samples.len() as u64;
    let mut device = Device { next_frame: 0 };
    let mut position = 0;
    let mut audible = Vec::new();
    for packet in 0..40 {
        if packet == 10 {
            device.discontinuity(&capture, 10 * delay_frames as i64);
        }
        device.packet(&mut capture, PACKET_FRAMES);
        audible.extend(drain(&capture, &mut position));
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    for (transmitted, device_frame) in audible {
        assert_eq!(transmitted, device_frame + delay_frames);
    }
}

#[test]
fn repeated_audio_beyond_the_capture_queue_faults() {
    let mut capture = delayed_assembler(14.0, 8);
    let mut device = Device {
        next_frame: 100_000,
    };
    device.packet(&mut capture, PACKET_FRAMES);
    capture.queue.pop();
    device.discontinuity(&capture, -(8 * BLOCK_FRAMES as i64 + 1));
    device.packet(&mut capture, PACKET_FRAMES);
    assert_eq!(
        capture.counters.error_message(),
        Some("a capture gap exceeded the capture queue headroom")
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
fn discarding_silences_the_delayed_tail_and_rebuilds_the_delay_from_new_audio() {
    let seconds = 0.25;
    let lag = (seconds * f64::from(SAMPLE_RATE)).round() as usize;
    let mut delay = AudioDelay::new(seconds).unwrap();
    for _ in 0..lag * 3 / 2 {
        delay.frame([0.5, -0.5]);
    }
    delay.discard_buffered();
    let mut previous_gain = 1.0;
    for frame in 0..lag {
        let input = [frame as f32 + 1.0, -(frame as f32 + 1.0)];
        let output = delay.frame(input);
        let gain = output[0] / 0.5;
        assert!(
            gain <= previous_gain && gain >= 0.0,
            "frame {frame} must fade"
        );
        assert_eq!(output[1], -output[0]);
        if frame >= DISCARD_FADE_FRAMES - 1 {
            assert_eq!(output, [0.0; CHANNELS], "frame {frame} must be silent");
        }
        previous_gain = gain;
    }
    // Audio captured after the discard plays exactly one delay later.
    for frame in 0..lag {
        let expected = [frame as f32 + 1.0, -(frame as f32 + 1.0)];
        assert_eq!(delay.frame([0.0; CHANNELS]), expected);
    }
}

#[test]
fn discard_request_keeps_receiver_playout_contiguous_and_silent_without_rebuffering() {
    // One block captured, sent and played per step keeps RX at its steady fill.
    fn step(
        capture: &mut CaptureAssembler,
        sender: &mut PlayoutSender,
        playout: &mut Playout,
        input: f32,
    ) -> Vec<f32> {
        let first_frame = capture.device_frames.load(Ordering::Relaxed);
        capture.capture_at(
            &[input; BLOCK_FRAMES * CHANNELS],
            CaptureClock::frame_nanos(first_frame),
        );
        while let Some(audio) = capture.queue.pop() {
            sender
                .enqueue(PlaybackBlock {
                    audio,
                    light_sequence_number: 1,
                    channels: [0; 512],
                })
                .unwrap();
        }
        playout.begin_callback();
        let mut output = [0.0; CHANNELS];
        let played = (0..BLOCK_FRAMES)
            .map(|_| {
                playout.frame(&mut output);
                output[0]
            })
            .collect();
        while sender.lighting.pop().is_some() {}
        played
    }

    let mut capture = assembler(CAPTURE_QUEUE_BLOCKS);
    capture.delay = AudioDelay::new(0.25).unwrap();
    let (mut playout, mut sender) = Playout::new(JITTER_BUFFER_MS).unwrap();
    let blocks_per_second = SAMPLE_RATE as usize / BLOCK_FRAMES;
    let mut played = Vec::new();
    for _ in 0..blocks_per_second {
        played = step(&mut capture, &mut sender, &mut playout, 0.5);
    }
    assert!(played.iter().all(|&sample| sample == 0.5));
    assert!(playout.is_primed());

    // New audio (0.25) starts with the discard; the old tail (0.5) must not play.
    capture.discard_requested.store(true, Ordering::Release);
    let mut after_discard = Vec::new();
    for _ in 0..blocks_per_second {
        after_discard.extend(step(&mut capture, &mut sender, &mut playout, 0.25));
        assert!(playout.is_primed(), "RX must not rebuffer");
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    assert_eq!(sender.counters.underruns.load(Ordering::Relaxed), 0);
    assert_eq!(sender.counters.rebuffer_count.load(Ordering::Relaxed), 0);

    let silence_starts = after_discard
        .iter()
        .position(|&sample| sample == 0.0)
        .expect("the discarded tail must reach RX as silence");
    let resumes = silence_starts
        + after_discard[silence_starts..]
            .iter()
            .position(|&sample| sample > 0.0)
            .expect("new audio must emerge one delay later");
    let delay_frames = (0.25 * f64::from(SAMPLE_RATE)).round() as usize;
    // RX clock correction interpolates, so each edge may blur by one frame.
    let silent_frames = resumes - silence_starts;
    assert!(silent_frames.abs_diff(delay_frames - DISCARD_FADE_FRAMES + 1) <= 2);
    assert!(after_discard[resumes + 1..].iter().all(|&s| s == 0.25));
}

#[test]
fn capture_assembles_fixed_blocks_without_losing_callback_tails() {
    let mut capture = assembler(8);
    let mut position = 0;
    for frames in [1, 7, 333, 43] {
        let input: Vec<f32> = (position..position + frames)
            .flat_map(|frame| [frame as f32, -(frame as f32)])
            .collect();
        capture.capture_at(&input, CaptureClock::frame_nanos(position as u64));
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
            capture.capture_at(
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
    capture.capture_at(&[0.5; BLOCK_FRAMES * CHANNELS * 2], 0);
    assert_eq!(
        capture.queue_overflow_frames.load(Ordering::Relaxed),
        BLOCK_FRAMES as u64
    );
    assert!(capture.counters.faulted.load(Ordering::Acquire));
    assert_eq!(
        capture.counters.error_message(),
        Some("capture queue overflowed; the consumer stopped keeping up")
    );
    capture.queue.pop();
    capture.capture_at(&[0.5; BLOCK_FRAMES * CHANNELS], 0);
    assert!(capture.queue.is_empty());
}

#[test]
fn a_bridged_resync_does_not_mislabel_a_later_fault() {
    let mut capture = delayed_assembler(0.25, 1024);
    let mut device = Device { next_frame: 0 };
    device.packet(&mut capture, PACKET_FRAMES);
    capture
        .counters
        .device_xruns
        .fetch_add(1, Ordering::Relaxed);
    device.discontinuity(&capture, 100);
    device.packet(&mut capture, PACKET_FRAMES);
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    capture.capture_at(&[0.5; 3], 0);
    assert_eq!(
        capture.counters.error_message(),
        Some("audio callback delivered a partial stereo frame")
    );
}

#[test]
fn realtime_denial_is_counted_without_stopping_audio() {
    let counters = StreamCounters::new();
    counters.error(cpal::ErrorKind::RealtimeDenied.into());
    assert!(!counters.faulted.load(Ordering::Acquire));
    assert_eq!(counters.realtime_denied.load(Ordering::Relaxed), 1);
    assert_eq!(counters.error_message(), None);
    counters.error(cpal::ErrorKind::Xrun.into());
    assert_eq!(
        counters.error_message(),
        Some("audio output underrun invalidated audible timing")
    );
}

#[test]
fn an_unflagged_timestamp_jump_is_bridged_but_jitter_is_ignored() {
    let mut capture = delayed_assembler(0.25, 1024);
    let delay_frames = capture.delay.samples.len() as u64;
    let mut device = Device { next_frame: 0 };
    let mut position = 0;
    let mut audible = Vec::new();
    for packet in 0..100 {
        match packet {
            // Within the dead band: scheduling jitter, no resync and no silence.
            20 => device.next_frame += CAPTURE_TIMESTAMP_DEAD_BAND_FRAMES - 1,
            // The xrun flag was lost, but the timestamp shows 1000 missing frames.
            50 => device.next_frame += 1_000,
            _ => {}
        }
        device.packet(&mut capture, PACKET_FRAMES);
        audible.extend(drain(&capture, &mut position));
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    assert_eq!(capture.discontinuities.total.load(Ordering::Acquire), 1);
    assert_eq!(capture.discontinuities.last_gap_frames(), Some(1_000));
    assert_eq!(
        capture
            .discontinuities
            .silence_frames
            .load(Ordering::Relaxed),
        1_000
    );
    let jitter = CAPTURE_TIMESTAMP_DEAD_BAND_FRAMES - 1;
    for (transmitted, device_frame) in audible {
        let error = (device_frame + delay_frames).abs_diff(transmitted);
        assert!(error <= jitter, "sync error {error} frames");
    }
}

#[test]
fn a_stall_backlog_is_stamped_at_capture_time_and_monotonically() {
    let mut capture = assembler(1024);
    let started = Instant::now();
    let mut device_nanos = DEVICE_EPOCH_NANOS;
    let mut callback_nanos = DEVICE_EPOCH_NANOS;
    let mut stamps = Vec::new();
    for packet in 0..40_u64 {
        if packet == 20 {
            // The endpoint dropped 3000 frames while the capture thread was stalled.
            device_nanos += CaptureClock::frame_nanos(3_000);
            capture
                .discontinuities
                .pending
                .store(true, Ordering::Release);
        }
        let packet_end = device_nanos + CaptureClock::frame_nanos(PACKET_FRAMES);
        // Packets 10..30 are delivered late, as a backlog drained by one late wakeup.
        callback_nanos = callback_nanos.max(if (10..30).contains(&packet) {
            DEVICE_EPOCH_NANOS + CaptureClock::frame_nanos(30 * PACKET_FRAMES + 3_000)
        } else {
            packet_end
        });
        let timing = CaptureTiming {
            captured_at_nanos: device_nanos,
            callback_nanos,
            callback_at: started
                + Duration::from_nanos((callback_nanos - DEVICE_EPOCH_NANOS) as u64),
        };
        capture.capture(&[0.5; PACKET_FRAMES as usize * CHANNELS], timing);
        while let Some(block) = capture.queue.pop() {
            stamps.push(block);
        }
        device_nanos = packet_end;
    }
    assert!(!capture.counters.faulted.load(Ordering::Acquire));
    let mut previous = None;
    for block in stamps {
        // Every block is stamped when its last frame was captured: real frames follow the
        // device clock, inserted silence fills the gap at its own device time.
        let last_frame = block.audio_frame_position + BLOCK_FRAMES as u64 - 1;
        let expected = started + Duration::from_nanos(CaptureClock::frame_nanos(last_frame) as u64);
        let error =
            block.assembly_completed_at.max(expected) - block.assembly_completed_at.min(expected);
        assert!(
            error <= Duration::from_micros(1),
            "block {last_frame} stamped {error:?} off"
        );
        assert!(previous.is_none_or(|previous| block.assembly_completed_at >= previous));
        previous = Some(block.assembly_completed_at);
    }
}

#[test]
fn queued_audio_retains_its_completion_time_across_later_callbacks() {
    let mut capture = assembler(2);
    capture.capture_at(&[0.5; CHANNELS], 0);
    let before_completion = Instant::now();
    capture.capture_at(
        &[0.5; (BLOCK_FRAMES - 1) * CHANNELS],
        CaptureClock::frame_nanos(1),
    );
    let after_completion = Instant::now();
    capture.capture_at(
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
