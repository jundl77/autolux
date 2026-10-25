use std::fs;
use std::path::PathBuf;

use starfire::engine::{Engine, RenderedFrame};
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Transition};
use starfire::replay::{Replay, dmx_digest, render_replay};
use starfire::script::SequenceCatalog;

const FLOAT_TOLERANCE: f64 = 1e-10;
const MAX_INTENSITY_STEP: f64 = 0.06;

struct Look {
    bank: &'static str,
    sequence: &'static str,
    wash_range: (f64, f64),
    mover_range: (f64, f64),
    pan_range: (f64, f64),
    tilt_range: (f64, f64),
    loop_beats: f64,
}

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn within(value: f64, range: (f64, f64)) {
    assert!(
        value.is_finite()
            && value >= range.0 - FLOAT_TOLERANCE
            && value <= range.1 + FLOAT_TOLERANCE,
        "{value} outside {range:?}"
    );
}

fn assert_semantics(frames: &[RenderedFrame], look: &Look) {
    let mut observed_intensity = (f64::INFINITY, f64::NEG_INFINITY);
    let mut observed_pan = (f64::INFINITY, f64::NEG_INFINITY);
    for frame in frames {
        assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
        assert_eq!(frame.active_bank.as_deref(), Some(look.bank));
        assert_eq!(frame.active_sequence.as_deref(), Some(look.sequence));
        assert_eq!(frame.fixture_properties_before_strobe.len(), 6);
        assert_eq!(frame.dmx_frames_by_universe[&0].len(), 512);
        for (id, state) in &frame.fixture_properties_before_strobe {
            let mover = id.starts_with("spot_");
            within(
                state.intensity,
                if mover {
                    look.mover_range
                } else {
                    look.wash_range
                },
            );
            assert_eq!(
                state.strobe_hz, 0.0,
                "baseline sequences must never repeat strobe"
            );
            // Saturated blue through violet, never the previously observed green.
            within(state.colour.blue, (1.0, 1.0));
            within(state.colour.green, (0.15, 0.15));
            within(state.colour.red, (0.15, 0.65));
            if mover {
                within(state.pan, look.pan_range);
                within(state.tilt, look.tilt_range);
                observed_pan.0 = observed_pan.0.min(state.pan);
                observed_pan.1 = observed_pan.1.max(state.pan);
            } else {
                observed_intensity.0 = observed_intensity.0.min(state.intensity);
                observed_intensity.1 = observed_intensity.1.max(state.intensity);
            }
        }
    }
    within(observed_intensity.0, (look.wash_range.0, look.wash_range.0));
    within(observed_intensity.1, (look.wash_range.1, look.wash_range.1));
    within(observed_pan.0, (look.pan_range.0, look.pan_range.0));
    within(observed_pan.1, (look.pan_range.1, look.pan_range.1));
    for pair in frames.windows(2) {
        for (id, before) in &pair[0].fixture_properties_before_strobe {
            let after = &pair[1].fixture_properties_before_strobe[id];
            assert!((after.intensity - before.intensity).abs() <= MAX_INTENSITY_STEP);
        }
    }

    // The first minute is 120 BPM. A full phrase repeats with no ramp reset.
    let period_frames = (look.loop_beats / 2.0 * 40.0) as usize;
    for index in 0..period_frames {
        let first = &frames[index].dmx_frames_by_universe;
        let repeated = &frames[index + period_frames].dmx_frames_by_universe;
        assert_eq!(
            first, repeated,
            "loop changed after one period at frame {index}"
        );
    }
}

fn check_replay(look: Look) {
    let replay = Replay::load(&root().join(format!("tests/replays/{}.json", look.bank))).unwrap();
    let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
    let rig = FixtureRig::load(&root().join("rigs/room.json")).unwrap();
    let first = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    assert_eq!(first.frames.len(), 3601);
    assert_semantics(&first.frames, &look);

    let second = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    let digest = dmx_digest(&first.frames);
    assert_eq!(digest, dmx_digest(&second.frames));

    // Duplicate selections cannot restart the current sequence.
    let mut without_duplicate = replay.clone();
    without_duplicate.events.retain(|event| {
        event.at_seconds == 0.0 || !matches!(event.command, Command::Select { .. })
    });
    let continued = render_replay(&without_duplicate, rig.clone(), catalog.clone()).unwrap();
    assert_eq!(digest, dmx_digest(&continued.frames));

    // Frame rate changes sampling density, never the authored effect at a given time.
    let mut slower = replay.clone();
    slower.frame_rate_hz = 20;
    let slower = render_replay(&slower, rig, catalog).unwrap();
    for (index, frame) in slower.frames.iter().enumerate() {
        assert_eq!(
            frame.dmx_frames_by_universe,
            first.frames[index * 2].dmx_frames_by_universe
        );
    }

    let expected =
        fs::read_to_string(root().join(format!("tests/golden/{}.sha256", look.bank))).unwrap();
    assert_eq!(
        digest,
        expected.trim(),
        "review semantic checks before accepting a new digest for {}",
        look.bank
    );
}

#[test]
fn atmospheric_blue_tide_replay() {
    check_replay(Look {
        bank: "atmospheric",
        sequence: "blue_tide",
        wash_range: (0.18, 0.18),
        mover_range: (0.22, 0.22),
        pan_range: (0.42, 0.58),
        tilt_range: (0.38, 0.48),
        loop_beats: 64.0,
    });
}

#[test]
fn every_bank_uses_measured_energy_without_changing_movement_or_repeating_strobe() {
    for (bank, sequence) in [
        ("atmospheric", "blue_tide"),
        ("breakdown", "violet_space"),
        ("buildup", "current"),
        ("drop", "blue_impact"),
    ] {
        let rig = FixtureRig::load(&root().join("rigs/room.json")).unwrap();
        let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
        let mut engine = Engine::new(rig, catalog).unwrap();
        engine
            .apply(
                0.0,
                &Command::Beat {
                    position_beats: 4.0,
                    bpm: 140.0,
                },
            )
            .unwrap();
        engine
            .apply(
                0.0,
                &Command::Select {
                    bank: bank.into(),
                    sequence: sequence.into(),
                    transition: Transition::Cut,
                },
            )
            .unwrap();
        let strong = engine.render_at(0.25).unwrap();
        engine
            .apply(0.25, &Command::SetNormalizedRmsLevel { level: 0.0 })
            .unwrap();
        let quiet = engine.render_at(0.25).unwrap();
        for (id, strong) in &strong.fixture_properties_before_strobe {
            let quiet = &quiet.fixture_properties_before_strobe[id];
            within(
                quiet.intensity,
                (strong.intensity * 0.65, strong.intensity * 0.65),
            );
            assert_eq!(strong.pan, quiet.pan);
            assert_eq!(strong.tilt, quiet.tilt);
            assert_eq!(strong.colour, quiet.colour);
            assert_eq!(quiet.strobe_hz, 0.0);
        }
        assert_ne!(strong.dmx_frames_by_universe, quiet.dmx_frames_by_universe);
    }
}

#[test]
fn breakdown_violet_space_replay() {
    check_replay(Look {
        bank: "breakdown",
        sequence: "violet_space",
        wash_range: (0.10, 0.10),
        mover_range: (0.12, 0.12),
        pan_range: (0.5, 0.5),
        tilt_range: (0.42, 0.42),
        loop_beats: 4.0,
    });
}

#[test]
fn buildup_current_replay() {
    check_replay(Look {
        bank: "buildup",
        sequence: "current",
        wash_range: (0.22, 0.48),
        mover_range: (0.22, 0.48),
        pan_range: (0.35, 0.65),
        tilt_range: (0.32, 0.52),
        loop_beats: 16.0,
    });
}

#[test]
fn drop_blue_impact_replay() {
    check_replay(Look {
        bank: "drop",
        sequence: "blue_impact",
        wash_range: (0.55, 0.78),
        mover_range: (0.55, 0.78),
        pan_range: (0.28, 0.72),
        tilt_range: (0.28, 0.56),
        loop_beats: 8.0,
    });
}

#[test]
fn every_registered_sequence_has_an_executable_replay_and_expected_dmx() {
    let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
    for entry in catalog.list() {
        let replay = Replay::load(&root().join(format!("tests/replays/{}.json", entry.bank)))
            .expect("every authored sequence needs a replay fixture");
        assert!(
            replay.events.iter().any(|event| {
                matches!(&event.command, Command::Select { bank, sequence, .. }
                if bank == &entry.bank && sequence == &entry.sequence)
            }),
            "replay does not exercise {}/{}",
            entry.bank,
            entry.sequence
        );
        let result = render_replay(
            &replay,
            FixtureRig::load(&root().join("rigs/room.json")).unwrap(),
            catalog.clone(),
        )
        .unwrap();
        let expected =
            fs::read_to_string(root().join(format!("tests/golden/{}.sha256", entry.bank)))
                .expect("every authored sequence needs a reviewed full DMX digest");
        assert!(
            result
                .frames
                .iter()
                .all(|frame| frame.script_error.is_none())
        );
        assert_eq!(dmx_digest(&result.frames), expected.trim());
    }
}

#[test]
fn the_same_sequences_work_on_a_different_rig_through_named_groups() {
    let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
    let rig = FixtureRig::load(&root().join("rigs/generic.json")).unwrap();
    for entry in catalog.list() {
        let mut replay =
            Replay::load(&root().join(format!("tests/replays/{}.json", entry.bank))).unwrap();
        replay.duration_seconds = 8.0;
        replay
            .events
            .retain(|event| event.at_seconds <= replay.duration_seconds);
        let rendered = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
        for frame in rendered.frames {
            assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
            assert_eq!(frame.dmx_frames_by_universe.len(), 2);
            assert!(
                frame
                    .fixture_properties_before_strobe
                    .values()
                    .all(|state| state.intensity > 0.0 && state.strobe_hz == 0.0)
            );
        }
    }
}

#[test]
fn rhythmic_pulses_follow_authoritative_beats_while_movement_keeps_its_phase() {
    let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
    let rig = FixtureRig::load(&root().join("rigs/room.json")).unwrap();
    for (bank, sequence, low, high) in [
        ("buildup", "current", 0.22, 0.48),
        ("drop", "blue_impact", 0.55, 0.78),
    ] {
        let mut engine = Engine::new(rig.clone(), catalog.clone()).unwrap();
        engine
            .apply(
                0.0,
                &Command::Beat {
                    position_beats: 0.0,
                    bpm: 120.0,
                },
            )
            .unwrap();
        // Selection halfway between beats must not offset the recurring pulse.
        engine
            .apply(
                0.25,
                &Command::Select {
                    bank: bank.into(),
                    sequence: sequence.into(),
                    transition: Transition::Cut,
                },
            )
            .unwrap();
        let selected = engine.render_at(0.25).unwrap();
        within(
            selected.fixture_properties_before_strobe["wash_left"].intensity,
            ((low + high) / 2.0, (low + high) / 2.0),
        );

        for (seconds, beat, bpm, intensity) in [
            (1.0, 1.0, 120.0, high),
            (2.0, 2.0, 90.0, low),
            (3.0, 5.0, 150.0, high),
        ] {
            if seconds == 2.0 {
                engine
                    .apply(1.25, &Command::SetSpeed { multiplier: 2.0 })
                    .unwrap();
            }
            let before = engine.render_at(seconds).unwrap();
            engine
                .apply(
                    seconds,
                    &Command::Beat {
                        position_beats: beat,
                        bpm,
                    },
                )
                .unwrap();
            let corrected = engine.render_at(seconds).unwrap();
            within(
                corrected.fixture_properties_before_strobe["wash_left"].intensity,
                (intensity, intensity),
            );
            for (id, before) in before.fixture_properties_before_strobe {
                let after = &corrected.fixture_properties_before_strobe[&id];
                assert_eq!(before.pan, after.pan);
                assert_eq!(before.tilt, after.tilt);
                assert_eq!(before.colour, after.colour);
            }
            if seconds == 2.0 {
                let next_beat = engine.render_at(2.0 + 2.0 / 3.0).unwrap();
                within(
                    next_beat.fixture_properties_before_strobe["wash_left"].intensity,
                    (high, high),
                );
            }
        }

        engine
            .apply(3.4, &Command::SetSpeed { multiplier: 0.0 })
            .unwrap();
        let held_motion = engine.render_at(3.4).unwrap();
        let next_beat = engine.render_at(3.8).unwrap();
        within(
            held_motion.fixture_properties_before_strobe["wash_left"].intensity,
            (low, low),
        );
        within(
            next_beat.fixture_properties_before_strobe["wash_left"].intensity,
            (high, high),
        );
        assert_eq!(
            held_motion.fixture_properties_before_strobe["spot_left"].pan,
            next_beat.fixture_properties_before_strobe["spot_left"].pan
        );
    }
}
