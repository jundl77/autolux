use std::fs;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Rgb, Target, Transition,
};
use starfire::script::AutoloopCatalog;

const RIG: &str = r#"{"schema_version":1,"fixtures":[
    {"id":"left","profile":"generic_rgb","universe":0,"address":1},
    {"id":"right","profile":"generic_rgb","universe":0,"address":4}],
    "groups":{"wash":["left","right"]}}"#;
const CONSTANT: &str =
    "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.5)} end}";
const LOOP: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), (ctx.autoloop_speed_scaled_elapsed_beats % 16) / 16),
    sf.pan_tilt(sf.all(), (ctx.autoloop_elapsed_beats % 16) / 16, 0.3),
    sf.colour(sf.all(), sf.rgb(0, 0, 1))} end}"#;
const RED: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 0.2), sf.colour(sf.all(), sf.rgb(1, 0, 0)),
    sf.pan_tilt(sf.all(), 0.2, 0.4), sf.strobe(sf.all(), 2)} end}"#;
const BLUE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 0.8), sf.colour(sf.all(), sf.rgb(0, 0, 1)),
    sf.pan_tilt(sf.all(), 0.8, 0.6), sf.strobe(sf.all(), 8)} end}"#;

fn engine(scripts: &[(&str, &str)]) -> Engine {
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
    let mut engine = Engine::new(FixtureRig::from_json(RIG).unwrap(), catalog).unwrap();
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
            },
        )
        .unwrap();
}

fn lighting_override(id: &str, values: PropertyValues) -> LightingOverride {
    LightingOverride {
        id: id.into(),
        target: Target::All,
        values,
        intensity_multiplier: None,
        lifetime: OverrideLifetime::UntilCleared,
        priority: 0,
    }
}

fn set_override(engine: &mut Engine, at: f64, value: LightingOverride) {
    engine.apply(at, &Command::SetOverride { value }).unwrap();
}

fn near(actual: f64, expected: f64) {
    assert!((actual - expected).abs() < 1e-12, "{actual} != {expected}");
}

#[test]
fn duplicate_selection_never_restarts_a_loop_or_creates_a_transition() {
    let mut engine = engine(&[("loop", LOOP)]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.125,
    );
    select(
        &mut engine,
        2.0,
        "loop",
        Transition::Crossfade {
            duration_beats: 8.0,
        },
    );
    near(
        engine
            .render_at(2.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.25,
    );
    for second in 3..=60 {
        let frame = engine.render_at(second as f64).unwrap();
        near(
            frame.fixture_properties_before_strobe["left"].intensity,
            ((second * 2) % 16) as f64 / 16.0,
        );
        assert_eq!(frame.active_autoloop.as_deref(), Some("loop"));
    }
}

#[test]
fn property_override_preserves_baseline_motion_and_other_fixtures() {
    let mut engine = engine(&[("loop", LOOP)]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    let red = Rgb {
        red: 1.0,
        green: 0.0,
        blue: 0.0,
    };
    let mut colour = lighting_override(
        "held_red",
        PropertyValues {
            colour: Some(red),
            ..Default::default()
        },
    );
    colour.target = Target::Fixture("left".into());
    set_override(&mut engine, 1.0, colour);
    let frame = engine.render_at(2.0).unwrap();
    assert_eq!(frame.fixture_properties_before_strobe["left"].colour, red);
    near(frame.fixture_properties_before_strobe["left"].pan, 0.25);
    near(
        frame.fixture_properties_before_strobe["left"].intensity,
        0.25,
    );
    assert_eq!(
        frame.fixture_properties_before_strobe["right"].colour.blue,
        1.0
    );
    engine
        .apply(
            3.0,
            &Command::ClearOverride {
                id: "held_red".into(),
            },
        )
        .unwrap();
    let restored = engine.render_at(3.0).unwrap();
    assert_eq!(
        restored.fixture_properties_before_strobe["left"],
        restored.fixture_properties_before_strobe["right"]
    );
    near(restored.fixture_properties_before_strobe["left"].pan, 0.375);
}

#[test]
fn override_priority_then_latest_set_wins_and_held_values_survive_selection() {
    let mut engine = engine(&[("first", CONSTANT), ("second", LOOP)]);
    select(&mut engine, 0.0, "first", Transition::Cut);
    let mut priority = lighting_override(
        "priority",
        PropertyValues {
            intensity: Some(0.7),
            ..Default::default()
        },
    );
    priority.priority = 5;
    set_override(&mut engine, 0.0, priority);
    set_override(
        &mut engine,
        1.0,
        lighting_override(
            "recent",
            PropertyValues {
                intensity: Some(0.3),
                ..Default::default()
            },
        ),
    );
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.7,
    );
    let mut equal = lighting_override(
        "equal",
        PropertyValues {
            intensity: Some(0.9),
            ..Default::default()
        },
    );
    equal.priority = 5;
    set_override(&mut engine, 2.0, equal);
    select(&mut engine, 2.0, "second", Transition::Cut);
    near(
        engine
            .render_at(2.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.9,
    );
    engine
        .apply(3.0, &Command::ClearOverride { id: "equal".into() })
        .unwrap();
    near(
        engine
            .render_at(3.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.7,
    );
}

#[test]
fn beat_and_second_expiry_reveal_current_baseline_at_the_exact_deadline() {
    let mut engine = engine(&[("loop", LOOP)]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    let mut beats = lighting_override(
        "beats",
        PropertyValues {
            intensity: Some(0.9),
            ..Default::default()
        },
    );
    beats.target = Target::Fixture("left".into());
    beats.lifetime = OverrideLifetime::Beats {
        duration_beats: 4.0,
    };
    let mut seconds = beats.clone();
    seconds.id = "seconds".into();
    seconds.target = Target::Fixture("right".into());
    seconds.lifetime = OverrideLifetime::Seconds {
        duration_seconds: 3.0,
    };
    set_override(&mut engine, 0.0, beats);
    set_override(&mut engine, 0.0, seconds);
    near(
        engine
            .render_at(1.999)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.9,
    );
    let beat_expired = engine.render_at(2.0).unwrap();
    near(
        beat_expired.fixture_properties_before_strobe["left"].intensity,
        0.25,
    );
    near(
        beat_expired.fixture_properties_before_strobe["right"].intensity,
        0.9,
    );
    let both_expired = engine.render_at(3.0).unwrap();
    near(
        both_expired.fixture_properties_before_strobe["right"].intensity,
        0.375,
    );
}

#[test]
fn each_multiplier_is_clamped_before_the_next_effect_and_master() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    for (id, multiplier) in [("boost1", 8.0), ("boost2", 8.0), ("attenuate", 0.01)] {
        let mut value = lighting_override(id, PropertyValues::default());
        value.intensity_multiplier = Some(multiplier);
        set_override(&mut engine, 0.0, value);
    }
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity: 0.5,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.005,
    );
    engine
        .apply(
            0.0,
            &Command::ClearOverride {
                id: "attenuate".into(),
            },
        )
        .unwrap();
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.5,
    );
    engine
        .apply(0.0, &Command::SetBlackout { enabled: true })
        .unwrap();
    let dark = engine.render_at(0.0).unwrap();
    assert!(
        dark.dmx_frames_by_universe[&0]
            .iter()
            .all(|byte| *byte == 0)
    );
    engine
        .apply(1.0, &Command::SetBlackout { enabled: false })
        .unwrap();
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.5,
    );
}

#[test]
fn speed_changes_preserve_phase_and_do_not_depend_on_render_cadence() {
    let mut sparse = engine(&[("loop", LOOP)]);
    let mut dense = engine(&[("loop", LOOP)]);
    for value in [&mut sparse, &mut dense] {
        select(value, 0.0, "loop", Transition::Cut);
    }
    for index in 0..100 {
        dense.render_at(index as f64 / 100.0).unwrap();
    }
    let before = sparse.render_at(1.0).unwrap();
    for value in [&mut sparse, &mut dense] {
        value
            .apply(1.0, &Command::SetSpeed { multiplier: 2.0 })
            .unwrap();
    }
    assert_eq!(
        before.fixture_properties_before_strobe,
        sparse
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe
    );
    for index in 100..200 {
        dense.render_at(index as f64 / 100.0).unwrap();
    }
    let expected = sparse.render_at(2.0).unwrap();
    assert_eq!(expected, dense.render_at(2.0).unwrap());
    near(
        expected.fixture_properties_before_strobe["left"].intensity,
        0.375,
    );
    near(expected.fixture_properties_before_strobe["left"].pan, 0.25);
    assert_eq!(expected.beat_position, 4.0);
}

#[test]
fn crossfade_blends_semantic_properties_and_switches_shutter_at_completion() {
    let mut engine = engine(&[("red", RED), ("blue", BLUE)]);
    select(&mut engine, 0.0, "red", Transition::Cut);
    select(
        &mut engine,
        1.0,
        "blue",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    let start = engine.render_at(1.0).unwrap();
    near(
        start.fixture_properties_before_strobe["left"].intensity,
        0.2,
    );
    let middle = engine.render_at(2.0).unwrap();
    let state = &middle.fixture_properties_before_strobe["left"];
    near(state.intensity, 0.5);
    near(state.colour.red, 0.5);
    near(state.colour.blue, 0.5);
    near(state.pan, 0.5);
    near(state.tilt, 0.5);
    near(state.strobe_hz, 2.0);
    let end = engine.render_at(3.0).unwrap();
    near(end.fixture_properties_before_strobe["left"].intensity, 0.8);
    near(end.fixture_properties_before_strobe["left"].strobe_hz, 8.0);
}

#[test]
fn outgoing_loop_keeps_animating_and_interrupted_fade_keeps_current_blend() {
    let mut engine = engine(&[("loop", LOOP), ("constant", CONSTANT), ("blue", BLUE)]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    select(
        &mut engine,
        1.0,
        "constant",
        Transition::Crossfade {
            duration_beats: 8.0,
        },
    );
    let middle = engine.render_at(3.0).unwrap();
    near(
        middle.fixture_properties_before_strobe["left"].intensity,
        (0.375 + 0.5) / 2.0,
    );
    select(
        &mut engine,
        3.0,
        "blue",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    assert_eq!(
        middle.fixture_properties_before_strobe,
        engine
            .render_at(3.0)
            .unwrap()
            .fixture_properties_before_strobe
    );
    near(
        engine
            .render_at(4.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        (0.4375 + 0.8) / 2.0,
    );
}

#[test]
fn failed_replacement_load_keeps_running_autoloop() {
    let mut engine = engine(&[("loop", LOOP), ("invalid", "this is not Lua")]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    for name in ["missing", "invalid"] {
        assert!(
            engine
                .apply(
                    1.0,
                    &Command::Select {
                        bank: "test".into(),
                        autoloop: name.into(),
                        transition: Transition::Cut
                    }
                )
                .is_err()
        );
    }
    let continuing = engine.render_at(2.0).unwrap();
    assert_eq!(continuing.active_autoloop.as_deref(), Some("loop"));
    assert!(continuing.script_error.is_none());
    near(
        continuing.fixture_properties_before_strobe["left"].intensity,
        0.25,
    );
}

#[test]
fn runtime_fault_clears_overrides_latches_blackout_and_never_retries_duplicate_selection() {
    let fault = "return {render=function(ctx) if ctx.autoloop_elapsed_beats >= 2 then error('broken autoloop') end return sf.scene {sf.dimmer(sf.all(), 0.7), sf.pan_tilt(sf.all(), 0.8, 0.2)} end}";
    let mut engine = engine(&[("fault", fault), ("constant", CONSTANT)]);
    select(&mut engine, 0.0, "fault", Transition::Cut);
    set_override(
        &mut engine,
        0.0,
        lighting_override(
            "flash",
            PropertyValues {
                intensity: Some(1.0),
                strobe_hz: Some(8.0),
                ..Default::default()
            },
        ),
    );
    let before = engine.render_at(0.0).unwrap();
    let faulted = engine.render_at(1.0).unwrap();
    assert!(
        faulted
            .script_error
            .as_deref()
            .unwrap()
            .contains("broken autoloop")
    );
    near(
        faulted.fixture_properties_before_strobe["left"].intensity,
        0.0,
    );
    near(
        faulted.fixture_properties_before_strobe["left"].strobe_hz,
        0.0,
    );
    near(
        faulted.fixture_properties_before_strobe["left"].pan,
        before.fixture_properties_before_strobe["left"].pan,
    );
    select(&mut engine, 2.0, "fault", Transition::Cut);
    assert!(engine.render_at(2.0).unwrap().script_error.is_some());
    select(&mut engine, 3.0, "constant", Transition::Cut);
    let recovered = engine.render_at(3.0).unwrap();
    assert!(recovered.script_error.is_none());
    near(
        recovered.fixture_properties_before_strobe["left"].intensity,
        0.5,
    );
    near(
        recovered.fixture_properties_before_strobe["left"].strobe_hz,
        0.0,
    );
}

#[test]
fn stop_cancels_autoloop_and_overrides_while_retaining_movement() {
    let mut engine = engine(&[("blue", BLUE)]);
    select(&mut engine, 0.0, "blue", Transition::Cut);
    set_override(
        &mut engine,
        0.0,
        lighting_override(
            "held",
            PropertyValues {
                intensity: Some(1.0),
                ..Default::default()
            },
        ),
    );
    let before = engine.render_at(0.0).unwrap();
    engine.apply(1.0, &Command::Stop).unwrap();
    let stopped = engine.render_at(2.0).unwrap();
    assert!(stopped.active_autoloop.is_none());
    near(
        stopped.fixture_properties_before_strobe["left"].pan,
        before.fixture_properties_before_strobe["left"].pan,
    );
    near(
        stopped.fixture_properties_before_strobe["left"].intensity,
        0.0,
    );
    near(
        stopped.fixture_properties_before_strobe["left"].strobe_hz,
        0.0,
    );
    select(&mut engine, 3.0, "blue", Transition::Cut);
    near(
        engine
            .render_at(3.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.8,
    );
}

#[test]
fn software_strobe_gates_dmx_without_changing_semantic_diagnostics() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    set_override(
        &mut engine,
        0.0,
        lighting_override(
            "strobe",
            PropertyValues {
                strobe_hz: Some(10.0),
                ..Default::default()
            },
        ),
    );
    for index in 0..40 {
        let time = index as f64 / 40.0;
        let expected = if index % 4 < 2 { 128 } else { 0 };
        let frame = engine.render_at(time).unwrap();
        near(
            frame.fixture_properties_before_strobe["left"].strobe_hz,
            10.0,
        );
        near(
            frame.fixture_properties_before_strobe["left"].intensity,
            0.5,
        );
        assert_eq!(frame.dmx_frames_by_universe[&0][0], expected);
    }
}

#[test]
fn invalid_commands_do_not_mutate_the_active_show() {
    let mut engine = engine(&[("loop", LOOP)]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    engine.render_at(1.0).unwrap();
    for time in [-1.0, 0.5, f64::NAN, f64::INFINITY] {
        assert!(engine.render_at(time).is_err());
        assert!(engine.apply(time, &Command::Stop).is_err());
    }
    for value in [f64::NAN, f64::INFINITY, -0.1, 1.1] {
        let invalid = lighting_override(
            "bad",
            PropertyValues {
                intensity: Some(value),
                ..Default::default()
            },
        );
        assert!(
            engine
                .apply(2.0, &Command::SetOverride { value: invalid })
                .is_err()
        );
        assert!(
            engine
                .apply(
                    2.0,
                    &Command::SetMaster {
                        intensity: value,
                        fade_seconds: 0.0
                    }
                )
                .is_err()
        );
    }
    let mut invalid_target = lighting_override(
        "bad_target",
        PropertyValues {
            intensity: Some(1.0),
            ..Default::default()
        },
    );
    invalid_target.target = Target::Group("missing".into());
    assert!(
        engine
            .apply(
                2.0,
                &Command::SetOverride {
                    value: invalid_target
                }
            )
            .is_err()
    );
    let frame = engine.render_at(2.0).unwrap();
    assert_eq!(frame.active_autoloop.as_deref(), Some("loop"));
    near(
        frame.fixture_properties_before_strobe["left"].intensity,
        0.25,
    );
}

#[test]
fn expired_overrides_release_capacity_even_without_an_intervening_render() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    for index in 0..128 {
        let mut value = lighting_override(
            &format!("short_{index}"),
            PropertyValues {
                intensity: Some(0.5),
                ..Default::default()
            },
        );
        value.lifetime = OverrideLifetime::Seconds {
            duration_seconds: 1.0,
        };
        set_override(&mut engine, 0.0, value);
    }
    let replacement = lighting_override(
        "new",
        PropertyValues {
            intensity: Some(0.7),
            ..Default::default()
        },
    );
    assert!(
        engine
            .apply(
                0.0,
                &Command::SetOverride {
                    value: replacement.clone()
                }
            )
            .is_err()
    );
    set_override(&mut engine, 1.0, replacement);
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.7,
    );
}

#[test]
fn script_error_while_interrupting_a_fade_stops_the_existing_show() {
    let fault = "return {render=function(ctx) if ctx.autoloop_elapsed_beats >= 2 then error('fade fault') end return sf.scene {sf.dimmer(sf.all(), 0.5)} end}";
    let mut engine = engine(&[("fault", fault), ("constant", CONSTANT), ("blue", BLUE)]);
    select(&mut engine, 0.0, "fault", Transition::Cut);
    select(
        &mut engine,
        0.5,
        "constant",
        Transition::Crossfade {
            duration_beats: 8.0,
        },
    );
    engine.render_at(0.5).unwrap();
    let result = engine.apply(
        1.0,
        &Command::Select {
            bank: "test".into(),
            autoloop: "blue".into(),
            transition: Transition::Crossfade {
                duration_beats: 4.0,
            },
        },
    );
    assert!(result.is_err());
    assert!(engine.render_at(0.75).is_err());
    let stopped = engine.render_at(1.0).unwrap();
    assert!(
        stopped
            .script_error
            .as_deref()
            .unwrap()
            .contains("fade fault")
    );
    near(
        stopped.fixture_properties_before_strobe["left"].intensity,
        0.0,
    );
}

#[test]
fn invalid_override_replacement_preserves_the_existing_property_and_deadline() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    let mut held = lighting_override(
        "held",
        PropertyValues {
            intensity: Some(0.8),
            ..Default::default()
        },
    );
    held.lifetime = OverrideLifetime::Seconds {
        duration_seconds: 2.0,
    };
    set_override(&mut engine, 0.0, held.clone());
    for lifetime in [
        OverrideLifetime::Seconds {
            duration_seconds: 0.0,
        },
        OverrideLifetime::Seconds {
            duration_seconds: f64::INFINITY,
        },
        OverrideLifetime::Beats {
            duration_beats: -1.0,
        },
        OverrideLifetime::Beats {
            duration_beats: f64::NAN,
        },
    ] {
        let mut invalid = held.clone();
        invalid.values.intensity = Some(0.1);
        invalid.lifetime = lifetime;
        assert!(
            engine
                .apply(1.0, &Command::SetOverride { value: invalid })
                .is_err()
        );
    }
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.8,
    );
    near(
        engine
            .render_at(2.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.5,
    );
}

#[test]
fn invalid_fade_duration_does_not_replace_the_running_autoloop() {
    let mut engine = engine(&[("constant", CONSTANT), ("blue", BLUE)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    for duration_beats in [0.0, -1.0, f64::NAN, f64::INFINITY, 1025.0] {
        assert!(
            engine
                .apply(
                    1.0,
                    &Command::Select {
                        bank: "test".into(),
                        autoloop: "blue".into(),
                        transition: Transition::Crossfade { duration_beats }
                    }
                )
                .is_err()
        );
    }
    let frame = engine.render_at(1.0).unwrap();
    assert_eq!(frame.active_autoloop.as_deref(), Some("constant"));
    near(
        frame.fixture_properties_before_strobe["left"].intensity,
        0.5,
    );
}

#[test]
fn measured_energy_is_distinct_from_master_and_rejects_invalid_values() {
    let mut engine = engine(&[(
        "energy",
        "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), ctx.normalized_rms_level or 0)} end}",
    )]);
    select(&mut engine, 0.0, "energy", Transition::Cut);
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.0,
    );
    engine
        .apply(0.25, &Command::SetNormalizedRmsLevel { level: 0.8 })
        .unwrap();
    engine
        .apply(
            0.25,
            &Command::SetMaster {
                intensity: 0.5,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
    near(
        engine
            .render_at(0.25)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.4,
    );
    for level in [-0.1, 1.1, f64::NAN, f64::INFINITY] {
        assert!(
            engine
                .apply(0.5, &Command::SetNormalizedRmsLevel { level })
                .is_err()
        );
    }
    near(
        engine
            .render_at(0.5)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.4,
    );
    engine
        .apply(0.75, &Command::SetNormalizedRmsLevel { level: 0.0 })
        .unwrap();
    near(
        engine
            .render_at(0.75)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.0,
    );
}

#[test]
fn crossfade_progress_uses_elapsed_beats_instead_of_corrected_phase() {
    let mut engine = engine(&[("red", RED), ("blue", BLUE)]);
    select(&mut engine, 0.0, "red", Transition::Cut);
    select(
        &mut engine,
        0.0,
        "blue",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    engine
        .apply(0.5, &Command::SetSpeed { multiplier: 0.0 })
        .unwrap();
    engine
        .apply(
            1.0,
            &Command::Beat {
                beat_index: 100,
                bar_phase: None,
                beats_per_bar: 4,
                bpm: 60.0,
            },
        )
        .unwrap();
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.5,
    );
    near(
        engine
            .render_at(2.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.65,
    );
    near(
        engine
            .render_at(3.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.8,
    );
}

fn master_at(engine: &mut Engine, time_seconds: f64) -> f64 {
    // CONSTANT renders dimmer 0.5, so output intensity is half the master level.
    engine
        .render_at(time_seconds)
        .unwrap()
        .fixture_properties_before_strobe["left"]
        .intensity
        / 0.5
}

#[test]
fn master_fades_linearly_over_wall_time() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity: 0.15,
                fade_seconds: 1.0,
            },
        )
        .unwrap();
    near(master_at(&mut engine, 0.0), 1.0);
    near(master_at(&mut engine, 0.5), 0.575);
    near(master_at(&mut engine, 1.0), 0.15);
    near(master_at(&mut engine, 3.0), 0.15);
}

#[test]
fn master_retarget_mid_fade_starts_from_the_visible_level() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    let fade = |intensity| Command::SetMaster {
        intensity,
        fade_seconds: 1.0,
    };
    engine.apply(0.0, &fade(0.0)).unwrap();
    engine.apply(0.5, &fade(1.0)).unwrap();
    near(master_at(&mut engine, 0.5), 0.5);
    near(master_at(&mut engine, 1.0), 0.75);
    near(master_at(&mut engine, 1.5), 1.0);
}

#[test]
fn zero_master_fade_is_instant_and_invalid_fades_are_rejected() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity: 0.2,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
    near(master_at(&mut engine, 0.0), 0.2);
    for fade_seconds in [-1.0, f64::NAN, f64::INFINITY] {
        assert!(
            engine
                .apply(
                    0.0,
                    &Command::SetMaster {
                        intensity: 1.0,
                        fade_seconds,
                    },
                )
                .is_err()
        );
    }
    near(master_at(&mut engine, 0.0), 0.2);
}
