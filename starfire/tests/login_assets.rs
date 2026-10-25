use std::path::Path;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, FixtureState, Rgb, Transition};
use starfire::protocol::{ClientMessage, apply_command, wire};
use starfire::script::AutoloopCatalog;

const FRAME_RATE_HZ: u32 = 40;
const RGB_RIG: &str = r#"{"schema_version":1,"fixtures":[{"id":"wash","profile":"generic_rgb","universe":0,"address":1},{"id":"mover","profile":"generic_rgb","universe":0,"address":4},{"id":"pixel","profile":"generic_rgb","universe":0,"address":7}],"groups":{"wash":["wash"],"movers":["mover"],"strip":["pixel"]}}"#;

fn rig() -> wire::RigDefinition {
    wire::RigDefinition {
        fixtures: ["wash", "mover", "pixel"]
            .into_iter()
            .enumerate()
            .map(|(index, id)| wire::Fixture {
                fixture_id: id.into(),
                profile_id: "generic_rgb".into(),
                address: 1 + index as u32 * 3,
                ..Default::default()
            })
            .collect(),
        groups: [("wash", "wash"), ("movers", "mover"), ("strip", "pixel")]
            .into_iter()
            .map(|(group, id)| wire::FixtureGroup {
                group_id: group.into(),
                fixture_ids: vec![id.into()],
            })
            .collect(),
        profiles: vec![profile("generic_rgb")],
    }
}

fn profile(id: &str) -> wire::FixtureProfile {
    let profiles: serde_json::Value =
        serde_json::from_str(include_str!("../../rigs/profiles.json")).unwrap();
    let value = &profiles["profiles"][id];
    wire::FixtureProfile {
        profile_id: id.into(),
        gdtf_xml: value["gdtf_xml"].as_str().unwrap().into(),
        dmx_mode: value["dmx_mode"].as_str().unwrap().into(),
    }
}

#[test]
fn supplied_rig_and_scripts_match_file_loaded_dmx_and_repeated_selection_keeps_phase() {
    let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloop");
    let offline_catalog = AutoloopCatalog::load(&directory).unwrap();
    let scripts: Vec<_> = offline_catalog
        .list()
        .into_iter()
        .enumerate()
        .map(|(index, autoloop)| wire::AutoloopDefinition {
            autoloop_id: format!("opaque-{index}"),
            lua_source: std::fs::read_to_string(
                directory
                    .join(&autoloop.bank)
                    .join(format!("{}.lua", autoloop.autoloop)),
            )
            .unwrap(),
            bank: autoloop.bank,
        })
        .collect();
    let supplied_catalog = AutoloopCatalog::from_definitions(&scripts).unwrap();
    let supplied_rig = FixtureRig::from_wire(&rig()).unwrap();
    for (script, autoloop) in scripts.iter().zip(offline_catalog.list()) {
        let mut supplied = Engine::new(supplied_rig.clone(), supplied_catalog.clone()).unwrap();
        let mut offline = Engine::new(
            FixtureRig::from_json(RGB_RIG).unwrap(),
            offline_catalog.clone(),
        )
        .unwrap();
        for engine in [&mut supplied, &mut offline] {
            engine
                .apply(
                    0.0,
                    &Command::Beat {
                        position_beats: 0.0,
                        bpm: 140.0,
                    },
                )
                .unwrap();
        }
        let selection = ClientMessage::PlayAutoloop(wire::PlayAutoloop {
            autoloop_id: script.autoloop_id.clone(),
            ..Default::default()
        });
        apply_command(&mut supplied, 0.0, &selection).unwrap();
        offline
            .apply(
                0.0,
                &Command::Select {
                    bank: autoloop.bank,
                    autoloop: autoloop.autoloop,
                    transition: Transition::Cut,
                },
            )
            .unwrap();
        for index in 0..FRAME_RATE_HZ * 2 {
            let seconds = f64::from(index) / f64::from(FRAME_RATE_HZ);
            if index == FRAME_RATE_HZ {
                apply_command(&mut supplied, seconds, &selection).unwrap();
            }
            let expected = offline.render_at(seconds).unwrap();
            let actual = supplied.render_at(seconds).unwrap();
            assert!(actual.script_error.is_none(), "{:?}", actual.script_error);
            assert_eq!(
                actual.dmx_frames_by_universe,
                expected.dmx_frames_by_universe
            );
            assert_eq!(
                actual.fixture_properties_before_strobe,
                expected.fixture_properties_before_strobe
            );
        }
    }
}

#[test]
fn typed_patch_rejects_overlaps_unknown_groups_and_ambiguous_profiles() {
    let mut candidates = Vec::new();
    let mut value = rig();
    value.fixtures[1].address = 2;
    candidates.push(value);
    let mut value = rig();
    value.fixtures[0].universe = u32::MAX;
    candidates.push(value);
    let mut value = rig();
    value.fixtures[0].profile_id = "missing".into();
    candidates.push(value);
    let mut value = rig();
    value.groups[0].fixture_ids = vec!["missing".into()];
    candidates.push(value);
    let mut value = rig();
    value.groups.push(value.groups[0].clone());
    candidates.push(value);
    let mut value = rig();
    value.profiles.push(value.profiles[0].clone());
    candidates.push(value);
    let mut value = rig();
    value.profiles[0].gdtf_xml = value.profiles[0]
        .gdtf_xml
        .replace("Offset=\"2\"", "Offset=\"1\"");
    candidates.push(value);
    let mut value = rig();
    value.profiles[0].dmx_mode = "missing mode".into();
    candidates.push(value);
    let mut value = rig();
    value.fixtures[0].pan = Some(wire::fixture::MovementRange {
        minimum: Some(f64::NAN),
        ..Default::default()
    });
    candidates.push(value);
    for invalid in candidates {
        assert!(FixtureRig::from_wire(&invalid).is_err(), "{invalid:?}");
    }
}

#[test]
fn typed_mover_encoding_preserves_safe_channels_and_applies_calibration() {
    let mut fixture = rig();
    fixture.fixtures.truncate(1);
    fixture.groups.clear();
    fixture.fixtures[0].pan = Some(wire::fixture::MovementRange {
        minimum: Some(0.25),
        maximum: Some(0.75),
        inverted: true,
    });
    fixture.profiles[0] = profile("chauvet_intimidator_spot_110_12ch");
    fixture.fixtures[0].profile_id = fixture.profiles[0].profile_id.clone();
    let rig = FixtureRig::from_wire(&fixture).unwrap();
    let mut states = rig.empty_states();
    states.insert(
        "wash".into(),
        FixtureState {
            intensity: 0.5,
            pan: 0.0,
            colour: Rgb {
                red: 0.0,
                green: 0.0,
                blue: 1.0,
            },
            ..Default::default()
        },
    );
    assert_eq!(
        &rig.encode(&states).unwrap()[&0][..12],
        &[191, 255, 128, 0, 0, 111, 7, 128, 0, 0, 0, 0]
    );
    fixture.profiles[0].gdtf_xml = fixture.profiles[0]
        .gdtf_xml
        .replace("WheelSlotIndex=\"1\"", "WheelSlotIndex=\"999\"");
    assert!(FixtureRig::from_wire(&fixture).is_err());
}

#[test]
fn supplied_catalog_requires_unique_ids_and_bounded_source() {
    let script = wire::AutoloopDefinition {
        autoloop_id: "look".into(),
        bank: "atmospheric".into(),
        lua_source: "return {render=function(ctx) return sf.scene {} end}".into(),
    };
    let mut other_bank = script.clone();
    other_bank.bank = "drop".into();
    assert!(AutoloopCatalog::from_definitions(&[script.clone(), other_bank]).is_err());
    assert!(AutoloopCatalog::from_definitions(&[]).is_err());
    for invalid in [
        wire::AutoloopDefinition {
            autoloop_id: String::new(),
            ..script.clone()
        },
        wire::AutoloopDefinition {
            autoloop_id: "look\n".into(),
            ..script.clone()
        },
        wire::AutoloopDefinition {
            bank: "invalid bank".into(),
            ..script.clone()
        },
        wire::AutoloopDefinition {
            lua_source: " ".repeat(128 * 1024 + 1),
            ..script
        },
    ] {
        assert!(AutoloopCatalog::from_definitions(&[invalid]).is_err());
    }
}
