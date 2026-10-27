mod support;

use std::collections::BTreeMap;
use std::fs;

use starfire::engine::Engine;
use starfire::model::{
    AttributeValue, Command, LightingOverride, OverrideLifetime, PropertyValues, Rgb, Target,
    Transition,
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
    let rig = support::room();
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
            special_fx: None,
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
    // The loop's sf.all() passes over the accent Kinta, which plays specials only.
    let room = support::room();
    for (id, state) in &frame.fixture_properties_before_strobe {
        if room.is_accent_cell(id) {
            continue;
        }
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
    let room = support::room();
    for (seconds, expected_intensity) in [(0.0, 204), (0.25, 0), (0.5, 204)] {
        let frame = engine.render_at(seconds).unwrap();
        assert!(
            frame
                .fixture_properties_before_strobe
                .iter()
                .filter(|(id, _)| !room.is_accent_cell(id))
                .all(|(_, state)| state.strobe_hz == 2.0 && state.intensity == 0.8)
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

#[test]
fn a_wash_blacklight_accent_drives_uv_full_with_rgb_off_above_the_automatic_look() {
    let mut engine = steady_room();
    let before = engine.render_at(0.0).unwrap();
    engine
        .apply(
            0.0,
            &Command::SetOverride {
                value: LightingOverride {
                    id: "static_wash".into(),
                    target: Target::Group("wash".into()),
                    values: PropertyValues {
                        intensity: Some(1.0),
                        colour: Some(Rgb {
                            red: 0.0,
                            green: 0.0,
                            blue: 0.0,
                        }),
                        attributes: BTreeMap::from([(
                            "ColorAdd_UV".into(),
                            AttributeValue::Number(255.0),
                        )]),
                        ..PropertyValues::default()
                    },
                    intensity_multiplier: None,
                    lifetime: OverrideLifetime::UntilCleared,
                    priority: 1101,
                    special_fx: None,
                },
            },
        )
        .unwrap();
    engine
        .apply(
            0.0,
            &Command::SetOverride {
                value: LightingOverride {
                    id: "drop_arrival".into(),
                    target: Target::Group("wash".into()),
                    values: PropertyValues {
                        intensity: Some(0.5),
                        colour: Some(Rgb {
                            red: 1.0,
                            green: 0.0,
                            blue: 0.0,
                        }),
                        strobe_hz: Some(2.0),
                        ..PropertyValues::default()
                    },
                    intensity_multiplier: None,
                    lifetime: OverrideLifetime::UntilCleared,
                    priority: 100,
                    special_fx: None,
                },
            },
        )
        .unwrap();
    let lit = engine.render_at(0.5).unwrap();
    for start in [64, 72, 80, 88] {
        assert_eq!(
            &lit.dmx_frames_by_universe[&0][start..start + 8],
            &[0, 0, 0, 255, 255, 0, 0, 0]
        );
    }
    assert_eq!(
        &lit.dmx_frames_by_universe[&0][..24],
        &before.dmx_frames_by_universe[&0][..24]
    );
    engine
        .apply(0.5, &Command::SetBlackout { enabled: true })
        .unwrap();
    let dark = engine.render_at(1.0).unwrap();
    for start in [64, 72, 80, 88] {
        assert_eq!(
            &dark.dmx_frames_by_universe[&0][start..start + 8],
            &[0, 0, 0, 0, 0, 0, 0, 0]
        );
    }
}

#[test]
fn a_group_with_an_unknown_role_is_refused_rather_than_read_as_a_loop_group() {
    let mut definition = support::room_definition();
    definition.groups[0].role = 99;
    let name = definition.groups[0].group_id.clone();
    let error = starfire::fixtures::FixtureRig::from_wire(&definition).unwrap_err();
    assert_eq!(
        error.to_string(),
        format!("fixture group {name} has unknown role 99")
    );
}
