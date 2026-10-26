#![allow(dead_code)]

use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};

use serde_json::Value;
use starfire::fixtures::FixtureRig;
use starfire::protocol::wire;

pub const PROFILES: &str = include_str!("../../../rigs/profiles.json");

pub fn rigs() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../rigs")
}

pub fn room() -> FixtureRig {
    FixtureRig::load(&rigs().join("room.mvr")).unwrap()
}

fn catalogue_profile(profile: &str) -> Option<(String, String)> {
    let catalogue: Value = serde_json::from_str(PROFILES).unwrap();
    let source = catalogue["profiles"].get(profile)?;
    Some((
        source["gdtf_xml"].as_str().unwrap().into(),
        source["dmx_mode"].as_str().unwrap().into(),
    ))
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

pub fn zipped(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut package = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, content) in entries {
        package
            .start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        package.write_all(content).unwrap();
    }
    package.finish().unwrap().into_inner()
}

pub fn room_entries() -> (String, Vec<(String, Vec<u8>)>) {
    let mut package =
        zip::ZipArchive::new(Cursor::new(std::fs::read(rigs().join("room.mvr")).unwrap())).unwrap();
    let mut scene = String::new();
    let mut others = Vec::new();
    for index in 0..package.len() {
        let mut entry = package.by_index(index).unwrap();
        let mut content = Vec::new();
        std::io::Read::read_to_end(&mut entry, &mut content).unwrap();
        if entry.name() == "GeneralSceneDescription.xml" {
            scene = String::from_utf8(content).unwrap();
        } else {
            others.push((entry.name().to_string(), content));
        }
    }
    (scene, others)
}

pub fn room_with_scene(scene_xml: &str) -> Vec<u8> {
    let (_, others) = room_entries();
    let mut entries: Vec<(&str, &[u8])> =
        vec![("GeneralSceneDescription.xml", scene_xml.as_bytes())];
    entries.extend(
        others
            .iter()
            .map(|(name, content)| (name.as_str(), content.as_slice())),
    );
    zipped(&entries)
}

pub fn archive(scene_xml: &str) -> Vec<u8> {
    let catalogue: Value = serde_json::from_str(PROFILES).unwrap();
    let packages: Vec<(String, Vec<u8>)> = catalogue["profiles"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(profile, source)| {
            let description = source["gdtf_xml"].as_str().unwrap().as_bytes();
            (
                format!("{profile}.gdtf"),
                zipped(&[("description.xml", description)]),
            )
        })
        .collect();
    let mut entries: Vec<(&str, &[u8])> =
        vec![("GeneralSceneDescription.xml", scene_xml.as_bytes())];
    entries.extend(
        packages
            .iter()
            .map(|(name, content)| (name.as_str(), content.as_slice())),
    );
    zipped(&entries)
}

pub fn scene_xml(spec: &Value) -> String {
    let groups = spec
        .get("groups")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let class_uuid = |index: usize| format!("00000000-0000-4000-8000-{:012X}", index + 1);
    let mut xml = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <GeneralSceneDescription verMajor=\"1\" verMinor=\"6\"><Scene><AUXData>",
    );
    for (index, group) in groups.keys().enumerate() {
        xml += &format!("<Class name=\"{group}\" uuid=\"{}\"/>", class_uuid(index));
    }
    xml += "</AUXData><Layers><Layer name=\"Room\" uuid=\"00000000-0000-4000-A000-000000000001\"><ChildList>";
    for (index, fixture) in spec["fixtures"].as_array().unwrap().iter().enumerate() {
        let id = fixture["id"].as_str().unwrap();
        let profile = fixture["profile"].as_str().unwrap();
        let mode = catalogue_profile(profile).map_or_else(String::new, |(_, mode)| mode);
        xml += &format!(
            "<Fixture name=\"{id}\" uuid=\"00000000-0000-4000-9000-{:012X}\">",
            index + 1
        );
        let values = matrix(fixture);
        if !values.is_empty() {
            xml += "<Matrix>";
            for row in values.chunks(3) {
                xml += &format!("{{{},{},{}}}", row[0], row[1], row[2]);
            }
            xml += "</Matrix>";
        }
        if let Some(group) = groups.iter().position(|(_, members)| {
            members
                .as_array()
                .unwrap()
                .iter()
                .any(|member| member == id)
        }) {
            xml += &format!("<Classing>{}</Classing>", class_uuid(group));
        }
        xml += &format!(
            "<GDTFSpec>{profile}.gdtf</GDTFSpec><GDTFMode>{mode}</GDTFMode>\
             <Addresses><Address break=\"0\">{}</Address></Addresses></Fixture>",
            number(fixture, "universe") * 512 + number(fixture, "address")
        );
    }
    xml + "</ChildList></Layer></Layers></Scene></GeneralSceneDescription>"
}

pub fn mvr(spec: &Value) -> Vec<u8> {
    archive(&scene_xml(spec))
}

pub fn write_mvr(path: &Path, spec: &Value) {
    std::fs::write(path, mvr(spec)).unwrap();
}
