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

impl CaptureAssembler {
    fn capture_at(&mut self, input: &[f32], first_frame: u64) {
        let last_frame = first_frame + (input.len() / CHANNELS).saturating_sub(1) as u64;
        self.capture_timed(input, first_frame, last_frame, Instant::now());
    }

    fn capture_timed(&mut self, input: &[f32], first: u64, callback: u64, callback_at: Instant) {
        let device_nanos = |frame| 1_000_000_000 + frame_nanos(frame);
        let timing = CaptureTiming {
            captured_at_nanos: device_nanos(first),
            callback_nanos: device_nanos(callback),
            callback_at,
        };
        self.capture(input, timing);
    }
}

#[derive(Clone, Copy, Debug)]
enum Event {
    Xrun(i64),
    Jump(i64),
    CallbackStall(u64),
}

struct Device {
    capture: CaptureAssembler,
    started: Instant,
    next_frame: u64,
    callback_frame: u64,
    blocks: Vec<AudioBlock>,
}

impl Device {
    fn new(delay_seconds: f64) -> Self {
        Self {
            capture: assembler(delay_seconds),
            started: Instant::now(),
            next_frame: 0,
            callback_frame: 0,
            blocks: Vec::new(),
        }
    }

    fn run(&mut self, packets: std::ops::Range<usize>, events: &[(usize, Event)]) {
        for packet in packets {
            let mut stall = 0;
            for &(_, event) in events.iter().filter(|(at, _)| *at == packet) {
                match event {
                    Event::Xrun(gap) | Event::Jump(gap) => {
                        self.next_frame = self.next_frame.checked_add_signed(gap).unwrap();
                    }
                    Event::CallbackStall(frames) => stall = frames,
                }
                if let Event::Xrun(_) = event {
                    let stream = &self.capture.counters.stream;
                    stream.error(cpal::ErrorKind::Xrun.into());
                }
            }
            let first = self.next_frame;
            let input: Vec<f32> = (first + 1..=first + PACKET_FRAMES)
                .flat_map(|sample| [sample as f32, -(sample as f32)])
                .collect();
            self.callback_frame = self.callback_frame.max(first + PACKET_FRAMES - 1 + stall);
            let callback_at =
                self.started + Duration::from_nanos(frame_nanos(self.callback_frame) as u64);
            self.capture
                .capture_timed(&input, first, self.callback_frame, callback_at);
            self.next_frame += PACKET_FRAMES;
            self.blocks
                .extend(std::iter::from_fn(|| self.capture.queue.pop()));
        }
    }
}

type Bridge<'a> = (&'a [(usize, Event)], i64, u64, u64, u64);

#[test]
fn discontinuities_reanchor_the_sample_timeline_to_device_time() {
    use Event::*;
    let band = CAPTURE_JITTER_DEAD_BAND_FRAMES as i64;
    let stall = (10, CallbackStall(20 * PACKET_FRAMES));
    let cases: [Bridge<'_>; 4] = [
        (&[(60, Xrun(3_000))], 3_000, 3_000, 0, 0),
        (&[(40, Xrun(-600))], -600, 0, 600, 0),
        (
            &[(20, Jump(band)), (50, Jump(1_000))],
            1_000,
            1_000,
            0,
            band as u64,
        ),
        (&[stall, (20, Xrun(3_000))], 3_000, 3_000, 0, 0),
    ];
    for (events, gap, silence, dropped, tolerance) in cases {
        let mut device = Device::new(0.25);
        device.run(0..100, events);
        let metrics = device.capture.counters.metrics(0);
        assert!(!metrics.faulted, "{events:?}");
        assert_eq!(metrics.device_frames, 100 * PACKET_FRAMES);
        assert_eq!(metrics.discontinuities, 1);
        assert_eq!(metrics.last_gap_frames, Some(gap));
        assert_eq!(metrics.resync_silence_frames, silence);
        assert_eq!(metrics.resync_dropped_frames, dropped);
        let queued = (device.blocks.len() * BLOCK_FRAMES) as u64;
        let emitted = queued + u64::from(device.capture.partial.frames);
        assert!(emitted.abs_diff(device.next_frame) <= tolerance);

        let delay_frames = device.capture.delay.samples.len() as u64;
        let stamp_tolerance = Duration::from_nanos(frame_nanos(tolerance) as u64 + 1_000);
        let mut audible = 0;
        for (index, block) in device.blocks.iter().enumerate() {
            let position = (index * BLOCK_FRAMES) as u64;
            assert_eq!(block.audio_frame_position, position);
            let last_frame = frame_nanos(position + BLOCK_FRAMES as u64 - 1) as u64;
            let expected = device.started + Duration::from_nanos(last_frame);
            let stamp = block.assembly_completed_at;
            assert!(stamp.max(expected) - stamp.min(expected) <= stamp_tolerance);
            for (frame, &sample) in block.samples.iter().step_by(CHANNELS).enumerate() {
                let transmitted = position + frame as u64 + 1;
                if sample != 0.0 {
                    assert!(transmitted.abs_diff(sample as u64 + delay_frames) <= tolerance);
                    audible += 1;
                }
            }
        }
        assert_eq!(audible, queued - delay_frames - silence);
        let stamps = device
            .blocks
            .is_sorted_by_key(|block| block.assembly_completed_at);
        assert!(tolerance != 0 || stamps, "{events:?}");
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
            .collect()
    };
    let queue_frames = (CAPTURE_QUEUE_BLOCKS * BLOCK_FRAMES) as i64;
    let headroom =
        queue_frames - (400 * PACKET_FRAMES % BLOCK_FRAMES as u64 + PACKET_FRAMES) as i64;
    let too_often = Some("capture discontinuities repeated too often");
    let too_long = Some("a capture gap exceeded the capture queue headroom");
    let cases: [(Vec<_>, _); 6] = [
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
        let blocks = device.blocks.len();
        device.run(last..last + 1, &events);
        assert_eq!(device.capture.counters.stream.error_message(), fault);
        let discontinuities = device.capture.counters.metrics(0).discontinuities;
        assert_eq!(discontinuities, events.len() as u64);
        assert!(fault.is_none() || device.blocks.len() == blocks);
    }
}

#[test]
fn a_consumer_stall_up_to_half_a_second_is_absorbed_and_a_longer_one_latches() {
    let queue_frames = CAPTURE_QUEUE_BLOCKS * BLOCK_FRAMES;
    let half_second = SAMPLE_RATE as usize / 2;
    assert_eq!(queue_frames, half_second.next_multiple_of(BLOCK_FRAMES));
    let mut capture = assembler(0.0);
    capture.capture_at(&vec![0.5; queue_frames * CHANNELS], 0);
    assert!(!capture.counters.stream.faulted.load(Acquire));
    capture.capture_at(&[0.5; BLOCK_FRAMES * CHANNELS], queue_frames as u64);
    let overflow_frames = capture.counters.metrics(0).queue_overflow_frames;
    assert_eq!(overflow_frames, BLOCK_FRAMES as u64);
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
    let message = |code| {
        let counters = StreamCounters::default();
        counters.fault(code);
        counters.error_message()
    };
    let messages: std::collections::HashSet<_> = (0..16).filter_map(message).collect();
    assert_eq!(messages.len(), 11);

    let mut device = Device::new(0.25);
    let denied = cpal::ErrorKind::RealtimeDenied;
    device.capture.counters.stream.error(denied.into());
    device.run(0..2, &[(1, Event::Xrun(100))]);
    let metrics = device.capture.counters.metrics(0);
    assert!(!metrics.faulted);
    assert_eq!(metrics.capture_priority_denied, 1);
    assert_eq!((metrics.device_xruns, metrics.discontinuities), (1, 1));
    device.capture.capture_at(&[0.5; 3], 0);
    assert_eq!(
        device.capture.counters.stream.error_message(),
        Some("audio callback delivered a partial stereo frame")
    );

    let mut untimed = Device::new(0.25);
    untimed.run(0..1, &[]);
    let timing = CaptureTiming {
        captured_at_nanos: 0,
        callback_nanos: 0,
        callback_at: Instant::now(),
    };
    untimed
        .capture
        .capture(&[0.5; BLOCK_FRAMES * CHANNELS], timing);
    let stamped = untimed.capture.queue.pop().unwrap().assembly_completed_at;
    assert_eq!(stamped, timing.callback_at);
    untimed
        .capture
        .counters
        .stream
        .error(cpal::ErrorKind::Xrun.into());
    untimed.capture.capture(&[0.5; CHANNELS], timing);
    assert_eq!(
        untimed.capture.counters.stream.error_message(),
        Some("a capture discontinuity arrived without a device timestamp")
    );

    let disconnected = StreamCounters::default();
    disconnected.error(cpal::ErrorKind::DeviceNotAvailable.into());
    assert_eq!(
        disconnected.error_message(),
        Some("audio endpoint disconnected")
    );

    let playback = StreamCounters::default();
    playback.error(denied.into());
    assert!(!playback.faulted.load(Acquire));
    playback.error(cpal::ErrorKind::Xrun.into());
    assert_eq!(
        playback.error_message(),
        Some("audio output underrun invalidated audible timing")
    );
}

#[test]
fn discard_request_fades_to_contiguous_silence_without_rebuffering_receiver_playout() {
    let mut capture = assembler(0.25);
    let (mut playout, mut sender) = Playout::new(JITTER_BUFFER_MS).unwrap();
    let mut step = |capture: &mut CaptureAssembler, input: f32| {
        let first_frame = capture.counters.device_frames.load(Relaxed);
        capture.capture_at(&[input; BLOCK_FRAMES * CHANNELS], first_frame);
        while let Some(audio) = capture.queue.pop() {
            let block = PlaybackBlock {
                audio,
                light_sequence_number: 1,
                channels: [0; 512],
            };
            sender.enqueue(block).unwrap();
        }
        playout.begin_callback();
        let mut output = [[0.0; CHANNELS]; BLOCK_FRAMES];
        for frame in &mut output {
            playout.frame(frame);
        }
        while sender.lighting.pop().is_some() {}
        (output.map(|frame| frame[0]), playout.is_primed())
    };
    let blocks_per_second = SAMPLE_RATE as usize / BLOCK_FRAMES;
    let (played, primed) =
        (0..blocks_per_second).fold(([0.0; BLOCK_FRAMES], false), |_, _| step(&mut capture, 0.5));
    assert!(primed && played.iter().all(|&sample| sample == 0.5));

    capture.counters.discard_requested.store(true, Release);
    let mut after = Vec::new();
    for _ in 0..blocks_per_second {
        let (played, primed) = step(&mut capture, 0.25);
        assert!(primed, "RX must not rebuffer");
        after.extend(played);
    }
    assert!(!capture.counters.stream.faulted.load(Acquire));
    assert_eq!(sender.counters.underruns.load(Relaxed), 0);
    assert_eq!(sender.counters.rebuffer_count.load(Relaxed), 0);

    let silence_starts = after.iter().position(|&sample| sample == 0.0).unwrap();
    assert!(after[..silence_starts].is_sorted_by(|a, b| a >= b));
    let silent = after[silence_starts..]
        .iter()
        .position(|&s| s > 0.0)
        .unwrap();
    let delay_frames = (0.25 * f64::from(SAMPLE_RATE)).round() as usize;
    assert!(silent.abs_diff(delay_frames - DISCARD_FADE_FRAMES + 1) <= 2);
    assert!(
        after[silence_starts + silent + 1..]
            .iter()
            .all(|&sample| sample == 0.25)
    );
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

#[test]
fn capture_scheduling_is_unknown_until_recorded_and_denial_counts() {
    use crate::realtime::Scheduling;
    let counters = CaptureCounters::default();
    assert_eq!(counters.metrics(0).capture_scheduling, None);
    for scheduling in [Scheduling::Mmcss, Scheduling::TimeCritical] {
        counters.record_scheduling(scheduling);
        assert_eq!(counters.metrics(0).capture_scheduling, Some(scheduling));
    }
    assert_eq!(counters.metrics(0).capture_priority_denied, 0);
    counters.record_scheduling(Scheduling::Denied);
    let metrics = counters.metrics(0);
    assert_eq!(metrics.capture_scheduling, Some(Scheduling::Denied));
    assert_eq!(metrics.capture_priority_denied, 1);
}
