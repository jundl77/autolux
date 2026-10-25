use std::fs;
use std::path::PathBuf;

use starfire::engine::{Engine, RenderedFrame};
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Transition};
use starfire::replay::{Replay, dmx_digest, render_replay};
use starfire::script::SequenceCatalog;

const FLOAT_TOLERANCE: f64 = 1e-10;
const ROOM_FIXTURE_COUNT: usize = 14;
const LOOP_BEATS: f64 = 64.0;

struct Look {
    bank: &'static str,
    sequence: &'static str,
    wash_range: (f64, f64),
    mover_range: (f64, f64),
    pan_range: (f64, f64),
    tilt_range: (f64, f64),
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
    for frame in frames {
        assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
        assert_eq!(frame.active_bank.as_deref(), Some(look.bank));
        assert_eq!(frame.active_sequence.as_deref(), Some(look.sequence));
        assert_eq!(
            frame.fixture_properties_before_strobe.len(),
            ROOM_FIXTURE_COUNT
        );
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
            if mover {
                within(state.colour.blue, (1.0, 1.0));
                within(state.colour.red, (0.0, 0.0));
                within(state.colour.green, (0.0, 0.0));
                within(state.pan, look.pan_range);
                within(state.tilt, look.tilt_range);
            } else {
                // Authored RGB palette stays within saturated blue/violet hues.
                within(state.colour.blue, (230.0 / 255.0, 1.0));
                within(state.colour.green, (0.0, 57.0 / 255.0));
                within(state.colour.red, (21.0 / 255.0, 119.0 / 255.0));
            }
        }
        let dmx = &frame.dmx_frames_by_universe[&0];
        for start in [0, 12] {
            assert_eq!(dmx[start + 5], 111, "mover wheel must stay blue");
            assert_eq!(dmx[start + 6], 7, "hardware shutter must stay open");
            assert_eq!(&dmx[start + 8..start + 12], &[0; 4]);
        }
        assert!(dmx[24..64].iter().chain(&dmx[120..]).all(|byte| *byte == 0));
    }

    // Equivalent loop phases have identical semantics; floating-point roundoff at
    // an exact DMX half-byte can choose neighboring bytes after modulo reduction.
    let period_frames = (LOOP_BEATS / 2.0 * 40.0) as usize;
    for index in 0..period_frames {
        for (id, first) in &frames[index].fixture_properties_before_strobe {
            let repeated = &frames[index + period_frames].fixture_properties_before_strobe[id];
            for (left, right) in [
                (first.intensity, repeated.intensity),
                (first.pan, repeated.pan),
                (first.tilt, repeated.tilt),
                (first.colour.red, repeated.colour.red),
                (first.colour.green, repeated.colour.green),
                (first.colour.blue, repeated.colour.blue),
            ] {
                assert!(
                    (left - right).abs() <= FLOAT_TOLERANCE,
                    "{id} changed after one loop at frame {index}"
                );
            }
        }
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
        wash_range: (63.0 / 255.0, 63.0 / 255.0),
        mover_range: (63.0 / 255.0, 245.0 / 255.0),
        pan_range: (0.23, 0.43),
        tilt_range: (0.68, 0.97),
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
        wash_range: (37.0 / 255.0, 37.0 / 255.0),
        mover_range: (37.0 / 255.0, 37.0 / 255.0),
        pan_range: (32767.0 / 65535.0, 32767.0 / 65535.0),
        tilt_range: (32767.0 / 65535.0, 32767.0 / 65535.0),
    });
}

#[test]
fn buildup_current_replay() {
    check_replay(Look {
        bank: "buildup",
        sequence: "current",
        wash_range: (0.0, 144.0 / 255.0),
        mover_range: (0.0, 144.0 / 255.0),
        pan_range: (0.40, 0.60),
        tilt_range: (0.30, 0.70),
    });
}

#[test]
fn drop_blue_impact_replay() {
    check_replay(Look {
        bank: "drop",
        sequence: "blue_impact",
        wash_range: (124.0 / 255.0, 245.0 / 255.0),
        mover_range: (0.0, 245.0 / 255.0),
        pan_range: (0.23, 0.43),
        tilt_range: (0.68, 0.97),
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
                    .all(|state| (0.0..=1.0).contains(&state.intensity) && state.strobe_hz == 0.0)
            );
        }
    }
}

#[test]
fn beat_corrections_change_pulses_without_restarting_choreography() {
    let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
    let rig = FixtureRig::load(&root().join("rigs/room.json")).unwrap();
    for (bank, sequence, initial_level, corrected_level) in [
        ("buildup", "current", 144.0 / 255.0, 72.0_f64 / 255.0),
        ("drop", "blue_impact", 198.0 / 255.0, 124.0 / 255.0),
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
        // A half-beat selection offset must not shift subsequent music pulses.
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
        let before = engine.render_at(2.0).unwrap();
        within(
            before.fixture_properties_before_strobe["wash_left"].intensity,
            (initial_level, initial_level),
        );
        engine
            .apply(
                2.0,
                &Command::Beat {
                    position_beats: if bank == "buildup" { 4.75 } else { 6.0 },
                    bpm: 120.0,
                },
            )
            .unwrap();
        let corrected = engine.render_at(2.0).unwrap();
        assert_eq!(
            corrected.dmx_frames_by_universe[&0][68],
            (corrected_level * 255.0).round() as u8
        );
        for (id, previous) in before.fixture_properties_before_strobe {
            let current = &corrected.fixture_properties_before_strobe[&id];
            assert_eq!(previous.pan, current.pan);
            assert_eq!(previous.tilt, current.tilt);
            assert_eq!(previous.colour, current.colour);
        }

        // Pausing choreography freezes position/colour; authoritative beat pulses continue.
        engine
            .apply(2.0, &Command::SetSpeed { multiplier: 0.0 })
            .unwrap();
        let frozen = engine.render_at(2.0).unwrap();
        let later = engine.render_at(2.375).unwrap();
        assert_ne!(
            frozen.fixture_properties_before_strobe["wash_left"].intensity,
            later.fixture_properties_before_strobe["wash_left"].intensity
        );
        for (id, previous) in frozen.fixture_properties_before_strobe {
            let current = &later.fixture_properties_before_strobe[&id];
            assert_eq!(previous.pan, current.pan);
            assert_eq!(previous.tilt, current.tilt);
            assert_eq!(previous.colour, current.colour);
        }
    }
}

#[test]
fn drop_pulses_survive_animation_freeze_and_selection_across_global_phrase_boundaries() {
    let catalog = SequenceCatalog::load(&root().join("sequences")).unwrap();
    let rig = FixtureRig::load(&root().join("rigs/room.json")).unwrap();
    let mut pulse_traces = Vec::new();
    for selection_beat in [2.0, 2.25, 62.0, 62.25] {
        let make_engine = |speed| {
            let mut engine = Engine::new(rig.clone(), catalog.clone()).unwrap();
            engine
                .apply(
                    0.0,
                    &Command::Beat {
                        position_beats: selection_beat,
                        bpm: 120.0,
                    },
                )
                .unwrap();
            engine
                .apply(
                    0.0,
                    &Command::Select {
                        bank: "drop".into(),
                        sequence: "blue_impact".into(),
                        transition: Transition::Cut,
                    },
                )
                .unwrap();
            engine
                .apply(0.0, &Command::SetSpeed { multiplier: speed })
                .unwrap();
            engine
        };
        let mut running = make_engine(1.0);
        let mut frozen = make_engine(0.0);
        let initial = frozen.render_at(0.0).unwrap();
        let initial_position = &initial.fixture_properties_before_strobe["spot_left"];
        let mut observed_levels = Vec::new();
        for seconds in [0.0, 0.5, 1.0, 1.25, 2.0, 2.5, 3.0] {
            if seconds == 2.5 {
                let correction = Command::Beat {
                    position_beats: selection_beat + 5.5,
                    bpm: 120.0,
                };
                running.apply(seconds, &correction).unwrap();
                frozen.apply(seconds, &correction).unwrap();
            }
            let expected = running.render_at(seconds).unwrap();
            let actual = frozen.render_at(seconds).unwrap();
            let dimmer = actual.dmx_frames_by_universe[&0][7];
            assert_eq!(
                dimmer, expected.dmx_frames_by_universe[&0][7],
                "speed changed pulse at {selection_beat} + {seconds}s"
            );
            let position = &actual.fixture_properties_before_strobe["spot_left"];
            assert_eq!(position.pan, initial_position.pan);
            assert_eq!(position.tilt, initial_position.tilt);
            if seconds == 1.25 {
                assert!(
                    dimmer > 160,
                    "global beat64 must not add another entry hold"
                );
            }
            observed_levels.push(dimmer);
        }
        assert!(observed_levels.windows(2).any(|pair| pair[0] != pair[1]));
        pulse_traces.push(observed_levels);
    }
    assert_eq!(
        pulse_traces[0], pulse_traces[2],
        "bar-aligned selection must own its pulse phrase"
    );
    assert_eq!(
        pulse_traces[1], pulse_traces[3],
        "fractional selection must keep the musical bar phase"
    );
}
