use serde_json::json;
use starfire::fixtures::FixtureRig;
use starfire::model::{DMX_CHANNELS, FixtureState, Rgb, Target};

fn fixture(profile: &str, address: usize) -> serde_json::Value {
    json!({"id":"test", "profile":profile, "universe":0, "address":address})
}

fn rig(fixtures: serde_json::Value) -> FixtureRig {
    FixtureRig::from_json(&json!({"schema_version":1, "fixtures":fixtures}).to_string()).unwrap()
}

fn full_light() -> FixtureState {
    FixtureState {
        intensity: 1.0,
        ..FixtureState::default()
    }
}

#[test]
fn rgb_and_rgbw_scale_intensity_without_a_master_channel() {
    let rgb = rig(json!([fixture("generic_rgb", 1)]));
    let rgbw = rig(json!([fixture("generic_rgbw", 1)]));
    let mut states = rgb.empty_states();
    states.insert(
        "test".into(),
        FixtureState {
            intensity: 0.5,
            colour: Rgb {
                red: 1.0,
                green: 0.5,
                blue: 0.25,
            },
            ..FixtureState::default()
        },
    );
    let frame = rgb.encode(&states).unwrap();
    assert_eq!(frame[&0].len(), DMX_CHANNELS);
    assert_eq!(&frame[&0][..4], &[128, 64, 32, 0]);
    assert_eq!(&rgbw.encode(&states).unwrap()[&0][..4], &[96, 32, 0, 32]);
}

#[test]
fn physical_profiles_keep_shutter_gobo_uv_and_program_controls_safe() {
    let rig = FixtureRig::from_json(include_str!("../../rigs/room.json")).unwrap();
    let mut states = rig.empty_states();
    for state in states.values_mut() {
        *state = FixtureState {
            intensity: 0.75,
            colour: Rgb {
                red: 1.0,
                green: 0.0,
                blue: 1.0,
            },
            pan: 0.25,
            tilt: 0.75,
            strobe_hz: 0.0,
            ..FixtureState::default()
        };
    }
    let frame = &rig.encode(&states).unwrap()[&0];
    for start in [0, 12] {
        assert_eq!(
            &frame[start..start + 12],
            &[64, 0, 191, 255, 0, 160, 7, 191, 0, 0, 0, 0]
        );
    }
    for start in [64, 72, 80, 88] {
        assert_eq!(&frame[start..start + 8], &[255, 0, 255, 0, 191, 0, 0, 0]);
    }
    assert!(
        frame[24..64]
            .iter()
            .chain(frame[120..].iter())
            .all(|byte| *byte == 0)
    );
}

#[test]
fn blackout_preserves_movement_and_disables_all_device_programs() {
    let rig = rig(json!([fixture("chauvet_intimidator_spot_110_12ch", 1)]));
    let mut states = rig.empty_states();
    let state = states.get_mut("test").unwrap();
    state.pan = 0.125;
    state.tilt = 0.875;
    state.intensity = 1.0;
    let lit_frame = rig.encode(&states).unwrap();
    states.get_mut("test").unwrap().intensity = 0.0;
    let dark_frame = rig.encode(&states).unwrap();
    assert_eq!(&dark_frame[&0][..4], &lit_frame[&0][..4]);
    assert_eq!(&dark_frame[&0][..4], &[32, 0, 223, 255]);
    assert_eq!(&dark_frame[&0][6..12], &[7, 0, 0, 0, 0, 0]);
}

#[test]
fn colour_wheel_maps_shades_to_same_hue_and_black_to_zero_intensity() {
    let rig = rig(json!([fixture("chauvet_intimidator_spot_110_12ch", 1)]));
    let mut states = rig.empty_states();
    for (colour, expected_slot, expected_dimmer) in [
        (
            Rgb {
                red: 1e-310,
                green: 0.0,
                blue: 0.0,
            },
            32,
            0,
        ),
        (
            Rgb {
                red: 0.25,
                green: 0.0,
                blue: 0.0,
            },
            32,
            64,
        ),
        (
            Rgb {
                red: 0.0,
                green: 0.0,
                blue: 0.5,
            },
            111,
            128,
        ),
        (
            Rgb {
                red: 0.0,
                green: 0.0,
                blue: 0.0,
            },
            0,
            0,
        ),
    ] {
        states.insert(
            "test".into(),
            FixtureState {
                colour,
                ..full_light()
            },
        );
        let frame = rig.encode(&states).unwrap();
        assert_eq!(frame[&0][5], expected_slot);
        assert_eq!(frame[&0][7], expected_dimmer);
    }
}

#[test]
fn movement_calibration_maps_inverted_endpoints_without_touching_macros() {
    let mut patch = fixture("chauvet_intimidator_spot_110_12ch", 1);
    patch["pan"] = json!({"min":0.25,"max":0.75,"inverted":true});
    patch["tilt"] = json!({"min":0.5,"max":0.5});
    let rig = rig(json!([patch]));
    let mut states = rig.empty_states();
    states.get_mut("test").unwrap().pan = 0.0;
    assert_eq!(&rig.encode(&states).unwrap()[&0][..4], &[191, 255, 128, 0]);
    states.get_mut("test").unwrap().pan = 1.0;
    assert_eq!(&rig.encode(&states).unwrap()[&0][..4], &[64, 0, 128, 0]);
}

#[test]
fn reference_patch_encodes_eight_independent_rgb_pixels_and_leaves_kinta_dark() {
    const STRIP_START: usize = 96;
    const RGB_CHANNELS: usize = 3;
    const PIXEL_COUNT: usize = 8;
    const MINI_KINTA_START: usize = 39;
    const MINI_KINTA_CHANNELS: usize = 4;
    let rig = FixtureRig::from_json(include_str!("../../rigs/room.json")).unwrap();
    let strip = rig.resolve(&Target::Group("strip".into())).unwrap();
    assert_eq!(strip.len(), PIXEL_COUNT);
    for (selected, id) in strip.iter().enumerate() {
        let mut states = rig.empty_states();
        states.get_mut(id).unwrap().intensity = 0.5;
        states.get_mut(id).unwrap().colour = Rgb {
            red: 0.2,
            green: 0.3,
            blue: 0.8,
        };
        let frames = rig.encode(&states).unwrap();
        let frame = &frames[&0];
        for pixel in 0..PIXEL_COUNT {
            let start = STRIP_START + pixel * RGB_CHANNELS;
            assert_eq!(
                &frame[start..start + RGB_CHANNELS],
                if pixel == selected {
                    &[26, 38, 102]
                } else {
                    &[0, 0, 0]
                }
            );
        }
        assert_eq!(
            &frame[MINI_KINTA_START..MINI_KINTA_START + MINI_KINTA_CHANNELS],
            &[0, 0, 0, 0]
        );
        assert!(
            frame[STRIP_START + PIXEL_COUNT * RGB_CHANNELS..]
                .iter()
                .all(|value| *value == 0)
        );
    }
}

#[test]
fn fixture_and_group_order_are_explicit_and_unknown_targets_fail() {
    let rig = FixtureRig::from_json(
        &json!({
            "schema_version":1,
            "fixtures":[
                {"id":"z", "profile":"generic_dimmer", "universe":0, "address":1},
                {"id":"a", "profile":"generic_dimmer", "universe":0, "address":2}
            ],
            "groups":{"wash":["a", "z"]}
        })
        .to_string(),
    )
    .unwrap();
    assert_eq!(rig.resolve(&Target::All).unwrap(), ["z", "a"]);
    assert_eq!(
        rig.resolve(&Target::Group("wash".into())).unwrap(),
        ["a", "z"]
    );
    assert_eq!(rig.resolve(&Target::Fixture("a".into())).unwrap(), ["a"]);
    assert!(rig.resolve(&Target::Group("missing".into())).is_err());
    assert!(rig.resolve(&Target::Fixture("missing".into())).is_err());
}

#[test]
fn patch_rejects_overlap_invalid_addresses_profiles_and_duplicate_ids() {
    let invalid = [
        json!([fixture("generic_dimmer", 0)]),
        json!([fixture("generic_dimmer", 513)]),
        json!([fixture("generic_rgb", 511)]),
        json!([fixture("unknown", 1)]),
        json!([fixture("generic_dimmer", 1), fixture("generic_dimmer", 2)]),
        json!([
            fixture("generic_rgb", 1),
            {"id":"other", "profile":"generic_dimmer", "universe":0, "address":3}
        ]),
        json!([{"id":"test", "profile":"generic_dimmer", "universe":32768, "address":1}]),
        json!([{"id":"", "profile":"generic_dimmer", "universe":0, "address":1}]),
    ];
    for fixtures in invalid {
        let definition = json!({"schema_version":1, "fixtures":fixtures});
        assert!(
            FixtureRig::from_json(&definition.to_string()).is_err(),
            "{definition}"
        );
    }
    for members in [json!([]), json!(["test", "test"]), json!(["missing"])] {
        let definition = json!({"schema_version":1,"fixtures":[fixture("generic_rgb",1)],"groups":{"wash":members}});
        assert!(FixtureRig::from_json(&definition.to_string()).is_err());
    }
}

#[test]
fn patch_rejects_bad_calibration_and_unsupported_schema() {
    for bounds in [
        json!({"min":-0.1}),
        json!({"max":1.1}),
        json!({"min":0.8,"max":0.2}),
    ] {
        let mut patch = fixture("chauvet_intimidator_spot_110_12ch", 1);
        patch["pan"] = bounds;
        assert!(
            FixtureRig::from_json(&json!({"schema_version":1,"fixtures":[patch]}).to_string())
                .is_err()
        );
    }
    assert!(
        FixtureRig::from_json(
            &json!({"schema_version":2,"fixtures":[fixture("generic_rgb",1)]}).to_string()
        )
        .is_err()
    );
}

#[test]
fn universes_have_independent_addresses_and_boundary_channels() {
    let rig = rig(json!([
        {"id":"first", "profile":"generic_dimmer", "universe":0, "address":512},
        {"id":"second", "profile":"generic_dimmer", "universe":32767, "address":512}
    ]));
    let mut states = rig.empty_states();
    states.insert("first".into(), full_light());
    states.insert(
        "second".into(),
        FixtureState {
            intensity: 0.5,
            ..FixtureState::default()
        },
    );
    let frames = rig.encode(&states).unwrap();
    assert_eq!(frames.keys().copied().collect::<Vec<_>>(), [0, 32767]);
    assert_eq!(frames[&0][511], 255);
    assert_eq!(frames[&32767][511], 128);
    assert!(frames[&0][..511].iter().all(|byte| *byte == 0));
}

#[test]
fn encoding_rejects_missing_unknown_nonfinite_and_unresolved_state() {
    let rig = rig(json!([fixture("generic_rgb", 1)]));
    let mut states = rig.empty_states();
    states.clear();
    assert!(rig.encode(&states).is_err());
    states.insert("unknown".into(), full_light());
    assert!(rig.encode(&states).is_err());
    let mut invalid = vec![
        FixtureState {
            intensity: f64::NAN,
            ..full_light()
        },
        FixtureState {
            intensity: 1.01,
            ..full_light()
        },
        FixtureState {
            pan: f64::INFINITY,
            ..full_light()
        },
        FixtureState {
            tilt: -0.1,
            ..full_light()
        },
        FixtureState {
            strobe_hz: 1.0,
            ..full_light()
        },
        FixtureState {
            strobe_hz: f64::NAN,
            ..full_light()
        },
    ];
    for component in 0..3 {
        let mut state = full_light();
        match component {
            0 => state.colour.red = -0.1,
            1 => state.colour.green = f64::NAN,
            _ => state.colour.blue = 1.01,
        }
        invalid.push(state);
    }
    for state in invalid {
        let mut states = rig.empty_states();
        states.insert("test".into(), state);
        assert!(rig.encode(&states).is_err());
    }
}
