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

fn assembler(delay_seconds: f64) -> CaptureAssembler {
    CaptureAssembler::new(AudioDelay::new(delay_seconds).unwrap())
}

const PACKET_FRAMES: u64 = 441;
const DEVICE_EPOCH_NANOS: u128 = 1_000_000_000;

impl CaptureAssembler {
    fn capture_at(&mut self, input: &[f32], first_frame: u64) {
        let captured_at_nanos = DEVICE_EPOCH_NANOS + frame_nanos(first_frame);
        let last_frame = (input.len() / CHANNELS).saturating_sub(1) as u64;
        self.capture(
            input,
            CaptureTiming {
                captured_at_nanos,
                callback_nanos: captured_at_nanos + frame_nanos(last_frame),
                callback_at: Instant::now(),
            },
        );
    }
}

#[derive(Clone, Copy, Debug)]
enum Event {
    Xrun(i64),
    Jump(i64),
    LateTimestamp(u64),
    CallbackStall(u64),
}

struct Device {
    capture: CaptureAssembler,
    started: Instant,
    next_frame: u64,
    callback_nanos: u128,
    position: u64,
    audible: Vec<(u64, u64)>,
    stamp_error: Duration,
    monotonic: bool,
    last_stamp: Option<Instant>,
}

impl Device {
    fn new(delay_seconds: f64) -> Self {
        Self {
            capture: assembler(delay_seconds),
            started: Instant::now(),
            next_frame: 0,
            callback_nanos: 0,
            position: 0,
            audible: Vec::new(),
            stamp_error: Duration::ZERO,
            monotonic: true,
            last_stamp: None,
        }
    }

    fn run(&mut self, packets: std::ops::Range<usize>, events: &[(usize, Event)]) {
        for packet in packets {
            let (mut late, mut stall) = (0, 0);
            for &(_, event) in events.iter().filter(|(at, _)| *at == packet) {
                match event {
                    Event::Xrun(gap) => {
                        self.next_frame = self.next_frame.checked_add_signed(gap).unwrap();
                        self.capture
                            .counters
                            .stream
                            .error(cpal::ErrorKind::Xrun.into());
                    }
                    Event::Jump(gap) => {
                        self.next_frame = self.next_frame.checked_add_signed(gap).unwrap()
                    }
                    Event::LateTimestamp(frames) => late = frames,
                    Event::CallbackStall(frames) => stall = frames,
                }
            }
            let input: Vec<f32> = (self.next_frame..self.next_frame + PACKET_FRAMES)
                .flat_map(|frame| [(frame + 1) as f32, -((frame + 1) as f32)])
                .collect();
            let captured_at_nanos = DEVICE_EPOCH_NANOS + frame_nanos(self.next_frame + late);
            let packet_end = captured_at_nanos + frame_nanos(PACKET_FRAMES - 1);
            self.callback_nanos = self.callback_nanos.max(packet_end + frame_nanos(stall));
            let since_epoch = (self.callback_nanos - DEVICE_EPOCH_NANOS) as u64;
            let timing = CaptureTiming {
                captured_at_nanos,
                callback_nanos: self.callback_nanos,
                callback_at: self.started + Duration::from_nanos(since_epoch),
            };
            self.capture.capture(&input, timing);
            self.next_frame += PACKET_FRAMES;
            self.drain();
        }
    }

    fn drain(&mut self) {
        while let Some(block) = self.capture.queue.pop() {
            assert_eq!(block.audio_frame_position, self.position);
            let last_frame = self.position + BLOCK_FRAMES as u64 - 1;
            let expected = self.started + Duration::from_nanos(frame_nanos(last_frame) as u64);
            let stamp = block.assembly_completed_at;
            self.stamp_error = self
                .stamp_error
                .max(stamp.max(expected) - stamp.min(expected));
            self.monotonic &= self.last_stamp.is_none_or(|last| stamp >= last);
            self.last_stamp = Some(stamp);
            for (frame, &sample) in block.samples.iter().step_by(CHANNELS).enumerate() {
                if sample != 0.0 {
                    self.audible
                        .push((self.position + frame as u64, sample as u64 - 1));
                }
            }
            self.position += BLOCK_FRAMES as u64;
        }
    }

    fn emitted_frames(&self) -> u64 {
        self.position + u64::from(self.capture.partial.frames)
    }
}

type Bridge<'a> = (f64, &'a [(usize, Event)], i64, u64, u64, u64);

#[test]
fn discontinuities_reanchor_the_sample_timeline_to_device_time() {
    use Event::*;
    let deadband = CAPTURE_JITTER_DEAD_BAND_FRAMES;
    let cases: [Bridge<'_>; 6] = [
        (0.25, &[(60, Xrun(3_000))], 3_000, 3_000, 0, 0),
        (0.25, &[(40, Xrun(-600))], -600, 0, 600, 0),
        (0.01, &[(10, Xrun(4_410))], 4_410, 4_410, 0, 0),
        (
            0.25,
            &[(20, Jump(deadband as i64 - 1)), (50, Jump(1_000))],
            1_000,
            1_000,
            0,
            deadband - 1,
        ),
        (
            0.25,
            &[(30, Xrun(2_000)), (30, LateTimestamp(132))],
            2_132,
            2_132,
            0,
            132,
        ),
        (
            0.25,
            &[(10, CallbackStall(20 * PACKET_FRAMES)), (20, Xrun(3_000))],
            3_000,
            3_000,
            0,
            0,
        ),
    ];
    for (delay_seconds, events, gap, silence, dropped, tolerance) in cases {
        let mut device = Device::new(delay_seconds);
        device.run(0..100, events);
        let metrics = device.capture.counters.metrics(0);
        assert!(!metrics.faulted, "{events:?}");
        assert_eq!(
            (
                metrics.discontinuities,
                metrics.last_gap_frames,
                metrics.resync_silence_frames,
                metrics.resync_dropped_frames,
                metrics.device_frames
            ),
            (1, Some(gap), silence, dropped, 100 * PACKET_FRAMES),
            "{events:?}"
        );
        assert!(device.emitted_frames().abs_diff(device.next_frame) <= tolerance);
        let delay_frames = device.capture.delay.samples.len() as u64;
        assert_eq!(
            device.audible.len() as u64,
            device.position - delay_frames - silence
        );
        let mut previous = None;
        for &(transmitted, device_frame) in &device.audible {
            assert!(transmitted.abs_diff(device_frame + delay_frames) <= tolerance);
            assert!(previous.is_none_or(|previous| device_frame > previous));
            previous = Some(device_frame);
        }
        let stamp_tolerance = frame_nanos(tolerance) as u64 + 1_000;
        assert!(device.stamp_error <= Duration::from_nanos(stamp_tolerance));
        assert!(tolerance != 0 || device.monotonic, "{events:?}");
    }
}

#[test]
fn faults_only_on_repeated_discontinuities_or_gaps_beyond_the_queue() {
    use Event::Xrun;
    let window_packets = (CAPTURE_DISCONTINUITY_WINDOW.as_secs() * u64::from(SAMPLE_RATE))
        .div_ceil(PACKET_FRAMES) as usize;
    let repeated = |spacing: usize| {
        (0..CAPTURE_DISCONTINUITY_LIMIT)
            .map(|index| (1 + index * spacing, Xrun(500)))
            .collect::<Vec<_>>()
    };
    let queue_frames = (CAPTURE_QUEUE_BLOCKS * BLOCK_FRAMES) as i64;
    let partial = (400 * PACKET_FRAMES % BLOCK_FRAMES as u64) as i64;
    let headroom = queue_frames - partial - PACKET_FRAMES as i64;
    let too_often = Some("capture discontinuities repeated too often");
    let too_long = Some("a capture gap exceeded the capture queue headroom");
    let cases = [
        (repeated(window_packets / 3), too_often),
        (repeated(window_packets / 2 + 1), None),
        (vec![(400, Xrun(headroom))], None),
        (vec![(400, Xrun(headroom + 1))], too_long),
        (vec![(400, Xrun(-queue_frames))], None),
        (vec![(400, Xrun(-queue_frames - 1))], too_long),
    ];
    for (events, fault) in cases {
        let mut device = Device::new(0.25);
        let last = events.last().unwrap().0;
        device.run(0..last, &events);
        let position = device.position;
        device.run(last..last + 1, &events);
        let metrics = device.capture.counters.metrics(0);
        assert_eq!(device.capture.counters.stream.error_message(), fault);
        assert_eq!(metrics.discontinuities, events.len() as u64);
        if fault.is_some() {
            assert_eq!(device.position, position, "{events:?} queued audio");
        }
    }
}

#[test]
fn a_consumer_stall_up_to_half_a_second_is_absorbed_and_a_longer_one_latches() {
    let queue_frames = CAPTURE_QUEUE_BLOCKS * BLOCK_FRAMES;
    assert!(queue_frames >= SAMPLE_RATE as usize / 2);
    assert!(queue_frames < SAMPLE_RATE as usize / 2 + BLOCK_FRAMES);
    let mut capture = assembler(0.0);
    capture.capture_at(&vec![0.5; queue_frames * CHANNELS], 0);
    assert!(!capture.counters.stream.faulted.load(Ordering::Acquire));
    capture.capture_at(&[0.5; BLOCK_FRAMES * CHANNELS], queue_frames as u64);
    let metrics = capture.counters.metrics(0);
    assert!(metrics.faulted);
    assert_eq!(metrics.queue_overflow_frames, BLOCK_FRAMES as u64);
    assert_eq!(
        capture.counters.stream.error_message(),
        Some("capture queue overflowed; the consumer stopped keeping up")
    );
    capture.queue.pop();
    capture.capture_at(&[0.5; BLOCK_FRAMES * CHANNELS], queue_frames as u64);
    assert_eq!(capture.queue.len(), CAPTURE_QUEUE_BLOCKS - 1);
}

#[test]
fn stream_errors_fault_distinctly_while_capture_xruns_and_priority_denial_continue() {
    let messages: Vec<_> = (0..16)
        .filter_map(|code| {
            let counters = StreamCounters::default();
            counters.fault(code);
            counters.error_message()
        })
        .collect();
    assert_eq!(messages.len(), 11);
    assert!(
        messages
            .iter()
            .all(|message| messages.iter().filter(|other| *other == message).count() == 1)
    );

    let mut device = Device::new(0.25);
    let denied = cpal::ErrorKind::RealtimeDenied;
    device.capture.counters.stream.error(denied.into());
    device.run(0..2, &[(1, Event::Xrun(100))]);
    let metrics = device.capture.counters.metrics(0);
    assert!(!metrics.faulted);
    assert_eq!(
        (
            metrics.capture_priority_denied,
            metrics.device_xruns,
            metrics.discontinuities
        ),
        (1, 1, 1)
    );
    device.capture.capture_at(&[0.5; 3], 0);
    assert_eq!(
        device.capture.counters.stream.error_message(),
        Some("audio callback delivered a partial stereo frame")
    );

    let playback = StreamCounters::default();
    playback.error(denied.into());
    assert!(!playback.faulted.load(Ordering::Acquire));
    playback.error(cpal::ErrorKind::Xrun.into());
    assert_eq!(
        playback.error_message(),
        Some("audio output underrun invalidated audible timing")
    );
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
    for frame in 0..lag {
        let expected = [frame as f32 + 1.0, -(frame as f32 + 1.0)];
        assert_eq!(delay.frame([0.0; CHANNELS]), expected);
    }
}

#[test]
fn discard_request_keeps_receiver_playout_contiguous_and_silent_without_rebuffering() {
    fn step(
        capture: &mut CaptureAssembler,
        sender: &mut PlayoutSender,
        playout: &mut Playout,
        input: f32,
    ) -> Vec<f32> {
        let first_frame = capture.counters.device_frames.load(Ordering::Relaxed);
        capture.capture_at(&[input; BLOCK_FRAMES * CHANNELS], first_frame);
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

    let mut capture = assembler(0.25);
    let (mut playout, mut sender) = Playout::new(JITTER_BUFFER_MS).unwrap();
    let blocks_per_second = SAMPLE_RATE as usize / BLOCK_FRAMES;
    let mut played = Vec::new();
    for _ in 0..blocks_per_second {
        played = step(&mut capture, &mut sender, &mut playout, 0.5);
    }
    assert!(played.iter().all(|&sample| sample == 0.5));
    assert!(playout.is_primed());

    capture
        .counters
        .discard_requested
        .store(true, Ordering::Release);
    let mut after_discard = Vec::new();
    for _ in 0..blocks_per_second {
        after_discard.extend(step(&mut capture, &mut sender, &mut playout, 0.25));
        assert!(playout.is_primed(), "RX must not rebuffer");
    }
    assert!(!capture.counters.stream.faulted.load(Ordering::Acquire));
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
    let silent_frames = resumes - silence_starts;
    assert!(silent_frames.abs_diff(delay_frames - DISCARD_FADE_FRAMES + 1) <= 2);
    assert!(after_discard[resumes + 1..].iter().all(|&s| s == 0.25));
}

#[test]
fn capture_assembles_fixed_blocks_without_losing_callback_tails() {
    let mut capture = assembler(0.0);
    let mut position = 0;
    for frames in [1, 7, 333, 43] {
        let input: Vec<f32> = (position..position + frames)
            .flat_map(|frame| [frame as f32, -(frame as f32)])
            .collect();
        capture.capture_at(&input, position as u64);
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
        let mut capture = assembler(seconds);
        let delay_frames = (seconds * f64::from(SAMPLE_RATE)) as usize;
        for block_index in 0..delay_frames.div_ceil(BLOCK_FRAMES) + 1 {
            capture.capture_at(
                &[0.5; BLOCK_FRAMES * CHANNELS],
                (block_index * BLOCK_FRAMES) as u64,
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
        let metrics = capture.counters.metrics(0);
        assert!(!metrics.faulted);
        assert_eq!(metrics.queue_overflow_frames, 0);
    }
}

#[test]
fn queued_audio_retains_its_completion_time_across_later_callbacks() {
    let mut capture = assembler(0.0);
    capture.capture_at(&[0.5; CHANNELS], 0);
    let before_completion = Instant::now();
    capture.capture_at(&[0.5; (BLOCK_FRAMES - 1) * CHANNELS], 1);
    let after_completion = Instant::now();
    capture.capture_at(&[0.5; BLOCK_FRAMES * CHANNELS], BLOCK_FRAMES as u64);
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
        counters: Arc::new(StreamCounters::default()),
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
