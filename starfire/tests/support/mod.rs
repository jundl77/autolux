#![allow(dead_code)]

use std::path::{Path, PathBuf};

use prost::Message;
use serde_json::Value;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, SectionCue, Transition};
use starfire::palette::{PaletteDefinition, PaletteTable};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

pub fn palette_definitions() -> Vec<PaletteDefinition> {
    serde_json::from_str(include_str!("../palettes.json")).unwrap()
}

pub fn palettes() -> PaletteTable {
    PaletteTable::new(&palette_definitions()).unwrap()
}

pub fn stage_rig() -> PathBuf {
    sample_rig().join("stage.rig.pb")
}

pub fn stage() -> FixtureRig {
    FixtureRig::load(&stage_rig()).unwrap()
}

pub fn show_catalog() -> AutoloopCatalog {
    AutoloopCatalog::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../show/autoloops")).unwrap()
}

pub fn test_catalog(scripts: &[(&str, &str)]) -> AutoloopCatalog {
    AutoloopCatalog::from_definitions(
        &scripts
            .iter()
            .map(|(autoloop_id, source)| wire::AutoloopDefinition {
                autoloop_id: (*autoloop_id).into(),
                bank: "test".into(),
                lua_source: (*source).into(),
                intensity_htp: false,
            })
            .collect::<Vec<_>>(),
    )
    .unwrap()
}

pub fn directory_catalog(bank: &str, scripts: &[(&str, &str)]) -> anyhow::Result<AutoloopCatalog> {
    let directory = tempfile::tempdir().unwrap();
    let bank_directory = directory.path().join(bank);
    std::fs::create_dir(&bank_directory).unwrap();
    for (name, source) in scripts {
        std::fs::write(bank_directory.join(format!("{name}.lua")), source).unwrap();
    }
    AutoloopCatalog::load(directory.path())
}

pub fn beat(beat_index: u64, bar_phase: Option<u32>, bpm: f64) -> Command {
    Command::Beat {
        beat_index,
        bar_phase,
        beats_per_bar: 4,
        bpm,
        features: None,
    }
}

pub fn select(bank: &str, autoloop: &str, transition: Transition) -> Command {
    Command::Select {
        bank: bank.into(),
        autoloop: autoloop.into(),
        transition,
        palette: "techno".into(),
    }
}

pub fn cue(cue: SectionCue) -> Command {
    Command::CueSection {
        value: Box::new(cue),
    }
}

pub fn mounted(hanging: bool, yaw: f64, position: [f64; 3]) -> Vec<f64> {
    let (sin, cos) = yaw.to_radians().sin_cos();
    let flip = if hanging { 1.0 } else { -1.0 };
    [
        [flip * cos, flip * sin, 0.0],
        [-sin, cos, 0.0],
        [0.0, 0.0, flip],
        position,
    ]
    .concat()
}

/// Decoded independently of the encoder: physical pan 0 faces local +Y and turns clockwise about
/// local +Z, mid tilt runs along local -Z and leans towards the pan heading.
pub fn beam_through(matrix: &[f64], pan_radians: f64, tilt_radians: f64) -> [f64; 3] {
    let (pan, tilt) = (pan_radians, tilt_radians);
    let body = [tilt.sin() * pan.sin(), tilt.sin() * pan.cos(), -tilt.cos()];
    [0, 1, 2].map(|axis| (0..3).map(|row| matrix[3 * row + axis] * body[row]).sum())
}

/// `source` is `<file stem>#<mode>`; regenerate with `python tools/compile_test_profiles.py`.
pub fn compiled_profile(source: &str, profile_id: &str) -> wire::FixtureProfile {
    let rig =
        wire::RigDefinition::decode(include_bytes!("../data/compiled_profiles.pb").as_slice())
            .unwrap();
    let mut profile = rig
        .profiles
        .into_iter()
        .find(|profile| profile.profile_id == source)
        .unwrap_or_else(|| panic!("no compiled test profile {source}"));
    profile.profile_id = profile_id.into();
    profile
}

pub fn catalogue_profile(fixture_type: &str) -> Option<wire::FixtureProfile> {
    let (_, spec) = SHIPPED_TYPES
        .iter()
        .find(|(name, _)| *name == fixture_type)?;
    [sample_rig().join("generic.rig.pb"), stage_rig()]
        .into_iter()
        .find_map(|file| {
            let rig = wire::RigDefinition::decode(std::fs::read(file).unwrap().as_slice()).unwrap();
            rig.profiles
                .into_iter()
                .find(|profile| {
                    profile.profile_id.split_once('#').map(|(file, _)| file) == Some(spec)
                })
                .map(|profile| wire::FixtureProfile {
                    profile_id: fixture_type.into(),
                    ..profile
                })
        })
}

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

pub fn sample_rig() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../autolux/tests/data/generic_rig")
}

fn number(fixture: &Value, key: &str) -> u32 {
    u32::try_from(fixture[key].as_u64().unwrap()).unwrap()
}

fn numbers(fixture: &Value, key: &str) -> Vec<f64> {
    fixture.get(key).map_or_else(Vec::new, |matrix| {
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
        let uuid = fixture["fixture_uuid"].as_str().unwrap();
        rig.fixtures.push(wire::Fixture {
            fixture_uuid: uuid.into(),
            name: fixture
                .get("name")
                .map_or(uuid, |name| name.as_str().unwrap())
                .into(),
            profile_id: profile.into(),
            universe: number(fixture, "universe"),
            address: number(fixture, "address"),
            pan: range("pan"),
            tilt: range("tilt"),
            matrix: numbers(fixture, "matrix"),
            focus: numbers(fixture, "focus"),
            cells: Vec::new(),
            area: wire::StageArea::from_str_name(&format!(
                "STAGE_AREA_{}",
                fixture
                    .get("area")
                    .map_or("CENTRE", |area| area.as_str().unwrap())
            ))
            .unwrap() as i32,
        });
        if !rig.profiles.iter().any(|known| known.profile_id == profile)
            && let Some(found) = catalogue_profile(profile)
        {
            rig.profiles.push(found);
        }
    }
    if let Some(groups) = spec.get("groups").and_then(Value::as_object) {
        for (group, members) in groups {
            rig.groups.push(wire::FixtureGroup {
                group_id: group.clone(),
                fixture_uuids: members
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|member| member.as_str().unwrap().into())
                    .collect(),
                ..Default::default()
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

pub fn stage_uuid(name: &str) -> String {
    stage().uuid_of_label(name).unwrap().to_owned()
}

pub fn stage_definition() -> wire::RigDefinition {
    wire::RigDefinition::decode(std::fs::read(stage_rig()).unwrap().as_slice()).unwrap()
}

pub fn stage_definition_as_loops() -> wire::RigDefinition {
    let mut definition = stage_definition();
    for group in &mut definition.groups {
        group.role = wire::GroupRole::Loop as i32;
    }
    definition
}

pub fn encoded(spec: &Value) -> Vec<u8> {
    definition(spec).encode_to_vec()
}

pub fn write_rig(path: &Path, spec: &Value) {
    std::fs::write(path, encoded(spec)).unwrap();
}
