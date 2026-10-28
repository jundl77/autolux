//! Fixture vocabulary: the software strobe gate, wheel colour blends, gobo slots and darkness.

mod support;

use std::fs;

use serde_json::{Value, json};
use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Transition};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

// A binary-exact frame time keeps the accumulated gate phase exact, so counts are exact too.
const FRAMES_PER_SECOND: usize = 32;
const FRAME_SECONDS: f64 = 1.0 / FRAMES_PER_SECOND as f64;

fn engine(rig: FixtureRig, scripts: &[(&str, &str)]) -> Engine {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    for (name, source) in scripts {
        fs::write(
            directory.path().join("test").join(format!("{name}.lua")),
            source,
        )
        .unwrap();
    }
    let catalog = AutoloopCatalog::load(directory.path()).unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: 0,
                bar_phase: None,
                beats_per_bar: 4,
                bpm: 120.0,
            },
        )
        .unwrap();
    engine
}

fn select(engine: &mut Engine, at: f64, name: &str, transition: Transition) {
    engine
        .apply(
            at,
            &Command::Select {
                bank: "test".into(),
                autoloop: name.into(),
                transition,
                palette: "techno".into(),
            },
        )
        .unwrap();
}

fn rig(spec: Value) -> FixtureRig {
    support::rig(spec)
}

// 1 -> 10 Hz over 16 beats (8 s at 120 bpm), then held at 10 Hz.
const STROBE_SWEEP: &str = r#"return {render=function(ctx)
    local rate = 1 + 9 * math.min(1, ctx.autoloop_elapsed_beats / 16)
    return sf.scene {sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(1, 1, 1)),
        sf.strobe(sf.all(), rate)} end}"#;

/// Gate openings (dark to lit) in each second-long window of frames.
fn openings_per_second(engine: &mut Engine, seconds: usize) -> Vec<usize> {
    let lit: Vec<bool> = (0..seconds * FRAMES_PER_SECOND)
        .map(|frame| {
            let rendered = engine.render_at(frame as f64 * FRAME_SECONDS).unwrap();
            rendered.dmx_frames_by_universe[&0][0] > 0
        })
        .collect();
    let openings: Vec<bool> = lit.windows(2).map(|pair| !pair[0] && pair[1]).collect();
    openings
        .windows(FRAMES_PER_SECOND)
        .map(|window| window.iter().filter(|&&opened| opened).count())
        .collect()
}

#[test]
fn a_strobe_rate_sweep_never_gates_faster_than_the_rate_cap() {
    for cap in [10.0, 4.0] {
        let mut engine = engine(
            rig(json!({"fixtures": [
                {"fixture_uuid": "dimmer", "profile": "generic_dimmer", "universe": 0, "address": 1}]})),
            &[("sweep", STROBE_SWEEP)],
        );
        engine
            .apply(0.0, &Command::SetStrobeMaxRateHz { hz: cap })
            .unwrap();
        select(&mut engine, 0.0, "sweep", Transition::Cut);
        let counts = openings_per_second(&mut engine, 12);
        let busiest = counts.iter().copied().max().unwrap();
        // The held rate fills a whole second with exactly cap cycles.
        assert_eq!(busiest, cap as usize, "{counts:?}");
        assert_eq!(*counts.last().unwrap(), cap as usize, "{counts:?}");
    }
}

#[test]
fn a_strobe_burst_stops_gating_at_the_burst_cap() {
    let mut engine = engine(
        rig(json!({"fixtures": [
            {"fixture_uuid": "dimmer", "profile": "generic_dimmer", "universe": 0, "address": 1}]})),
        &[("sweep", STROBE_SWEEP)],
    );
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 2.0 })
        .unwrap();
    select(&mut engine, 0.0, "sweep", Transition::Cut);
    let counts = openings_per_second(&mut engine, 4);
    assert!(counts[0] > 0, "{counts:?}");
    assert!(
        counts[2 * FRAMES_PER_SECOND..]
            .iter()
            .all(|&count| count == 0),
        "{counts:?}"
    );
}

/// A spec's rig with synthetic compiled test profiles added under the names its fixtures use.
fn rig_with(spec: Value, profiles: &[(&str, wire::FixtureProfile)]) -> FixtureRig {
    let mut definition = support::definition(&spec);
    for (name, profile) in profiles {
        definition.profiles.push(wire::FixtureProfile {
            profile_id: (*name).into(),
            ..profile.clone()
        });
    }
    FixtureRig::from_wire(&definition).unwrap()
}

fn solid(colour: &str) -> String {
    format!(
        "return {{render=function(ctx) return sf.scene {{sf.dimmer(sf.all(), 1), \
         sf.colour(sf.all(), sf.rgb({colour}))}} end}}"
    )
}

#[test]
fn a_wheel_only_cell_changes_slot_half_way_through_a_fade_while_mixing_cells_blend() {
    let rig = rig_with(
        json!({"fixtures": [
            {"fixture_uuid": "wheel", "profile": "wheel", "universe": 0, "address": 1},
            {"fixture_uuid": "mixer", "profile": "generic_rgb", "universe": 0, "address": 4}]}),
        &[(
            "wheel",
            support::compiled_profile("wheel_spinner#3ch", "wheel"),
        )],
    );
    let (red, blue) = (solid("1, 0, 0"), solid("0, 0, 1"));
    let mut engine = engine(rig, &[("red", &red), ("blue", &blue)]);
    select(&mut engine, 0.0, "red", Transition::Cut);
    engine.render_at(0.0).unwrap();
    // 120 bpm: a 4-beat crossfade from 1 s reaches half-way at 2 s.
    select(
        &mut engine,
        1.0,
        "blue",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    // wheel_spinner's Color1 slots: Red from 64, Blue from 192.
    for (seconds, wheel_dmx, wheel_red, mixer_red) in [
        (1.5, 64, 1.0, 0.75),
        (1.99, 64, 1.0, 0.505),
        (2.0, 192, 0.0, 0.5),
        (2.5, 192, 0.0, 0.25),
    ] {
        let frame = engine.render_at(seconds).unwrap();
        let states = &frame.fixture_properties_before_strobe;
        assert_eq!(frame.dmx_frames_by_universe[&0][0], wheel_dmx, "{seconds}");
        assert_eq!(states["wheel"].colour.red, wheel_red, "{seconds}");
        assert!(
            (states["mixer"].colour.red - mixer_red).abs() < 1e-9,
            "{seconds}: {}",
            states["mixer"].colour.red
        );
    }
}
