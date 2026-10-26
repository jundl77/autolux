use std::fs;
use std::path::PathBuf;

use starfire::engine::{Engine, RenderedFrame};
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Rgb, Transition};
use starfire::protocol::wire;
use starfire::replay::{Replay, dmx_digest, render_replay};
use starfire::script::AutoloopCatalog;

const FLOAT_TOLERANCE: f64 = 1e-9;
const ROOM_FIXTURE_COUNT: usize = 14;
const REPLAY_FRAME_RATE_HZ: f64 = 40.0;
const REPLAY_BPM: f64 = 120.0;
// Replays hold 120 BPM until 64 s, so loop periodicity is checked before then.
const STEADY_TEMPO_SECONDS: f64 = 64.0;
const SECONDS_PER_BEAT: f64 = 60.0 / REPLAY_BPM;

struct Look {
    bank: &'static str,
    autoloop: &'static str,
    /// `None` when the look depends on time since selection, not only on bar phase.
    loop_beats: Option<f64>,
}

const LOOKS: [Look; 4] = [
    Look {
        bank: "atmospheric",
        autoloop: "cold_breathe",
        loop_beats: Some(16.0),
    },
    Look {
        bank: "breakdown",
        autoloop: "wide_space",
        loop_beats: Some(16.0),
    },
    Look {
        bank: "buildup",
        autoloop: "ramp",
        loop_beats: None,
    },
    Look {
        bank: "drop",
        autoloop: "kick",
        loop_beats: Some(8.0),
    },
];

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn room() -> FixtureRig {
    FixtureRig::load(&root().join("../rigs/room.mvr")).unwrap()
}

fn catalog() -> AutoloopCatalog {
    AutoloopCatalog::load(&root().join("../autoloops")).unwrap()
}

/// Engine at 120 BPM with `autoloop` selected while the beat clock reads `select_beat`.
fn engine(bank: &str, autoloop: &str, select_beat: f64) -> Engine {
    let mut engine = Engine::new(room(), catalog()).unwrap();
    for command in [
        Command::Beat {
            position_beats: select_beat,
            bpm: REPLAY_BPM,
        },
        Command::Select {
            bank: bank.into(),
            autoloop: autoloop.into(),
            transition: Transition::Cut,
        },
    ] {
        engine.apply(0.0, &command).unwrap();
    }
    engine
}

fn wash_level(engine: &mut Engine, beats_after_select: f64) -> f64 {
    let frame = engine
        .render_at(beats_after_select * SECONDS_PER_BEAT)
        .unwrap();
    frame.fixture_properties_before_strobe["wash_left"].intensity
}

fn assert_semantics(frames: &[RenderedFrame], look: &Look) {
    for frame in frames {
        assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
        assert_eq!(frame.active_bank.as_deref(), Some(look.bank));
        assert_eq!(frame.active_autoloop.as_deref(), Some(look.autoloop));
        assert_eq!(
            frame.fixture_properties_before_strobe.len(),
            ROOM_FIXTURE_COUNT
        );
        for (id, state) in &frame.fixture_properties_before_strobe {
            assert!(state.strobe_hz == 0.0, "{} strobes {id}", look.bank);
        }
    }

    // Bar-locked looks repeat on the beat grid, so reselection never restarts a phrase.
    let Some(loop_beats) = look.loop_beats else {
        return;
    };
    let period_frames = (loop_beats * SECONDS_PER_BEAT * REPLAY_FRAME_RATE_HZ) as usize;
    let steady_frames = (STEADY_TEMPO_SECONDS * REPLAY_FRAME_RATE_HZ) as usize;
    for index in 0..steady_frames - period_frames {
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

fn check_replay(look: &Look) {
    let replay = Replay::load(&root().join(format!("tests/replays/{}.json", look.bank))).unwrap();
    let first = render_replay(&replay, room(), catalog()).unwrap();
    // 90 s at 120-150 BPM covers well over 16 bars.
    assert_eq!(first.frames.len(), 3601);
    assert_semantics(&first.frames, look);

    let second = render_replay(&replay, room(), catalog()).unwrap();
    let digest = dmx_digest(&first.frames);
    assert_eq!(digest, dmx_digest(&second.frames));

    // Duplicate selections cannot restart the current autoloop.
    let mut without_duplicate = replay.clone();
    without_duplicate.events.retain(|event| {
        event.at_seconds == 0.0 || !matches!(event.command, Command::Select { .. })
    });
    let continued = render_replay(&without_duplicate, room(), catalog()).unwrap();
    assert_eq!(digest, dmx_digest(&continued.frames));

    // Frame rate changes sampling density, never the authored effect at a given time.
    let mut slower = replay.clone();
    slower.frame_rate_hz = 20;
    let slower = render_replay(&slower, room(), catalog()).unwrap();
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
fn atmospheric_cold_breathe_replay() {
    check_replay(&LOOKS[0]);
}

#[test]
fn breakdown_wide_space_replay() {
    check_replay(&LOOKS[1]);
}

#[test]
fn buildup_ramp_replay() {
    check_replay(&LOOKS[2]);
}

#[test]
fn drop_kick_replay() {
    check_replay(&LOOKS[3]);
}

#[test]
fn drop_washes_hit_on_every_beat() {
    let mut engine = engine("drop", "kick", 0.0);
    for beat in 0..16 {
        let hit = wash_level(&mut engine, beat as f64 + 0.1);
        let offbeat = wash_level(&mut engine, beat as f64 + 0.5);
        assert!(hit > offbeat, "beat {beat}: {hit} <= {offbeat}");
    }
}

#[test]
fn breakdown_washes_drift_without_beat_pulses_or_strobe() {
    let mut engine = engine("breakdown", "wide_space", 0.0);
    let mut levels = Vec::new();
    for step in 0..=128 {
        let frame = engine
            .render_at(step as f64 * 0.5 * SECONDS_PER_BEAT)
            .unwrap();
        assert!(
            frame
                .fixture_properties_before_strobe
                .values()
                .all(|state| state.strobe_hz == 0.0)
        );
        levels.push(frame.fixture_properties_before_strobe["wash_left"].intensity);
    }
    // The fade is piecewise linear between beats, so a beat pulse would bend mid-beat.
    for beat in levels.windows(3).step_by(2) {
        assert!((beat[1] - (beat[0] + beat[2]) / 2.0).abs() <= FLOAT_TOLERANCE);
    }
}

#[test]
fn buildup_peaks_rise_across_the_ramp() {
    let mut engine = engine("buildup", "ramp", 0.0);
    let mut bar_peak = |bar: f64| {
        (0..80)
            .map(|step| wash_level(&mut engine, bar * 4.0 + step as f64 * 0.05))
            .fold(0.0, f64::max)
    };
    let early = bar_peak(2.0);
    let late = bar_peak(30.0);
    assert!(late > early, "bar 30 peak {late} <= bar 2 peak {early}");
}

#[test]
fn buildup_never_strobes_even_when_loud() {
    let mut engine = engine("buildup", "ramp", 0.0);
    engine
        .apply(0.0, &Command::SetNormalizedRmsLevel { level: 0.9 })
        .unwrap();
    // 48 bars sampled every quarter beat spans the ramp and well past its plateau.
    for step in 0..=48 * 4 * 4 {
        let frame = engine
            .render_at(step as f64 * 0.25 * SECONDS_PER_BEAT)
            .unwrap();
        for (id, state) in &frame.fixture_properties_before_strobe {
            assert_eq!(state.strobe_hz, 0.0, "{id} strobes at step {step}");
        }
    }
}

#[test]
fn selecting_mid_bar_keeps_the_musical_phrase() {
    for look in LOOKS.iter().filter(|look| look.loop_beats.is_some()) {
        let mut on_the_one = engine(look.bank, look.autoloop, 0.0);
        let mut mid_bar = engine(look.bank, look.autoloop, 2.25);
        for beat in [3.0, 3.1, 4.0, 6.5, 9.0] {
            let expected = on_the_one.render_at(beat * SECONDS_PER_BEAT).unwrap();
            let actual = mid_bar.render_at((beat - 2.25) * SECONDS_PER_BEAT).unwrap();
            assert_eq!(
                expected.dmx_frames_by_universe, actual.dmx_frames_by_universe,
                "{} restarted on selection",
                look.bank
            );
        }
    }
}

#[test]
fn measured_energy_scales_levels_and_reach_but_not_colour_or_structure() {
    for look in &LOOKS {
        let mut engine = engine(look.bank, look.autoloop, 4.0);
        engine
            .apply(0.0, &Command::SetNormalizedRmsLevel { level: 1.0 })
            .unwrap();
        let strong = engine.render_at(0.25).unwrap();
        engine
            .apply(0.25, &Command::SetNormalizedRmsLevel { level: 0.0 })
            .unwrap();
        let quiet = engine.render_at(0.25).unwrap();
        for (id, strong) in &strong.fixture_properties_before_strobe {
            let quiet = &quiet.fixture_properties_before_strobe[id];
            assert!(quiet.intensity <= strong.intensity + FLOAT_TOLERANCE);
            assert!((quiet.pan - 0.5).abs() <= (strong.pan - 0.5).abs() + FLOAT_TOLERANCE);
            assert_eq!(strong.colour, quiet.colour);
            assert_eq!(strong.strobe_hz, quiet.strobe_hz);
        }
        assert_ne!(strong.dmx_frames_by_universe, quiet.dmx_frames_by_universe);
    }
}

#[test]
fn silence_drops_wash_to_a_tenth_of_loud_level() {
    for look in &LOOKS {
        let mean_wash = |level: f64| {
            let mut engine = engine(look.bank, look.autoloop, 0.0);
            engine
                .apply(0.0, &Command::SetNormalizedRmsLevel { level })
                .unwrap();
            // 32 bars at quarter beats covers every loop period and the buildup ramp.
            let steps = 32 * 4 * 4;
            (0..steps)
                .map(|step| wash_level(&mut engine, step as f64 * 0.25))
                .sum::<f64>()
                / steps as f64
        };
        let (silent, loud) = (mean_wash(0.0), mean_wash(0.9));
        assert!(
            silent < 0.15 * loud,
            "{}: silent wash {silent} vs loud {loud}",
            look.bank
        );
    }
}

#[test]
fn loudness_gain_floors_silence_and_saturates_when_loud() {
    let source = r#"
        local function near(a, b) return math.abs(a - b) < 1e-12 end
        assert(near(sf.loudness_gain(0), 0.08))
        assert(near(sf.loudness_gain(0.8), 1) and near(sf.loudness_gain(1), 1))
        assert(near(sf.loudness_gain(nil), sf.loudness_gain(0.6)))
        assert(sf.loudness_gain(0.2) < sf.loudness_gain(0.4))
        return { render = function(ctx) return {} end }
    "#;
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "gain".into(),
        bank: "test".into(),
        lua_source: source.into(),
    }])
    .unwrap();
    let mut engine = Engine::new(room(), catalog).unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "gain".into(),
                transition: Transition::Cut,
            },
        )
        .unwrap();
    let frame = engine.render_at(0.0).unwrap();
    assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
}

#[test]
fn palettes_swap_by_name_and_grid_helpers_follow_the_bar() {
    let source = r#"
        assert(sf.beat_in_bar(5.5) == 1.5 and sf.bar(5.5) == 1)
        assert(sf.phrase_phase(8, 4) == 0.5 and sf.phrase_phase(16, 4) == 0)
        assert(sf.on_beat(3, 0.5) == 1 and sf.on_beat(3.5, 0.5) == 0)
        return { render = function(ctx)
            return { sf.colour(sf.all(), sf.palette("house").cold), sf.dimmer(sf.all(), 1) }
        end }
    "#;
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "house".into(),
        bank: "test".into(),
        lua_source: source.into(),
    }])
    .unwrap();
    let mut engine = Engine::new(room(), catalog).unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "house".into(),
                transition: Transition::Cut,
            },
        )
        .unwrap();
    let frame = engine.render_at(0.0).unwrap();
    assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
    assert_eq!(
        frame.fixture_properties_before_strobe["cf805_pixel_1"].colour,
        Rgb {
            red: 0.0,
            green: 0.85,
            blue: 1.0
        }
    );
}

#[test]
fn every_registered_autoloop_has_an_executable_replay_and_expected_dmx() {
    let catalog = catalog();
    for entry in catalog.list() {
        let replay = Replay::load(&root().join(format!("tests/replays/{}.json", entry.bank)))
            .expect("every authored autoloop needs a replay fixture");
        assert!(
            replay.events.iter().any(|event| {
                matches!(&event.command, Command::Select { bank, autoloop, .. }
                if bank == &entry.bank && autoloop == &entry.autoloop)
            }),
            "replay does not exercise {}/{}",
            entry.bank,
            entry.autoloop
        );
        let result = render_replay(&replay, room(), catalog.clone()).unwrap();
        let expected =
            fs::read_to_string(root().join(format!("tests/golden/{}.sha256", entry.bank)))
                .expect("every authored autoloop needs a reviewed full DMX digest");
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
fn the_same_autoloops_work_on_a_different_rig_through_named_groups() {
    let catalog = catalog();
    let rig = FixtureRig::load(&root().join("../rigs/generic.mvr")).unwrap();
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
