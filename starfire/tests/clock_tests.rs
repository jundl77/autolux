use starfire::clock::{MAX_SPEED_MULTIPLIER, MAX_TEMPO_BPM, MusicalClock};

#[test]
fn clock_waits_for_beat_input_and_tracks_tempo_changes() {
    let mut clock = MusicalClock::default();
    assert_eq!(clock.sample_at(60.0).unwrap().beat_position, 0.0);
    clock.set_beat_anchor(2.0, 8.0, 120.0).unwrap();
    assert_eq!(clock.sample_at(5.0).unwrap().beat_position, 14.0);
    clock.set_beat_anchor(5.0, 14.0, 60.0).unwrap();
    assert_eq!(clock.sample_at(7.0).unwrap().beat_position, 16.0);
}

#[test]
fn speed_integrates_continuously_without_changing_musical_beats() {
    let mut clock = MusicalClock::default();
    clock.set_beat_anchor(0.0, 0.0, 120.0).unwrap();
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
    sparse.set_beat_anchor(0.0, 0.0, 127.3).unwrap();
    let mut dense = sparse.clone();
    for index in 0..3_000 {
        dense.sample_at(index as f64 / 1_000.0).unwrap();
    }
    for clock in [&mut sparse, &mut dense] {
        clock.set_speed(3.0, 1.33).unwrap();
        clock.set_beat_anchor(7.0, 14.8, 129.7).unwrap();
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
fn measured_anchor_can_correct_extrapolation_but_cannot_rewind_received_beats() {
    let mut clock = MusicalClock::default();
    clock.set_beat_anchor(0.0, 10.0, 120.0).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().beat_position, 12.0);
    clock.set_beat_anchor(1.0, 11.9, 120.0).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().beat_position, 11.9);
    assert!(clock.set_beat_anchor(2.0, 11.8, 120.0).is_err());
    assert_eq!(clock.sample_at(2.0).unwrap().beat_position, 13.9);
}

#[test]
fn invalid_controls_and_overflow_are_rejected_without_mutation() {
    let mut clock = MusicalClock::default();
    clock.set_beat_anchor(2.0, 4.0, 120.0).unwrap();
    let expected = clock.sample_at(4.0).unwrap();
    for value in [f64::NAN, f64::INFINITY, -1.0] {
        assert!(clock.sample_at(value).is_err());
        assert!(clock.set_beat_anchor(3.0, value, 120.0).is_err());
        assert!(clock.set_speed(3.0, value).is_err());
    }
    for bpm in [0.0, -1.0, f64::NAN, f64::INFINITY, MAX_TEMPO_BPM + 1.0] {
        assert!(clock.set_beat_anchor(3.0, 6.0, bpm).is_err());
    }
    assert!(clock.set_speed(3.0, MAX_SPEED_MULTIPLIER + 1.0).is_err());
    assert!(clock.set_beat_anchor(1.0, 4.0, 120.0).is_err());
    assert!(clock.sample_at(f64::MAX).is_err());
    assert_eq!(clock.sample_at(4.0).unwrap(), expected);
}

#[test]
fn beat_correction_after_speed_change_cannot_make_animation_negative() {
    let mut clock = MusicalClock::default();
    clock.set_beat_anchor(0.0, 0.0, 120.0).unwrap();
    clock.set_speed(0.1, 8.0).unwrap();
    let before = clock.sample_at(0.11).unwrap();
    clock.set_beat_anchor(0.11, 0.1, 90.0).unwrap();
    let corrected = clock.sample_at(0.11).unwrap();
    assert_eq!(corrected.animation_beats, before.animation_beats);
    assert!(corrected.animation_beats > 0.0);
    assert_eq!(corrected.beat_position, 0.1);
    let later = clock.sample_at(0.21).unwrap();
    assert!((later.animation_beats - 1.56).abs() < 1e-12);
    assert!((later.beat_position - 0.25).abs() < 1e-12);
}

#[test]
fn authoritative_beat_jumps_and_tempo_changes_preserve_integrated_animation_phase() {
    let mut clock = MusicalClock::default();
    clock.set_beat_anchor(0.0, 0.0, 120.0).unwrap();
    clock.set_beat_anchor(1.0, 100.0, 120.0).unwrap();
    assert_eq!(clock.sample_at(1.0).unwrap().animation_beats, 2.0);
    clock.set_speed(2.0, 2.0).unwrap();
    let before = clock.sample_at(3.0).unwrap();
    clock.set_beat_anchor(3.0, 103.0, 60.0).unwrap();
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
    clock.set_beat_anchor(0.0, 0.0, 120.0).unwrap();
    clock.set_speed(0.5, 0.0).unwrap();
    clock.set_beat_anchor(1.0, 100.0, 60.0).unwrap();
    let sample = clock.sample_at(3.0).unwrap();
    assert_eq!(sample.beat_position, 102.0);
    assert_eq!(sample.elapsed_musical_beats, 4.0);
    assert_eq!(sample.animation_beats, 1.0);
}
