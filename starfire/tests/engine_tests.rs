mod support;

use std::fs;

use starfire::engine::{Engine, perceptual_to_linear_master};
use starfire::fixtures::FixtureRig;
use starfire::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Rgb, Target, Transition,
};
use starfire::script::AutoloopCatalog;

const RIG: &str = r#"{"schema_version":1,"fixtures":[
    {"fixture_uuid":"left","profile":"generic_rgb","universe":0,"address":1},
    {"fixture_uuid":"right","profile":"generic_rgb","universe":0,"address":4}],
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
    let rig = support::rig(serde_json::from_str(RIG).unwrap());
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

fn lighting_override(id: &str, values: PropertyValues) -> LightingOverride {
    LightingOverride {
        id: id.into(),
        target: Target::All,
        values,
        intensity_multiplier: None,
        lifetime: OverrideLifetime::UntilCleared,
        priority: 0,
        special_fx: None,
        pan_sweep: false,
        aim_focus: false,
        pose: None,
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
                instantaneous_release: false,
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
        .apply(
            3.0,
            &Command::ClearOverride {
                id: "equal".into(),
                instantaneous_release: false,
            },
        )
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
        0.0025,
    );
    engine
        .apply(
            0.0,
            &Command::ClearOverride {
                id: "attenuate".into(),
                instantaneous_release: false,
            },
        )
        .unwrap();
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.25,
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
        0.25,
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
                        transition: Transition::Cut,
                        palette: "techno".into(),
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
            palette: "techno".into(),
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
fn a_seconds_override_re_sent_as_a_keep_alive_holds_past_ten_minutes_then_lapses() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    let mut held = lighting_override(
        "highlight",
        PropertyValues {
            intensity: Some(0.8),
            ..Default::default()
        },
    );
    held.lifetime = OverrideLifetime::Seconds {
        duration_seconds: 60.0,
    };
    let left = |engine: &mut Engine, at: f64| {
        engine
            .render_at(at)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity
    };
    for tick in 0..=60 {
        let at = f64::from(tick) * 15.0;
        set_override(&mut engine, at, held.clone());
        near(left(&mut engine, at + 14.999), 0.8);
    }
    near(left(&mut engine, 900.0 + 59.999), 0.8);
    assert!((left(&mut engine, 960.0) - 0.8).abs() > 0.1);
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
                        transition: Transition::Crossfade { duration_beats },
                        palette: "techno".into(),
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
        0.2,
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
        0.2,
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

fn linear_master_at(engine: &mut Engine, time_seconds: f64) -> f64 {
    engine
        .render_at(time_seconds)
        .unwrap()
        .fixture_properties_before_strobe["left"]
        .intensity
        / 0.5
}

fn perceptual_master_at(engine: &mut Engine, time_seconds: f64) -> f64 {
    linear_master_at(engine, time_seconds).sqrt()
}

fn set_master_instantly(engine: &mut Engine, intensity: f64) {
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
}

#[test]
fn half_perceptual_master_drives_quarter_linear_output() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    set_master_instantly(&mut engine, 0.5);
    near(linear_master_at(&mut engine, 0.0), 0.25);
    assert_eq!(perceptual_to_linear_master(0.5), 0.25);
}

#[test]
fn full_master_leaves_output_unchanged() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    set_master_instantly(&mut engine, 1.0);
    near(linear_master_at(&mut engine, 0.0), 1.0);
    assert_eq!(perceptual_to_linear_master(1.0), 1.0);
    assert_eq!(perceptual_to_linear_master(0.0), 0.0);
}

#[test]
fn master_fades_evenly_in_perceptual_space_over_wall_time() {
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
    near(perceptual_master_at(&mut engine, 0.0), 1.0);
    near(perceptual_master_at(&mut engine, 0.5), 0.575);
    near(linear_master_at(&mut engine, 0.5), 0.575 * 0.575);
    near(perceptual_master_at(&mut engine, 1.0), 0.15);
    near(perceptual_master_at(&mut engine, 3.0), 0.15);
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
    near(perceptual_master_at(&mut engine, 0.5), 0.5);
    near(perceptual_master_at(&mut engine, 1.0), 0.75);
    near(perceptual_master_at(&mut engine, 1.5), 1.0);
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
    near(perceptual_master_at(&mut engine, 0.0), 0.2);
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
    near(perceptual_master_at(&mut engine, 0.0), 0.2);
}

fn paused(engine: &mut Engine, at: f64, paused: bool) {
    engine.apply(at, &Command::SetPaused { paused }).unwrap();
}

#[test]
fn pause_goes_dark_holds_the_beat_and_resume_continues_from_the_held_position() {
    let mut engine = engine(&[("loop", LOOP)]);
    select(&mut engine, 0.0, "loop", Transition::Cut);
    paused(&mut engine, 1.0, true);
    let held = engine.render_at(1.0).unwrap();
    near(held.beat_position, 2.0);
    assert!(
        held.fixture_properties_before_strobe
            .values()
            .all(|state| state.output_blackout)
    );
    engine
        .apply(
            2.0,
            &Command::Beat {
                beat_index: 4,
                bar_phase: None,
                beats_per_bar: 4,
                bpm: 120.0,
            },
        )
        .unwrap();
    let later = engine.render_at(3.0).unwrap();
    near(later.beat_position, 2.0);
    assert_eq!(
        later.fixture_properties_before_strobe,
        held.fixture_properties_before_strobe
    );
    assert_eq!(later.dmx_frames_by_universe, held.dmx_frames_by_universe);

    paused(&mut engine, 3.0, false);
    near(engine.render_at(3.5).unwrap().beat_position, 3.0);
    engine
        .apply(
            4.0,
            &Command::Beat {
                beat_index: 8,
                bar_phase: None,
                beats_per_bar: 4,
                bpm: 120.0,
            },
        )
        .unwrap();
    near(engine.render_at(4.0).unwrap().beat_position, 8.0);
}

#[test]
fn pause_releases_effect_and_script_strobes_and_they_stay_released() {
    let mut engine = engine(&[("red", RED)]);
    select(&mut engine, 0.0, "red", Transition::Cut);
    set_override(
        &mut engine,
        0.0,
        lighting_override(
            "accent",
            PropertyValues {
                strobe_hz: Some(10.0),
                ..Default::default()
            },
        ),
    );
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz,
        10.0,
    );
    paused(&mut engine, 0.5, true);
    near(
        engine
            .render_at(0.5)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz,
        0.0,
    );
    paused(&mut engine, 1.0, false);
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz,
        2.0,
    );
}

#[test]
fn selection_without_a_transition_uses_the_renderer_default_crossfade() {
    let mut engine = engine(&[("red", RED), ("blue", BLUE)]);
    select(&mut engine, 0.0, "red", Transition::RendererDefault);
    select(&mut engine, 1.0, "blue", Transition::RendererDefault);
    near(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.8,
    );
    engine
        .apply(1.0, &Command::SetDefaultCrossfadeBeats { beats: 2.0 })
        .unwrap();
    select(&mut engine, 1.0, "red", Transition::RendererDefault);
    near(
        engine
            .render_at(1.5)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.5,
    );
    select(&mut engine, 2.0, "blue", Transition::Cut);
    near(
        engine
            .render_at(2.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .intensity,
        0.8,
    );
}

#[test]
fn strobe_caps_clamp_the_rate_and_release_a_burst_until_its_strobe_stops() {
    let mut engine = engine(&[("blue", BLUE), ("constant", CONSTANT)]);
    select(&mut engine, 0.0, "blue", Transition::Cut);
    engine
        .apply(0.0, &Command::SetStrobeMaxRateHz { hz: 4.0 })
        .unwrap();
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 1.0 })
        .unwrap();
    let rate = |engine: &mut Engine, at: f64| {
        engine
            .render_at(at)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz
    };
    near(rate(&mut engine, 0.0), 4.0);
    near(rate(&mut engine, 0.9), 4.0);
    near(rate(&mut engine, 1.0), 0.0);
    near(rate(&mut engine, 5.0), 0.0);
    select(&mut engine, 5.0, "constant", Transition::Cut);
    near(rate(&mut engine, 5.0), 0.0);
    select(&mut engine, 6.0, "blue", Transition::Cut);
    near(rate(&mut engine, 6.0), 4.0);
}

#[test]
fn renderer_settings_reject_values_outside_their_ranges() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    for invalid in [
        Command::SetDefaultCrossfadeBeats { beats: -1.0 },
        Command::SetDefaultCrossfadeBeats { beats: f64::NAN },
        Command::SetStrobeMaxRateHz { hz: -0.1 },
        Command::SetStrobeMaxRateHz { hz: 10.5 },
        Command::SetStrobeMaxBurstSeconds { seconds: 0.0 },
        Command::SetStrobeMaxBurstSeconds { seconds: 61.0 },
        Command::SetStrobeMaxBurstSeconds {
            seconds: f64::INFINITY,
        },
    ] {
        assert!(engine.apply(0.0, &invalid).is_err(), "{invalid:?}");
    }
}

#[test]
fn the_burst_cap_releases_a_timed_strobe_override_before_its_own_lifetime() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 1.0 })
        .unwrap();
    let mut accent = lighting_override(
        "accent",
        PropertyValues {
            strobe_hz: Some(8.0),
            ..Default::default()
        },
    );
    accent.lifetime = OverrideLifetime::Seconds {
        duration_seconds: 10.0,
    };
    set_override(&mut engine, 0.0, accent);
    let rate = |engine: &mut Engine, at: f64| {
        engine
            .render_at(at)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz
    };
    near(rate(&mut engine, 0.0), 8.0);
    near(rate(&mut engine, 1.0), 0.0);
    near(rate(&mut engine, 5.0), 0.0);
}

fn room_engine(scripts: &[(&str, &str)]) -> Engine {
    engine_on(support::room(), scripts)
}

fn engine_on(rig: FixtureRig, scripts: &[(&str, &str)]) -> Engine {
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

#[test]
fn a_loop_on_all_leaves_the_accent_fixtures_dark() {
    let room = support::room();
    let mut engine = room_engine(&[(
        "lit",
        "return {render=function(ctx) return {sf.dimmer(sf.all(), 1)} end}",
    )]);
    select(&mut engine, 0.0, "lit", Transition::Cut);
    let states = engine
        .render_at(0.0)
        .unwrap()
        .fixture_properties_before_strobe;
    assert_eq!(states[&room.groups()["wash"][0]].intensity, 1.0);
    for cell in &room.groups()["fx"] {
        assert_eq!(states[cell].intensity, 0.0, "{cell}");
    }
}

#[test]
fn a_crossfade_takes_the_target_preset_from_its_start_and_ramps_numeric_attributes() {
    use starfire::model::AttributeValue;
    let rig = FixtureRig::from_wire(&support::room_definition_as_loops()).unwrap();
    let fx_fixture = rig.groups()["fx"][0].clone();
    let mut engine = engine_on(
        rig,
        &[
            ("empty", "return {render=function(ctx) return {} end}"),
            (
                "still",
                "return {render=function(ctx) return {sf.physical_attribute(sf.group('fx'), 'Effects1PosRotate', 0)} end}",
            ),
            (
                "spin",
                "return {render=function(ctx) return sf.scene {sf.channel_preset(sf.group('fx'), 'Color1', 'Red'), sf.physical_attribute(sf.group('fx'), 'Effects1PosRotate', 160)} end}",
            ),
        ],
    );
    let red = AttributeValue::Preset("Red".into());
    select(&mut engine, 0.0, "empty", Transition::Cut);
    select(
        &mut engine,
        0.0,
        "spin",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    for time in [0.0, 1.0, 1.9] {
        let attributes = &engine
            .render_at(time)
            .unwrap()
            .fixture_properties_before_strobe[&fx_fixture]
            .attributes;
        assert_eq!(attributes.get("Color1"), Some(&red), "{time}");
        assert_eq!(
            attributes.get("Effects1PosRotate"),
            Some(&AttributeValue::Number(160.0)),
            "{time}"
        );
    }
    select(&mut engine, 2.0, "still", Transition::Cut);
    select(
        &mut engine,
        2.0,
        "spin",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    for (time, expected) in [(2.0, 0.0), (3.0, 80.0), (4.0, 160.0)] {
        let attributes = &engine
            .render_at(time)
            .unwrap()
            .fixture_properties_before_strobe[&fx_fixture]
            .attributes;
        assert_eq!(attributes.get("Color1"), Some(&red), "{time}");
        let AttributeValue::Number(rotation) = attributes["Effects1PosRotate"] else {
            panic!("{time}: {attributes:?}");
        };
        assert!((rotation - expected).abs() < 1e-9, "{time}: {rotation}");
    }
}

const SECONDARY: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.5), sf.colour(sf.all(), sf.palette().secondary)} end}";
const TECHNO_SECONDARY: Rgb = Rgb {
    red: 1.0,
    green: 0.55,
    blue: 0.1,
};
const HOUSE_SECONDARY: Rgb = Rgb {
    red: 1.0,
    green: 0.0,
    blue: 0.7,
};
const HALFWAY_SECONDARY: Rgb = Rgb {
    red: 1.0,
    green: 0.275,
    blue: 0.4,
};

fn palette_parameter(name: &str) -> starfire::protocol::wire::Parameter {
    use starfire::protocol::wire;
    wire::Parameter {
        r#type: wire::ParameterType::Palette as i32,
        value: Some(wire::parameter::Value::Text(name.into())),
        ..Default::default()
    }
}

fn select_with_palette(
    engine: &mut Engine,
    at: f64,
    transition: Transition,
    palette: &str,
) -> anyhow::Result<()> {
    engine.apply(
        at,
        &Command::Select {
            bank: "test".into(),
            autoloop: "secondary".into(),
            transition,
            palette: palette.into(),
        },
    )
}

fn colour_at(engine: &mut Engine, seconds: f64) -> Rgb {
    engine
        .render_at(seconds)
        .unwrap()
        .fixture_properties_before_strobe["left"]
        .colour
}

fn assert_colour(actual: Rgb, expected: Rgb) {
    near(actual.red, expected.red);
    near(actual.green, expected.green);
    near(actual.blue, expected.blue);
}

#[test]
fn palette_parameter_crossfades_slots_over_the_default_crossfade_beats() {
    let mut engine = engine(&[("secondary", SECONDARY)]);
    engine
        .apply(0.0, &Command::SetDefaultCrossfadeBeats { beats: 4.0 })
        .unwrap();
    select(&mut engine, 0.0, "secondary", Transition::Cut);
    assert_colour(colour_at(&mut engine, 0.0), TECHNO_SECONDARY);
    engine
        .set_parameter(0.0, &palette_parameter("house"))
        .unwrap();
    assert_eq!(engine.active_palette(), "house");
    let halfway = engine.render_at(1.0).unwrap();
    assert_colour(
        halfway.fixture_properties_before_strobe["left"].colour,
        HALFWAY_SECONDARY,
    );
    near(
        halfway.fixture_properties_before_strobe["left"].intensity,
        0.5,
    );
    assert_eq!(colour_at(&mut engine, 2.0), HOUSE_SECONDARY);
}

#[test]
fn a_selection_palette_fades_with_the_loop_transition() {
    let mut engine = engine(&[("secondary", SECONDARY)]);
    select_with_palette(&mut engine, 0.0, Transition::Cut, "techno").unwrap();
    select_with_palette(
        &mut engine,
        0.0,
        Transition::Crossfade {
            duration_beats: 2.0,
        },
        "house",
    )
    .unwrap();
    assert_colour(colour_at(&mut engine, 0.5), HALFWAY_SECONDARY);
    assert_eq!(colour_at(&mut engine, 1.0), HOUSE_SECONDARY);
    select_with_palette(&mut engine, 1.0, Transition::Cut, "techno").unwrap();
    assert_eq!(colour_at(&mut engine, 1.0), TECHNO_SECONDARY);
}

#[test]
fn unknown_palettes_are_rejected_on_both_paths_without_changing_the_show() {
    let mut engine = engine(&[("secondary", SECONDARY), ("constant", CONSTANT)]);
    select_with_palette(&mut engine, 0.0, Transition::Cut, "techno").unwrap();
    let error = engine
        .set_parameter(0.0, &palette_parameter("neon"))
        .unwrap_err();
    assert!(
        format!("{error:#}").contains(r#"unknown palette "neon"; this session has house, techno"#),
        "{error:#}"
    );
    let error = engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "constant".into(),
                transition: Transition::Cut,
                palette: "neon".into(),
            },
        )
        .unwrap_err();
    assert!(format!("{error:#}").contains("unknown palette"));
    let frame = engine.render_at(0.0).unwrap();
    assert_eq!(frame.active_autoloop.as_deref(), Some("secondary"));
    assert_eq!(engine.active_palette(), "techno");
    assert_eq!(
        frame.fixture_properties_before_strobe["left"].colour,
        TECHNO_SECONDARY
    );
}

#[test]
fn selection_and_parameter_palettes_write_one_active_palette() {
    for (first, last) in [("house", "techno"), ("techno", "house")] {
        let mut selection_then_parameter = engine(&[("secondary", SECONDARY)]);
        select_with_palette(&mut selection_then_parameter, 0.0, Transition::Cut, first).unwrap();
        selection_then_parameter
            .set_parameter(0.5, &palette_parameter(last))
            .unwrap();
        let mut parameter_then_selection = engine(&[("secondary", SECONDARY)]);
        parameter_then_selection
            .set_parameter(0.0, &palette_parameter(first))
            .unwrap();
        select_with_palette(&mut parameter_then_selection, 0.5, Transition::Cut, last).unwrap();
        assert_eq!(selection_then_parameter.active_palette(), last);
        assert_eq!(parameter_then_selection.active_palette(), last);
        assert_eq!(
            selection_then_parameter.render_at(1.0).unwrap(),
            parameter_then_selection.render_at(1.0).unwrap()
        );
    }
}

const DIM_SECONDARY: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.2), sf.colour(sf.all(), sf.palette().secondary)} end}";

#[test]
fn a_palette_change_during_a_loop_crossfade_blends_both_loops_with_the_same_slots() {
    let mut engine = engine(&[("dim", DIM_SECONDARY), ("secondary", SECONDARY)]);
    engine
        .apply(0.0, &Command::SetDefaultCrossfadeBeats { beats: 2.0 })
        .unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "dim".into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
    select_with_palette(
        &mut engine,
        0.0,
        Transition::Crossfade {
            duration_beats: 4.0,
        },
        "techno",
    )
    .unwrap();
    engine
        .set_parameter(0.5, &palette_parameter("house"))
        .unwrap();
    let mut previous = colour_at(&mut engine, 0.5);
    assert_eq!(previous, TECHNO_SECONDARY);
    for step in 1..=9 {
        let seconds = 0.5 + f64::from(step) * 0.05;
        let colour = colour_at(&mut engine, seconds);
        assert!(
            (colour.green - previous.green).abs() <= 0.55 * 0.05 + 1e-9,
            "palette snapped at {seconds}s"
        );
        previous = colour;
    }
    let halfway = engine.render_at(1.0).unwrap();
    let left = &halfway.fixture_properties_before_strobe["left"];
    assert_colour(left.colour, HALFWAY_SECONDARY);
    near(left.intensity, 0.35);
    assert_eq!(colour_at(&mut engine, 1.5), HOUSE_SECONDARY);
}

#[test]
fn scripts_cannot_replace_palette_slots() {
    for script in [
        "local p = sf.palette() p.primary = sf.rgb(0, 1, 0) return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 1)} end}",
        "local p = sf.palette('house') p.cold = sf.rgb(0, 1, 0) return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 1)} end}",
    ] {
        let mut engine = engine(&[("tamper", script)]);
        let error = engine
            .apply(
                0.0,
                &Command::Select {
                    bank: "test".into(),
                    autoloop: "tamper".into(),
                    transition: Transition::Cut,
                    palette: "techno".into(),
                },
            )
            .unwrap_err();
        assert!(
            format!("{error:#}").contains("palettes are read-only"),
            "{error:#}"
        );
    }
}

fn set_intensity(engine: &mut Engine, at: f64, level: f64) {
    engine.apply(at, &Command::SetIntensity { level }).unwrap();
}

#[test]
fn intensity_gates_strobes_below_the_threshold_and_scales_the_caps_above_it() {
    let mut engine = engine(&[("blue", BLUE)]);
    select(&mut engine, 0.0, "blue", Transition::Cut);
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 2.0 })
        .unwrap();
    let rate = |engine: &mut Engine, at: f64| {
        engine
            .render_at(at)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz
    };
    set_intensity(&mut engine, 0.0, 0.29);
    near(rate(&mut engine, 0.0), 0.0);
    set_intensity(&mut engine, 1.0, 0.5);
    near(rate(&mut engine, 1.0), 5.0);
    near(rate(&mut engine, 1.9), 5.0);
    near(rate(&mut engine, 2.0), 0.0);
}

#[test]
fn full_intensity_leaves_strobes_at_the_operator_caps() {
    let mut engine = engine(&[("blue", BLUE)]);
    select(&mut engine, 0.0, "blue", Transition::Cut);
    set_intensity(&mut engine, 0.0, 1.0);
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .strobe_hz,
        8.0,
    );
}

fn pan_after_snap(intensity: f64) -> Vec<f64> {
    const FRAME_SECONDS: f64 = 1.0 / 40.0;
    let mut engine = engine(&[("constant", CONSTANT)]);
    select(&mut engine, 0.0, "constant", Transition::Cut);
    set_intensity(&mut engine, 0.0, intensity);
    near(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe["left"]
            .pan,
        0.5,
    );
    set_override(
        &mut engine,
        0.0,
        lighting_override(
            "snap",
            PropertyValues {
                pan: Some(1.0),
                tilt: Some(0.0),
                ..Default::default()
            },
        ),
    );
    // Three seconds: calm's glide needs under one to cross half the range.
    (1..=120)
        .map(|frame| {
            let state = &engine
                .render_at(frame as f64 * FRAME_SECONDS)
                .unwrap()
                .fixture_properties_before_strobe["left"];
            near(state.pan - 0.5, 0.5 - state.tilt);
            state.pan
        })
        .collect()
}

#[test]
fn a_snap_at_full_intensity_reaches_its_target_in_one_frame() {
    assert!(pan_after_snap(1.0).iter().all(|pan| *pan == 1.0));
}

#[test]
fn a_snap_at_low_intensity_glides_to_its_target_without_overshooting() {
    let pans = pan_after_snap(0.3);
    let frames_to_target = pans.iter().position(|pan| *pan == 1.0).unwrap() + 1;
    assert!(frames_to_target > 5, "{pans:?}");
    assert!(
        pans.windows(2)
            .all(|pair| pair[0] <= pair[1] && pair[1] <= 1.0)
    );
    assert!(pans[frames_to_target..].iter().all(|pan| *pan == 1.0));
}

#[test]
fn low_and_mid_intensity_move_at_clearly_different_speeds() {
    let frames_to_target = |intensity| {
        pan_after_snap(intensity)
            .iter()
            .position(|pan| *pan == 1.0)
            .unwrap()
            + 1
    };
    let (low, mid) = (frames_to_target(0.3), frames_to_target(0.7));
    // Calm moves at 0.3 of full and balanced at 0.7: 7/3 as many frames.
    assert!(low > 2 * mid, "0.3 took {low} frames, 0.7 took {mid}");
}

#[test]
fn pulse_depth_and_movement_follow_the_three_level_table() {
    use starfire::intensity::{
        INTENSITY_BALANCED, INTENSITY_CALM, INTENSITY_WILD, movement_range_per_second,
        movement_speed, pulse_depth,
    };
    for (level, depth, speed) in [
        (INTENSITY_CALM, 0.25, 0.3),
        (INTENSITY_BALANCED, 0.8, 0.7),
        (INTENSITY_WILD, 1.0, 1.0),
        (0.5, 0.525, 0.5),
        (0.0, 0.25, 0.3),
    ] {
        near(pulse_depth(level), depth);
        near(movement_speed(level), speed);
    }
    near(movement_range_per_second(INTENSITY_CALM).unwrap(), 0.6);
    assert_eq!(movement_range_per_second(INTENSITY_WILD), None);
}

#[test]
fn intensity_outside_zero_to_one_is_rejected() {
    let mut engine = engine(&[("constant", CONSTANT)]);
    for level in [-0.1, 1.1, f64::NAN] {
        assert!(engine.apply(0.0, &Command::SetIntensity { level }).is_err());
    }
}

fn group_master(group: &str, level: f64) -> starfire::protocol::wire::Parameter {
    use starfire::protocol::wire;
    wire::Parameter {
        r#type: wire::ParameterType::GroupMaster as i32,
        value: Some(wire::parameter::Value::Number(level)),
        group: group.into(),
        ..Default::default()
    }
}

#[test]
fn a_half_group_master_dims_only_that_groups_output_on_the_master_curve() {
    let mut engine = room_engine(&[(
        "lit",
        "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.8), sf.colour(sf.all(), sf.rgb(1, 0.5, 0))} end}",
    )]);
    select(&mut engine, 0.0, "lit", Transition::Cut);
    set_master_instantly(&mut engine, 0.5);
    let before = engine.render_at(0.0).unwrap();
    engine
        .set_parameter(0.0, &group_master("wash", 0.5))
        .unwrap();
    let capped = engine.render_at(0.0).unwrap();
    let wash = support::room().groups()["wash"].clone();
    for (cell, state) in &before.fixture_properties_before_strobe {
        let expected = if wash.contains(cell) {
            state.intensity * perceptual_to_linear_master(0.5)
        } else {
            state.intensity
        };
        near(
            capped.fixture_properties_before_strobe[cell].intensity,
            expected,
        );
    }
    assert!(
        wash.iter()
            .all(|cell| before.fixture_properties_before_strobe[cell].intensity > 0.0)
    );
    assert_ne!(capped.dmx_frames_by_universe, before.dmx_frames_by_universe);
    engine
        .set_parameter(0.0, &group_master("wash", 1.0))
        .unwrap();
    let restored = engine.render_at(0.0).unwrap();
    assert_eq!(
        restored.dmx_frames_by_universe,
        before.dmx_frames_by_universe
    );
}

#[test]
fn group_masters_reject_unknown_groups_values_outside_unit_and_stray_groups() {
    use starfire::protocol::wire;
    let mut engine = room_engine(&[("empty", "return {render=function(ctx) return {} end}")]);
    assert!(
        engine
            .set_parameter(0.0, &group_master("ceiling", 0.5))
            .is_err()
    );
    assert!(
        engine
            .set_parameter(0.0, &group_master("wash", 1.5))
            .is_err()
    );
    assert!(engine.set_parameter(0.0, &group_master("", 0.5)).is_err());
    let stray = wire::Parameter {
        group: "wash".into(),
        ..palette_parameter("techno")
    };
    assert!(engine.set_parameter(0.0, &stray).is_err());
}
