#![allow(dead_code)]

use std::path::{Path, PathBuf};

use prost::Message;
use serde_json::Value;
use starfire::fixtures::FixtureRig;
use starfire::palette::{PaletteDefinition, PaletteTable};
use starfire::protocol::wire;

pub fn palette_definitions() -> Vec<PaletteDefinition> {
    serde_json::from_str(include_str!("../palettes.json")).unwrap()
}

pub fn palettes() -> PaletteTable {
    PaletteTable::new(&palette_definitions()).unwrap()
}

pub fn rig_folder() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../rig")
}

pub fn room() -> FixtureRig {
    FixtureRig::load(&rig_folder().join("room.rig.pb")).unwrap()
}

/// A shipped fixture type's GDTF description and mode, as the exported rigs carry them.
pub fn catalogue_profile(fixture_type: &str) -> Option<(String, String)> {
    let (_, spec) = SHIPPED_TYPES
        .iter()
        .find(|(name, _)| *name == fixture_type)?;
    [
        sample_rig().join("generic.rig.pb"),
        rig_folder().join("room.rig.pb"),
    ]
    .into_iter()
    .find_map(|file| {
        let rig = wire::RigDefinition::decode(std::fs::read(file).unwrap().as_slice()).unwrap();
        rig.profiles
            .into_iter()
            .find(|profile| profile.profile_id.split_once('#').map(|(file, _)| file) == Some(spec))
            .map(|profile| (profile.gdtf_xml, profile.dmx_mode))
    })
}

/// The short profile names these tests patch, and the GDTFSpec each one is shipped as.
const SHIPPED_TYPES: [(&str, &str); 6] = [
    ("generic_dimmer", "Generic@Dimmer@1.0.gdtf"),
    ("generic_rgb", "Generic@RGB@1.0.gdtf"),
    ("generic_rgbw", "Generic@RGBW@1.0.gdtf"),
    (
        "chauvet_intimidator_spot_110_12ch",
        "Chauvet DJ@Intimidator Spot 110@1.0.gdtf",
    ),
    (
        "chauvet_mini_kinta_irc_4ch",
        "Chauvet DJ@Mini Kinta IRC@1.0.gdtf",
    ),
    (
        "fun_generation_separ_quad_uv_8ch",
        "Fun Generation@SePar Quad LED RGB UV IR@1.0.gdtf",
    ),
];

/// The synthetic sample rig, kept with the test data rather than the venue rigs.
pub fn sample_rig() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../autolux/tests/data/generic_rig")
}

fn number(fixture: &Value, key: &str) -> u32 {
    u32::try_from(fixture[key].as_u64().unwrap()).unwrap()
}

fn matrix(fixture: &Value) -> Vec<f64> {
    fixture.get("matrix").map_or_else(Vec::new, |matrix| {
        matrix
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_f64().unwrap())
            .collect()
    })
}

pub fn definition(spec: &Value) -> wire::RigDefinition {
    let mut rig = wire::RigDefinition::default();
    for fixture in spec["fixtures"].as_array().unwrap() {
        let profile = fixture["profile"].as_str().unwrap();
        let range = |axis: &str| {
            fixture.get(axis).map(|range| wire::fixture::MovementRange {
                minimum: range.get("min").and_then(Value::as_f64),
                maximum: range.get("max").and_then(Value::as_f64),
                inverted: range.get("inverted").and_then(Value::as_bool) == Some(true),
            })
        };
        rig.fixtures.push(wire::Fixture {
            fixture_id: fixture["id"].as_str().unwrap().into(),
            profile_id: profile.into(),
            universe: number(fixture, "universe"),
            address: number(fixture, "address"),
            pan: range("pan"),
            tilt: range("tilt"),
            matrix: matrix(fixture),
        });
        if !rig.profiles.iter().any(|known| known.profile_id == profile)
            && let Some((gdtf_xml, dmx_mode)) = catalogue_profile(profile)
        {
            rig.profiles.push(wire::FixtureProfile {
                profile_id: profile.into(),
                gdtf_xml,
                dmx_mode,
            });
        }
    }
    if let Some(groups) = spec.get("groups").and_then(Value::as_object) {
        for (group, members) in groups {
            rig.groups.push(wire::FixtureGroup {
                group_id: group.clone(),
                fixture_ids: members
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|member| member.as_str().unwrap().into())
                    .collect(),
            });
        }
    }
    rig
}

pub fn try_rig(spec: Value) -> anyhow::Result<FixtureRig> {
    FixtureRig::from_wire(&definition(&spec))
}

pub fn rig(spec: Value) -> FixtureRig {
    try_rig(spec).unwrap()
}

pub fn room_definition() -> wire::RigDefinition {
    wire::RigDefinition::decode(
        std::fs::read(rig_folder().join("room.rig.pb"))
            .unwrap()
            .as_slice(),
    )
    .unwrap()
}

pub fn encoded(spec: &Value) -> Vec<u8> {
    definition(spec).encode_to_vec()
}

pub fn write_rig(path: &Path, spec: &Value) {
    std::fs::write(path, encoded(spec)).unwrap();
}
