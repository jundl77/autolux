mod support;

use std::collections::BTreeMap;

use prost::Message;
use serde_json::json;
use starfire::fixtures::FixtureRig;
use starfire::gdtf::GdtfProfile;
use starfire::model::{AttributeValue, DMX_CHANNELS, FixtureState, Rgb, Target};
use starfire::protocol::wire;

const HALF_TURN: [f64; 12] = [-1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0];

fn fixture(profile: &str, address: usize) -> serde_json::Value {
    json!({"id":"test", "profile":profile, "universe":0, "address":address})
}

fn rig(fixtures: serde_json::Value) -> FixtureRig {
    support::rig(json!({ "fixtures": fixtures }))
}

fn accepts(spec: serde_json::Value) -> bool {
    support::try_rig(spec).is_ok()
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
    let rig = support::room();
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
    let wheel_fixture = 39..43;
    let magenta_slot = 81;
    assert_eq!(&frame[wheel_fixture.clone()], &[magenta_slot, 0, 0, 0]);
    for start in [0, 12] {
        assert_eq!(
            &frame[start..start + 12],
            &[149, 85, 191, 255, 0, 160, 7, 191, 0, 0, 0, 0]
        );
    }
    for start in [64, 72, 80, 88] {
        assert_eq!(&frame[start..start + 8], &[255, 0, 255, 0, 191, 0, 0, 0]);
    }
    assert!(
        frame[24..wheel_fixture.start]
            .iter()
            .chain(&frame[wheel_fixture.end..64])
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
fn matrix_yaw_shifts_pan_home_and_identity_matches_unrotated_dmx() {
    let degrees =
        |frame: &[u8]| f64::from(u16::from_be_bytes([frame[0], frame[1]])) / 65535.0 * 540.0;
    let mut patch = fixture("chauvet_intimidator_spot_110_12ch", 1);
    let unrotated = rig(json!([patch.clone()]));
    patch["matrix"] = json!([
        1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 900.0, -50.0, 3000.0
    ]);
    let moved = rig(json!([patch.clone()]));
    patch["matrix"] = json!(HALF_TURN);
    let turned = rig(json!([patch]));
    for pan in [0.0, 0.25, 0.5, 1.0] {
        let mut states = unrotated.empty_states();
        states.get_mut("test").unwrap().pan = pan;
        assert_eq!(
            moved.encode(&states).unwrap(),
            unrotated.encode(&states).unwrap()
        );
    }
    let mut states = turned.empty_states();
    states.get_mut("test").unwrap().pan = 0.5;
    assert!((degrees(&turned.encode(&states).unwrap()[&0]) - 450.0).abs() < 0.01);
}

#[test]
fn standing_head_reverses_movement_like_dmx_inversion_and_hanging_does_not() {
    const STANDING: [f64; 12] = [1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0];
    let spot = |matrix: [f64; 12], inverted: bool| {
        let mut patch = fixture("chauvet_intimidator_spot_110_12ch", 1);
        patch["matrix"] = json!(matrix);
        patch["pan"] = json!({"inverted": inverted});
        patch["tilt"] = json!({"inverted": inverted});
        rig(json!([patch]))
    };
    let standing = spot(STANDING, false);
    let hanging_inverted = spot(HALF_TURN, true);
    let hanging = spot(HALF_TURN, false);
    let standing_inverted = spot(STANDING, true);
    for (pan, tilt) in [(0.1, 0.2), (0.5, 0.5), (0.9, 0.7)] {
        let mut states = standing.empty_states();
        let state = states.get_mut("test").unwrap();
        (state.pan, state.tilt) = (pan, tilt);
        let encode = |rig: &FixtureRig| rig.encode(&states).unwrap()[&0][..4].to_vec();
        assert_eq!(encode(&standing), encode(&hanging_inverted));
        assert_eq!(encode(&standing_inverted), encode(&hanging));
    }
}

#[test]
fn rotated_fixed_fixtures_are_accepted_and_non_rotations_rejected() {
    let mut wash = fixture("generic_rgb", 1);
    wash["matrix"] = json!(HALF_TURN);
    assert!(accepts(json!({ "fixtures": [wash] })));
    for matrix in [
        json!([-1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]),
        json!([1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]),
        json!([1.0, 0.0, 0.0]),
    ] {
        let mut patch = fixture("chauvet_intimidator_spot_110_12ch", 1);
        patch["matrix"] = matrix;
        assert!(!accepts(json!({ "fixtures": [patch] })));
    }
}

#[test]
fn reference_patch_encodes_eight_independent_rgb_pixels_and_leaves_the_fx_derby_dark() {
    const STRIP_START: usize = 96;
    const RGB_CHANNELS: usize = 3;
    const PIXEL_COUNT: usize = 8;
    const FX_DERBY_START: usize = 39;
    const FX_DERBY_CHANNELS: usize = 4;
    let rig = support::room();
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
            &frame[FX_DERBY_START..FX_DERBY_START + FX_DERBY_CHANNELS],
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
    let rig = support::rig(json!({
        "fixtures":[
            {"id":"z", "profile":"generic_dimmer", "universe":0, "address":1},
            {"id":"a", "profile":"generic_dimmer", "universe":0, "address":2}
        ],
        "groups":{"wash":["a", "z"]}
    }));
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
        let definition = json!({ "fixtures": fixtures });
        assert!(!accepts(definition.clone()), "{definition}");
    }
    for members in [json!([]), json!(["test", "test"]), json!(["missing"])] {
        let definition = json!({"fixtures":[fixture("generic_rgb",1)],"groups":{"wash":members}});
        assert!(!accepts(definition));
    }
}

#[test]
fn patch_rejects_bad_calibration() {
    for bounds in [
        json!({"min":-0.1}),
        json!({"max":1.1}),
        json!({"min":0.8,"max":0.2}),
    ] {
        let mut patch = fixture("chauvet_intimidator_spot_110_12ch", 1);
        patch["pan"] = bounds;
        assert!(!accepts(json!({ "fixtures": [patch] })));
    }
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

fn pixel_bar(mode: &str, cells: Vec<wire::fixture::Cell>) -> wire::RigDefinition {
    wire::RigDefinition {
        fixtures: vec![wire::Fixture {
            area: wire::StageArea::Centre as i32,
            fixture_id: "bar".into(),
            profile_id: "bar".into(),
            address: 1,
            cells,
            ..Default::default()
        }],
        groups: vec![wire::FixtureGroup {
            group_id: "strip".into(),
            fixture_ids: vec!["bar".into()],
            ..Default::default()
        }],
        profiles: vec![support::compiled_profile(
            &format!("pixel_bar#{mode}"),
            "bar",
        )],
    }
}

fn cell(geometry: &str, offsets: &[u32]) -> wire::fixture::Cell {
    wire::fixture::Cell {
        geometry: geometry.into(),
        offsets: offsets.to_vec(),
    }
}

fn lit(intensity: f64, red: f64, green: f64, blue: f64) -> FixtureState {
    FixtureState {
        intensity,
        colour: Rgb { red, green, blue },
        ..FixtureState::default()
    }
}

#[test]
fn each_channel_owning_geometry_is_a_cell_encoded_from_its_own_state() {
    let layout = |mode| {
        GdtfProfile::cells_from_wire(
            support::compiled_profile(&format!("pixel_bar#{mode}"), "bar")
                .mode
                .as_ref()
                .unwrap(),
        )
        .unwrap()
        .into_iter()
        .map(|cell| (cell.geometry, cell.offsets))
        .collect::<Vec<_>>()
    };
    assert_eq!(
        layout("Pixels"),
        [
            ("Pixel 1".to_owned(), vec![1, 2, 3]),
            ("Pixel 2".to_owned(), vec![4, 5, 6])
        ]
    );
    assert_eq!(
        layout("Mastered"),
        [
            ("Base".to_owned(), vec![1]),
            ("Pixel 1".to_owned(), vec![2, 3, 4]),
            ("Pixel 2".to_owned(), vec![5, 6, 7])
        ]
    );

    let rig = FixtureRig::from_wire(&pixel_bar("Pixels", Vec::new())).unwrap();
    let cells = ["bar/Pixel 1", "bar/Pixel 2"];
    assert_eq!(rig.fixture_ids(), ["bar"]);
    assert_eq!(rig.cell_ids(), cells);
    assert_eq!(rig.resolve(&Target::Group("strip".into())).unwrap(), cells);
    assert_eq!(rig.resolve(&Target::Fixture("bar".into())).unwrap(), cells);
    assert_eq!(
        rig.resolve(&Target::Fixture("bar/Pixel 2".into())).unwrap(),
        ["bar/Pixel 2"]
    );
    let red = BTreeMap::from([("ColorAdd_R".to_owned(), AttributeValue::Number(0.5))]);
    rig.validate_attributes("bar/Pixel 1", &red).unwrap();
    let mut states = rig.empty_states();
    states.insert(cells[0].into(), lit(0.5, 1.0, 0.5, 0.0));
    states.insert(cells[1].into(), lit(1.0, 0.0, 0.0, 1.0));
    assert_eq!(
        &rig.encode(&states).unwrap()[&0][..7],
        [128, 64, 0, 0, 0, 255, 0]
    );
}

#[test]
fn a_master_dimmer_follows_its_pixels_so_the_bar_shows_what_it_would_without_one() {
    let mastered = FixtureRig::from_wire(&pixel_bar("Mastered", Vec::new())).unwrap();
    let pixels = ["bar/Pixel 1", "bar/Pixel 2"];
    assert_eq!(
        mastered.resolve(&Target::Group("strip".into())).unwrap(),
        pixels
    );
    let plain = FixtureRig::from_wire(&pixel_bar("Pixels", Vec::new())).unwrap();
    let net = |frame: &[u8]| -> Vec<f64> {
        frame[1..7]
            .iter()
            .map(|&byte| f64::from(frame[0]) * f64::from(byte) / 255.0)
            .collect()
    };
    let explicit = FixtureState {
        attributes: BTreeMap::from([("ColorAdd_G".to_owned(), AttributeValue::Number(1.0))]),
        attribute_brightness: 0.4,
        ..lit(0.1, 1.0, 0.0, 0.0)
    };
    for (first, second) in [
        (lit(0.25, 1.0, 0.5, 0.0), lit(0.5, 0.0, 0.0, 1.0)),
        (lit(1.0, 1.0, 1.0, 1.0), lit(0.0, 1.0, 0.0, 0.0)),
        (lit(0.0, 1.0, 0.0, 0.0), lit(0.0, 0.0, 1.0, 0.0)),
        (explicit.clone(), lit(0.2, 0.0, 0.0, 1.0)),
    ] {
        let mut states = mastered.empty_states();
        // The master cell's own state is ignored: its level comes from the pixels.
        states.insert("bar/Base".into(), lit(0.9, 1.0, 1.0, 1.0));
        states.insert(pixels[0].into(), first.clone());
        states.insert(pixels[1].into(), second.clone());
        let mut alone = plain.empty_states();
        alone.insert(pixels[0].into(), first);
        alone.insert(pixels[1].into(), second);
        let expected = plain.encode(&alone).unwrap()[&0][..6].to_vec();
        let shown = net(&mastered.encode(&states).unwrap()[&0][..7]);
        for (shown, expected) in shown.iter().zip(&expected) {
            assert!(
                (shown - f64::from(*expected)).abs() <= 1.5,
                "{shown:?} vs {expected:?}"
            );
        }
    }
    let mut states = mastered.empty_states();
    states.insert(pixels[0].into(), lit(0.25, 1.0, 0.5, 0.0));
    states.insert(pixels[1].into(), lit(0.5, 0.0, 0.0, 1.0));
    assert_eq!(
        &mastered.encode(&states).unwrap()[&0][..7],
        [128, 128, 64, 0, 0, 0, 255]
    );
}

#[test]
fn a_login_whose_declared_cells_differ_from_the_profile_is_refused() {
    let exact = vec![cell("Pixel 1", &[1, 2, 3]), cell("Pixel 2", &[4, 5, 6])];
    FixtureRig::from_wire(&pixel_bar("Pixels", exact)).unwrap();
    for declared in [
        vec![cell("Pixel 1", &[1, 2, 3])],
        vec![cell("Pixel 2", &[4, 5, 6]), cell("Pixel 1", &[1, 2, 3])],
        vec![cell("Pixel 1", &[1, 2]), cell("Pixel 2", &[3, 4, 5, 6])],
    ] {
        let error = FixtureRig::from_wire(&pixel_bar("Pixels", declared))
            .unwrap_err()
            .to_string();
        assert!(error.contains("declares cells"), "{error}");
    }
}

#[test]
fn exported_rigs_declare_the_cells_starfire_derives_and_the_bar_is_eight_strip_cells() {
    for exported in [
        include_bytes!("../../rig/room.rig.pb").as_slice(),
        include_bytes!("../../autolux/tests/data/generic_rig/generic.rig.pb"),
    ] {
        let definition = wire::RigDefinition::decode(exported).unwrap();
        assert!(
            definition
                .fixtures
                .iter()
                .all(|fixture| !fixture.cells.is_empty())
        );
        FixtureRig::from_wire(&definition).unwrap();
    }
    let room = support::room();
    assert_eq!(room.fixture_ids().len(), 8);
    assert_eq!(room.cell_ids().len(), 15);
    assert_eq!(
        room.resolve(&Target::Group("strip".into())).unwrap(),
        (1..=8)
            .map(|segment| format!("zq06017/Beam Segment {segment}"))
            .collect::<Vec<_>>()
    );
}
