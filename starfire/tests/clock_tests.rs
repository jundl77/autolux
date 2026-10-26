use starfire::clock::{
    BeatFact, MAX_BEATS_PER_BAR, MAX_SPEED_MULTIPLIER, MAX_TEMPO_BPM, MusicalClock,
};

fn counted(beat_index: u64, bpm: f64) -> BeatFact {
    BeatFact {
        beat_index,
        bar_phase: None,
        beats_per_bar: 4,
        bpm,
    }
}

fn phased(beat_index: u64, bar_phase: u32) -> BeatFact {
    BeatFact {
        bar_phase: Some(bar_phase),
        ..counted(beat_index, 120.0)
    }
}

#[test]
fn clock_waits_for_beat_input_and_tracks_tempo_changes() {
    let mut clock = MusicalClock::default();
    assert_eq!(clock.sample_at(60.0).unwrap().beat_position, 0.0);
    clock.set_beat(2.0, counted(8, 120.0)).unwrap();
    assert_eq!(clock.sample_at(5.0).unwrap().beat_position, 6.0);
    clock.set_beat(5.0, counted(14, 60.0)).unwrap();
    assert_eq!(clock.sample_at(7.0).unwrap().beat_position, 8.0);
}

#[test]
fn the_first_beat_anchors_at_its_bar_phase_not_its_index() {
    for (bar_phase, expected) in [(None, 0.0), (Some(0), 0.0), (Some(3), 3.0)] {
        let mut clock = MusicalClock::default();
        let before = clock.sample_at(1.0).unwrap();
        clock
            .set_beat(
                1.0,
                BeatFact {
                    bar_phase,
                    ..counted(1, 120.0)
                },
            )
            .unwrap();
        let after = clock.sample_at(1.0).unwrap();
        assert_eq!(after.beat_position, expected);
        assert_eq!(after.elapsed_musical_beats, before.elapsed_musical_beats);
    }
}

#[test]
fn a_renderer_restarted_mid_session_does_not_jump_by_the_session_beat_count() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, phased(90_001, 1)).unwrap();
    assert_eq!(clock.sample_at(0.0).unwrap().beat_position, 1.0);
    clock.set_beat(0.5, phased(90_002, 2)).unwrap();
    let sample = clock.sample_at(0.5).unwrap();
    assert_eq!(sample.beat_position, 2.0);
    assert_eq!(sample.elapsed_musical_beats, 1.0);
}

#[test]
fn phase_correction_moves_the_grid_forward_to_the_reported_beat_in_bar() {
    let mut clock = MusicalClock::default();
    let phases = [1, 2, 3, 0, 1, 3, 0, 1, 0];
    let mut positions = Vec::new();
    for (index, phase) in (1..).zip(phases) {
        let at = index as f64 * 0.5;
        clock.set_beat(at, phased(index, phase)).unwrap();
        positions.push(clock.sample_at(at).unwrap().beat_position);
    }
    assert_eq!(positions, [1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 8.0, 9.0, 12.0]);
}

#[test]
fn every_received_beat_lands_on_its_phase_and_the_grid_never_moves_back() {
    let mut clock = MusicalClock::default();
    let mut state: u64 = 0x2545_f491_4f6c_dd1d;
    let (mut index, mut previous) = (0_u64, None::<f64>);
    for step in 0..2_000_u32 {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        index += 1 + state % 3;
        let bar_phase = (!state.is_multiple_of(5)).then_some(((state >> 8) % 4) as u32);
        let at = f64::from(step) * 0.5;
        clock
            .set_beat(
                at,
                BeatFact {
                    bar_phase,
                    ..counted(index, 120.0)
                },
            )
            .unwrap();
        let position = clock.sample_at(at).unwrap().beat_position;
        if let Some(phase) = bar_phase {
            assert_eq!(position.floor() as u64 % 4, u64::from(phase));
        }
        if let Some(previous) = previous {
            assert!(position > previous, "grid moved back at step {step}");
        }
        previous = Some(position);
    }
}

#[test]
fn unknown_phase_counts_beats_and_keeps_the_last_alignment() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, counted(5, 120.0)).unwrap();
    assert_eq!(clock.sample_at(0.0).unwrap().beat_position, 0.0);
    clock.set_beat(0.5, counted(6, 120.0)).unwrap();
    assert_eq!(clock.sample_at(0.5).unwrap().beat_position, 1.0);
    clock.set_beat(1.0, phased(7, 0)).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().beat_position, 4.0);
    clock.set_beat(2.0, counted(10, 120.0)).unwrap();
    assert_eq!(clock.sample_at(2.0).unwrap().beat_position, 7.0);
}

#[test]
fn bar_fields_are_continuous_and_follow_beats_per_bar() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, phased(6, 2)).unwrap();
    clock.set_beat(0.5, phased(7, 3)).unwrap();
    let sample = clock.sample_at(0.75).unwrap();
    assert_eq!(
        (sample.beat_position, sample.bar_phase, sample.bar_index),
        (3.5, 3.5, 0)
    );
    let waltz = |beat_index, bar_phase| BeatFact {
        bar_phase: Some(bar_phase),
        beats_per_bar: 3,
        ..counted(beat_index, 120.0)
    };
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, waltz(4, 1)).unwrap();
    let edge = clock.sample_at(1.0).unwrap();
    assert_eq!(
        (
            edge.beat_position,
            edge.bar_phase,
            edge.bar_index,
            edge.beats_per_bar
        ),
        (3.0, 0.0, 1, 3)
    );
}

#[test]
fn beats_per_bar_cannot_change_within_a_session() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, phased(1, 0)).unwrap();
    let expected = clock.sample_at(0.5).unwrap();
    let change = BeatFact {
        beats_per_bar: 3,
        ..phased(2, 1)
    };
    let error = clock.set_beat(0.5, change).unwrap_err();
    assert!(error.to_string().contains("fixed for the session"));
    assert_eq!(clock.sample_at(0.5).unwrap(), expected);
}

#[test]
fn speed_integrates_continuously_without_changing_musical_beats() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, counted(0, 120.0)).unwrap();
    let before = clock.sample_at(3.0).unwrap();
    clock.set_speed(3.0, 2.0).unwrap();
    assert_eq!(clock.sample_at(3.0).unwrap(), before);
    let accelerated = clock.sample_at(5.0).unwrap();
    assert_eq!(accelerated.beat_position, 10.0);
    assert_eq!(accelerated.animation_beats, 14.0);
    clock.set_speed(5.0, 0.0).unwrap();
    let frozen = clock.sample_at(9.0).unwrap();
    assert_eq!(frozen.beat_position, 18.0);
    assert_eq!(frozen.animation_beats, 14.0);
}

#[test]
fn sampling_cadence_cannot_change_integrated_animation_position() {
    let mut sparse = MusicalClock::default();
    sparse.set_beat(0.0, counted(0, 127.3)).unwrap();
    let mut dense = sparse.clone();
    for index in 0..3_000 {
        dense.sample_at(index as f64 / 1_000.0).unwrap();
    }
    for clock in [&mut sparse, &mut dense] {
        clock.set_speed(3.0, 1.33).unwrap();
        clock.set_beat(7.0, counted(15, 129.7)).unwrap();
    }
    for index in 7_000..60_000 {
        dense.sample_at(index as f64 / 1_000.0).unwrap();
    }
    assert_eq!(
        sparse.sample_at(60.0).unwrap(),
        dense.sample_at(60.0).unwrap()
    );
}

#[test]
fn measured_beat_can_correct_extrapolation_but_cannot_rewind_received_beats() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, counted(10, 120.0)).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().beat_position, 2.0);
    clock.set_beat(1.0, counted(11, 120.0)).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().beat_position, 1.0);
    for stale in [11, 10] {
        assert!(clock.set_beat(2.0, counted(stale, 120.0)).is_err());
    }
    assert_eq!(clock.sample_at(2.0).unwrap().beat_position, 3.0);
}

#[test]
fn invalid_controls_and_overflow_are_rejected_without_mutation() {
    let mut clock = MusicalClock::default();
    clock.set_beat(2.0, counted(4, 120.0)).unwrap();
    let expected = clock.sample_at(4.0).unwrap();
    for value in [f64::NAN, f64::INFINITY, -1.0] {
        assert!(clock.sample_at(value).is_err());
        assert!(clock.set_beat(value, counted(6, 120.0)).is_err());
        assert!(clock.set_speed(3.0, value).is_err());
    }
    for bpm in [0.0, -1.0, f64::NAN, f64::INFINITY, MAX_TEMPO_BPM + 1.0] {
        assert!(clock.set_beat(3.0, counted(6, bpm)).is_err());
    }
    for (bar_phase, beats_per_bar) in [(None, 0), (None, MAX_BEATS_PER_BAR + 1), (Some(4), 4)] {
        let beat = BeatFact {
            bar_phase,
            beats_per_bar,
            ..counted(6, 120.0)
        };
        assert!(clock.set_beat(3.0, beat).is_err());
    }
    assert!(clock.set_beat(3.0, counted(u64::MAX, 120.0)).is_err());
    assert!(clock.set_speed(3.0, MAX_SPEED_MULTIPLIER + 1.0).is_err());
    assert!(clock.set_beat(1.0, counted(6, 120.0)).is_err());
    assert!(clock.sample_at(f64::MAX).is_err());
    assert_eq!(clock.sample_at(4.0).unwrap(), expected);
}

#[test]
fn beat_correction_after_speed_change_keeps_animation_continuous() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, counted(0, 120.0)).unwrap();
    clock.set_speed(0.1, 8.0).unwrap();
    let before = clock.sample_at(0.6).unwrap();
    assert!((before.beat_position - 1.2).abs() < 1e-12);
    clock.set_beat(0.6, counted(1, 90.0)).unwrap();
    let corrected = clock.sample_at(0.6).unwrap();
    assert_eq!(corrected.animation_beats, before.animation_beats);
    assert_eq!(corrected.beat_position, 1.0);
    let later = clock.sample_at(0.7).unwrap();
    assert!((later.animation_beats - 9.4).abs() < 1e-12);
    assert!((later.beat_position - 1.15).abs() < 1e-12);
}

#[test]
fn authoritative_beat_jumps_and_tempo_changes_preserve_integrated_animation_phase() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, counted(0, 120.0)).unwrap();
    clock.set_beat(1.0, counted(100, 120.0)).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().animation_beats, 2.0);
    clock.set_speed(2.0, 2.0).unwrap();
    let before = clock.sample_at(3.0).unwrap();
    clock.set_beat(3.0, counted(103, 60.0)).unwrap();
    assert_eq!(
        clock.sample_at(3.0).unwrap().animation_beats,
        before.animation_beats
    );
    let later = clock.sample_at(4.0).unwrap();
    assert_eq!(later.animation_beats, 10.0);
    assert_eq!(later.beat_position, 104.0);
}

#[test]
fn musical_elapsed_beats_ignore_phase_corrections_and_animation_speed() {
    let mut clock = MusicalClock::default();
    clock.set_beat(0.0, counted(0, 120.0)).unwrap();
    clock.set_speed(0.5, 0.0).unwrap();
    clock.set_beat(1.0, counted(100, 60.0)).unwrap();
    let sample = clock.sample_at(3.0).unwrap();
    assert_eq!(sample.beat_position, 102.0);
    assert_eq!(sample.elapsed_musical_beats, 4.0);
    assert_eq!(sample.animation_beats, 1.0);
}
