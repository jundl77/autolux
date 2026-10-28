//! Real GDTF exports from the corpus, compiled by Autolux, encode as the fixture expects.

mod support;

use starfire::fixtures::FixtureRig;
use starfire::model::{AttributeValue, FixtureState, Rgb};
use starfire::protocol::wire;
use starfire::special::capabilities;

fn rig(source: &str) -> FixtureRig {
    FixtureRig::from_wire(&wire::RigDefinition {
        fixtures: vec![wire::Fixture {
            area: wire::StageArea::Centre as i32,
            fixture_uuid: "fixture".into(),
            name: "fixture".into(),
            profile_id: "profile".into(),
            address: 1,
            ..Default::default()
        }],
        groups: vec![wire::FixtureGroup {
            group_id: "group".into(),
            fixture_uuids: vec!["fixture".into()],
            ..Default::default()
        }],
        profiles: vec![support::compiled_profile(source, "profile")],
    })
    .unwrap()
}

fn frame(rig: &FixtureRig, state: FixtureState) -> Vec<u8> {
    let mut states = rig.empty_states();
    for value in states.values_mut() {
        *value = state.clone();
    }
    rig.encode(&states).unwrap()[&0].clone()
}

const RED: Rgb = Rgb {
    red: 1.0,
    green: 0.0,
    blue: 0.0,
};

#[test]
fn a_robe_mover_drives_16_bit_pan_tilt_cmy_mixing_wheels_and_dimmer() {
    let rig = rig("corpus/robe_bmfl_spot#Mode 2 - Reduced 8 bit");
    let output = frame(
        &rig,
        FixtureState {
            intensity: 1.0,
            colour: RED,
            pan: 0.25,
            tilt: 0.75,
            ..Default::default()
        },
    );
    assert_eq!(&output[..4], [149, 85, 191, 255], "16-bit pan and tilt");
    assert_eq!(&output[8..11], [0, 255, 255], "CMY subtracts to red");
    assert_eq!(output[32], 255, "dimmer");
    assert_eq!(output[31], 32, "shutter keeps its open default");
    let dark = frame(&rig, FixtureState::default());
    assert_eq!(dark[32], 0);
    let [group] = capabilities(&rig).try_into().unwrap();
    assert!(group.colour && group.speed && group.pulse && group.pattern);
}

#[test]
fn a_referenced_geometry_is_patched_at_its_break_offset() {
    let profile = support::compiled_profile("corpus/virtual_channel#Default", "profile");
    let channels = &profile.mode.as_ref().unwrap().channels;
    assert_eq!(channels[1].geometry_path, ["Body", "Reference 1"]);
    assert_eq!(channels[1].offsets, [2]);
    let rig = rig("corpus/virtual_channel#Default");
    let lit = FixtureState {
        intensity: 1.0,
        ..Default::default()
    };
    assert_eq!(frame(&rig, lit)[..2], [255, 255]);
}

#[test]
fn a_dmx_profile_curve_maps_light_output_to_its_dmx_value() {
    let rig = rig("curved_dimmer#8 bit");
    for (intensity, expected) in [(0.0, 0), (0.25, 127), (1.0, 255)] {
        let state = FixtureState {
            intensity,
            ..Default::default()
        };
        assert_eq!(frame(&rig, state)[0], expected, "{intensity}");
    }
    let quarter = FixtureState {
        attributes: [("Dimmer".to_owned(), AttributeValue::Number(25.0))].into(),
        ..Default::default()
    };
    assert_eq!(frame(&rig, quarter)[0], 127);
}
