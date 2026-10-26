mod support;

use std::fs;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Rgb, Target, Transition,
};
use starfire::script::AutoloopCatalog;

fn steady_room() -> Engine {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    fs::write(
        directory.path().join("test/steady.lua"),
        r#"
        return { render = function(ctx)
            return sf.scene {
                sf.dimmer(sf.all(), 0.8),
                sf.colour(sf.all(), {red = 0, green = 0, blue = 1}),
                sf.pan_tilt(sf.group("movers"), 0.25, 0.75)
            }
        end }
    "#,
    )
    .unwrap();
    let catalogue = AutoloopCatalog::load(directory.path()).unwrap();
    let rig = FixtureRig::decode(include_bytes!("../../rigs/room.rig.pb")).unwrap();
    let mut engine = Engine::new(rig, catalogue, support::palettes()).unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "steady".into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
    engine
}

fn lighting_override(id: &str, target: Target, values: PropertyValues) -> Command {
    Command::SetOverride {
        value: LightingOverride {
            id: id.into(),
            target,
            values,
            intensity_multiplier: None,
            lifetime: OverrideLifetime::UntilCleared,
            priority: 0,
        },
    }
}

#[test]
fn partial_lua_properties_compose_before_master_and_profile_encoding() {
    let mut engine = steady_room();
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity: 0.5,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
    let frame = engine.render_at(0.0).unwrap();
    for state in frame.fixture_properties_before_strobe.values() {
        assert_eq!(state.intensity, 0.2);
        assert_eq!(
            state.colour,
            Rgb {
                red: 0.0,
                green: 0.0,
                blue: 1.0
            }
        );
    }
    for start in [0, 12] {
        assert_eq!(
            &frame.dmx_frames_by_universe[&0][start..start + 12],
            &[149, 85, 191, 255, 0, 111, 7, 51, 0, 0, 0, 0]
        );
    }
    for start in [64, 72, 80, 88] {
        assert_eq!(
            &frame.dmx_frames_by_universe[&0][start..start + 8],
            &[0, 0, 255, 0, 51, 0, 0, 0]
        );
    }
}

#[test]
fn timed_strobe_gates_dimmers_without_selecting_hardware_strobe_or_macros() {
    let mut engine = steady_room();
    engine
        .apply(
            0.0,
            &lighting_override(
                "accent",
                Target::All,
                PropertyValues {
                    strobe_hz: Some(2.0),
                    ..PropertyValues::default()
                },
            ),
        )
        .unwrap();
    for (seconds, expected_intensity) in [(0.0, 204), (0.25, 0), (0.5, 204)] {
        let frame = engine.render_at(seconds).unwrap();
        assert!(
            frame
                .fixture_properties_before_strobe
                .values()
                .all(|state| state.strobe_hz == 2.0 && state.intensity == 0.8)
        );
        for start in [0, 12] {
            assert_eq!(
                frame.dmx_frames_by_universe[&0][start + 7],
                expected_intensity
            );
            assert_eq!(frame.dmx_frames_by_universe[&0][start + 6], 7);
            assert_eq!(
                &frame.dmx_frames_by_universe[&0][start + 8..start + 12],
                &[0, 0, 0, 0]
            );
        }
        for start in [64, 72, 80, 88] {
            assert_eq!(
                frame.dmx_frames_by_universe[&0][start + 4],
                expected_intensity
            );
            assert_eq!(
                &frame.dmx_frames_by_universe[&0][start + 5..start + 8],
                &[0, 0, 0]
            );
        }
    }
    engine
        .apply(0.5, &Command::SetBlackout { enabled: true })
        .unwrap();
    let blackout = engine.render_at(0.6).unwrap();
    assert!(
        blackout
            .fixture_properties_before_strobe
            .values()
            .all(|state| state.intensity == 0.0 && state.strobe_hz == 0.0)
    );
    assert_eq!(
        &blackout.dmx_frames_by_universe[&0][..4],
        &[149, 85, 191, 255]
    );
    assert_eq!(
        &blackout.dmx_frames_by_universe[&0][6..12],
        &[7, 0, 0, 0, 0, 0]
    );
    engine.apply(0.7, &Command::Stop).unwrap();
    let stopped = engine.render_at(0.7).unwrap();
    assert_eq!(
        stopped.dmx_frames_by_universe,
        blackout.dmx_frames_by_universe
    );
}

#[test]
fn held_colour_changes_only_target_colour_and_clear_reveals_baseline() {
    let mut engine = steady_room();
    let before = engine.render_at(0.0).unwrap();
    engine
        .apply(
            0.0,
            &lighting_override(
                "red",
                Target::Fixture("spot_left".into()),
                PropertyValues {
                    colour: Some(Rgb {
                        red: 1.0,
                        green: 0.0,
                        blue: 0.0,
                    }),
                    ..PropertyValues::default()
                },
            ),
        )
        .unwrap();
    let held = engine.render_at(0.25).unwrap();
    let mut expected = before.dmx_frames_by_universe.clone();
    expected.get_mut(&0).unwrap()[5] = 32;
    assert_eq!(held.dmx_frames_by_universe, expected);
    engine
        .apply(0.25, &Command::ClearOverride { id: "red".into() })
        .unwrap();
    assert_eq!(
        engine.render_at(0.5).unwrap().dmx_frames_by_universe,
        before.dmx_frames_by_universe
    );
}
