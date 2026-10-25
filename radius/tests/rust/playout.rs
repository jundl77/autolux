use super::*;

fn block(position: u64, sequence_number: u32) -> PlaybackBlock {
    PlaybackBlock {
        audio: AudioBlock {
            audio_frame_position: position,
            assembly_completed_at: std::time::Instant::now(),
            frames: BLOCK_FRAMES as u16,
            samples: [0.5; BLOCK_FRAMES * CHANNELS],
        },
        light_sequence_number: sequence_number,
        channels: [sequence_number as u8; 512],
    }
}

fn fill(sender: &mut PlayoutSender, first: u64, blocks: usize) {
    for index in 0..blocks {
        sender
            .enqueue(block(first + (index * BLOCK_FRAMES) as u64, 1))
            .unwrap();
    }
}

#[test]
fn startup_requires_full_500ms_and_audio_light_are_admitted_together() {
    let (mut playout, mut sender) = Playout::new(500).unwrap();
    fill(&mut sender, 0, 172);
    playout.begin_callback();
    let mut output = [9.0; CHANNELS];
    assert_eq!(playout.frame(&mut output), None);
    assert_eq!(output, [0.0; CHANNELS]);
    assert!(sender.lighting.is_empty());
    sender.enqueue(block(172 * BLOCK_FRAMES as u64, 1)).unwrap();
    playout.begin_callback();
    assert_eq!(playout.frame(&mut output), Some(0));
    assert_eq!(output, [0.5; CHANNELS]);
    let update = sender.lighting.pop().unwrap();
    assert_eq!(update.audio_frame_position, 0);
    assert_eq!(update.channels, [1; 512]);
}

#[test]
fn starvation_silences_immediately_then_requires_full_rebuffer_and_republishes_light() {
    let (mut playout, mut sender) = Playout::new(500).unwrap();
    fill(&mut sender, 0, 173);
    playout.begin_callback();
    let mut output = [0.0; CHANNELS];
    for _ in 0..23_000 {
        playout.frame(&mut output);
    }
    assert_eq!(output, [0.0; CHANNELS]);
    assert!(!playout.is_primed());
    assert_eq!(sender.counters.underruns.load(Ordering::Relaxed), 1);
    sender.lighting.pop();
    let next = 173 * BLOCK_FRAMES as u64;
    fill(&mut sender, next, 172);
    playout.begin_callback();
    assert_eq!(playout.frame(&mut output), None);
    assert!(sender.lighting.is_empty());
    sender
        .enqueue(block(next + 172 * BLOCK_FRAMES as u64, 1))
        .unwrap();
    playout.begin_callback();
    assert!(playout.frame(&mut output).is_some());
    assert_eq!(sender.counters.rebuffer_count.load(Ordering::Relaxed), 1);
    let update = sender.lighting.pop().unwrap();
    assert_eq!(update.audio_frame_position, next);
}

#[test]
fn starvation_emits_the_final_buffered_source_frame_before_silence() {
    let (mut playout, mut sender) = Playout::new(500).unwrap();
    fill(&mut sender, 0, 173);
    playout.begin_callback();
    // Remove resampling from this exact frame-conservation check.
    playout.ratio = 1.0;
    let mut output = [0.0; CHANNELS];
    for position in 0..173 * BLOCK_FRAMES as u64 {
        assert_eq!(playout.frame(&mut output), Some(position));
        assert_eq!(output, [0.5; CHANNELS]);
    }
    assert_eq!(playout.frame(&mut output), None);
    assert_eq!(output, [0.0; CHANNELS]);
    assert_eq!(sender.counters.underruns.load(Ordering::Relaxed), 1);
}

#[test]
fn invalid_order_gap_and_full_queue_preserve_admission_state() {
    let (_playout, mut sender) = Playout::new(500).unwrap();
    sender.enqueue(block(0, 1)).unwrap();
    assert_eq!(sender.enqueue(block(0, 2)), Err(EnqueueError::OutOfOrder));
    assert_eq!(sender.enqueue(block(256, 2)), Err(EnqueueError::Gap));
    let mut invalid = block(128, 2);
    invalid.audio.samples[1] = f32::NAN;
    assert_eq!(sender.enqueue(invalid), Err(EnqueueError::InvalidBlock));
    fill(&mut sender, 128, PLAYBACK_QUEUE_BLOCKS - 1);
    let position = PLAYBACK_QUEUE_BLOCKS as u64 * BLOCK_FRAMES as u64;
    assert_eq!(sender.enqueue(block(position, 2)), Err(EnqueueError::Full));
    assert_eq!(
        sender.latest_sample_position.load(Ordering::Acquire),
        position
    );
    assert!(sender.lighting.is_empty());
    sender.queue.pop();
    sender.enqueue(block(position, 2)).unwrap();
}

#[test]
fn lighting_queue_overflow_stops_audio_before_unscheduled_state_is_played() {
    let (mut playout, mut sender) = Playout::new(500).unwrap();
    for index in 0..LIGHTING_QUEUE_UPDATES {
        sender
            .lighting
            .push(LightingUpdate {
                audio_frame_position: index as u64,
                sequence_number: index as u32,
                channels: [0; 512],
            })
            .unwrap();
    }
    fill(&mut sender, 0, 173);
    playout.begin_callback();
    assert!(!playout.is_primed());
    assert!(sender.counters.faulted.load(Ordering::Acquire));
    assert_eq!(
        sender.counters.lighting_overflows.load(Ordering::Relaxed),
        1
    );
}

#[test]
fn independent_hardware_clock_drift_remains_bounded_without_drops_or_rebuffers() {
    for ppm in [-500.0, 500.0] {
        let (mut playout, mut sender) = Playout::new(500).unwrap();
        fill(&mut sender, 0, 173);
        let mut next_position = 173 * BLOCK_FRAMES as u64;
        let mut generated = 0.0;
        let mut previous_position = 0;
        let mut output = [0.0; CHANNELS];
        for _ in 0..12_000 {
            generated += 441.0 * (1.0 + ppm / 1_000_000.0);
            while generated >= BLOCK_FRAMES as f64 {
                sender.enqueue(block(next_position, 1)).unwrap();
                next_position += BLOCK_FRAMES as u64;
                generated -= BLOCK_FRAMES as f64;
            }
            playout.begin_callback();
            for _ in 0..441 {
                let position = playout
                    .frame(&mut output)
                    .expect("must remain primed across independent clocks");
                assert!(position >= previous_position);
                previous_position = position;
            }
            while sender.lighting.pop().is_some() {}
        }
        let fill = sender.counters.buffered_frames.load(Ordering::Relaxed);
        assert!(
            (17_640..=26_460).contains(&fill),
            "buffer departed 400..600 ms: {fill}"
        );
        assert!(sender.counters.drift_ppm.load(Ordering::Relaxed).abs() <= 1000);
        assert_eq!(sender.counters.underruns.load(Ordering::Relaxed), 0);
        assert_eq!(
            sender
                .counters
                .queue_overflow_frames
                .load(Ordering::Relaxed),
            0
        );
    }
}
