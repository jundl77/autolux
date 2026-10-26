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
        queue: Arc::new(ArrayQueue::new(capacity)),
        captured_frames: Arc::new(AtomicU64::new(0)),
        queue_overflow_frames: Arc::new(AtomicU64::new(0)),
        counters: Arc::new(StreamCounters::new()),
        discard_requested: Arc::new(AtomicBool::new(false)),
    }
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
        capture.capture(&[input; BLOCK_FRAMES * CHANNELS]);
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
        capture.capture(&input);
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
            capture.capture(&[0.5; BLOCK_FRAMES * CHANNELS]);
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
    capture.capture(&[0.5; BLOCK_FRAMES * CHANNELS * 2]);
    assert_eq!(
        capture.queue_overflow_frames.load(Ordering::Relaxed),
        BLOCK_FRAMES as u64
    );
    assert!(capture.counters.faulted.load(Ordering::Acquire));
    capture.queue.pop();
    capture.capture(&[0.5; BLOCK_FRAMES * CHANNELS]);
    assert!(capture.queue.is_empty());
}

#[test]
fn queued_audio_retains_its_completion_time_across_later_callbacks() {
    let mut capture = assembler(2);
    capture.capture(&[0.5; CHANNELS]);
    let before_completion = Instant::now();
    capture.capture(&[0.5; (BLOCK_FRAMES - 1) * CHANNELS]);
    let after_completion = Instant::now();
    capture.capture(&[0.5; BLOCK_FRAMES * CHANNELS]);
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
