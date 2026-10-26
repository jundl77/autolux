//! MVR rig files mapped onto the Login rig definition, so files and Login share one validator.
//! Spec conformance is checked by Autolux before saving; this reader extracts the patch.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Cursor, Read};

use anyhow::{Context, Result, bail, ensure};
use quick_xml::events::{BytesStart, Event};
use quick_xml::{Reader, XmlVersion};

use crate::gdtf::MAX_GDTF_XML_BYTES;
use crate::protocol::wire;

pub const SCENE_ENTRY: &str = "GeneralSceneDescription.xml";
pub const GDTF_DESCRIPTION: &str = "description.xml";
pub const MAX_MVR_BYTES: usize = 32 * 1024 * 1024;
const MAX_SCENE_BYTES: usize = 1024 * 1024;
const MAX_ARCHIVE_ENTRIES: usize = 4096;
const MAX_XML_NODES: usize = 65_536;
const MAX_XML_DEPTH: usize = 64;
const DMX_UNIVERSE_SIZE: u32 = 512;
const MAX_ARTNET_UNIVERSE: u32 = 32_767;
const MATRIX_VALUES: usize = 12;
const IDENTITY: [f64; MATRIX_VALUES] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0];
const AUTOLUX_PROVIDER: &str = "Autolux";
const OBJECT_TAGS: [&str; 8] = [
    "SceneObject",
    "GroupObject",
    "FocusPoint",
    "Fixture",
    "Support",
    "Truss",
    "VideoScreen",
    "Projector",
];

#[derive(Debug, Default)]
struct Element {
    name: String,
    attributes: BTreeMap<String, String>,
    text: String,
    children: Vec<Element>,
}

impl Element {
    fn child(&self, name: &str) -> Option<&Element> {
        self.children.iter().find(|child| child.name == name)
    }

    fn children<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a Element> {
        self.children.iter().filter(move |child| child.name == name)
    }

    fn attribute(&self, name: &str) -> &str {
        self.attributes.get(name).map_or("", String::as_str)
    }

    fn child_text(&self, name: &str) -> Option<&str> {
        self.child(name).map(|child| child.text.trim())
    }
}

fn element(start: &BytesStart) -> Result<Element> {
    let mut attributes = BTreeMap::new();
    for attribute in start.attributes() {
        let attribute = attribute.context("read MVR attribute")?;
        attributes.insert(
            String::from_utf8(attribute.key.as_ref().to_vec())?,
            attribute
                .normalized_value(XmlVersion::Explicit1_0)?
                .into_owned(),
        );
    }
    Ok(Element {
        name: String::from_utf8(start.name().as_ref().to_vec())?,
        attributes,
        ..Element::default()
    })
}

fn parse_xml(xml: &str) -> Result<Element> {
    let mut reader = Reader::from_str(xml);
    let mut stack: Vec<Element> = Vec::new();
    let mut nodes = 0;
    loop {
        let event = reader.read_event().context("read MVR scene XML")?;
        if matches!(event, Event::Start(_) | Event::Empty(_)) {
            nodes += 1;
            ensure!(
                nodes <= MAX_XML_NODES && stack.len() < MAX_XML_DEPTH,
                "MVR scene exceeds its node or depth limit"
            );
        }
        match event {
            Event::Start(start) => stack.push(element(&start)?),
            Event::Empty(start) => {
                let node = element(&start)?;
                match stack.last_mut() {
                    Some(parent) => parent.children.push(node),
                    None => return Ok(node),
                }
            }
            Event::End(_) => {
                let node = stack.pop().context("unbalanced MVR scene XML")?;
                match stack.last_mut() {
                    Some(parent) => parent.children.push(node),
                    None => return Ok(node),
                }
            }
            Event::Text(text) => {
                if let Some(node) = stack.last_mut() {
                    node.text.push_str(&text.decode()?);
                }
            }
            Event::GeneralRef(reference) => {
                let character = match reference.resolve_char_ref()? {
                    Some(character) => character,
                    None => match reference.decode()?.as_ref() {
                        "lt" => '<',
                        "gt" => '>',
                        "amp" => '&',
                        "apos" => '\'',
                        "quot" => '"',
                        other => bail!("MVR scene uses undefined entity {other}"),
                    },
                };
                if let Some(node) = stack.last_mut() {
                    node.text.push(character);
                }
            }
            Event::DocType(_) => bail!("MVR DTD and entity declarations are unsupported"),
            Event::Eof => bail!("MVR scene XML has no root element"),
            _ => {}
        }
    }
}

fn read_entry(archive: &[u8], name: &str, limit: usize, label: &str) -> Result<Vec<u8>> {
    ensure!(
        archive.len() <= MAX_MVR_BYTES,
        "{label} exceeds its size limit"
    );
    let mut package =
        zip::ZipArchive::new(Cursor::new(archive)).with_context(|| format!("open {label}"))?;
    ensure!(
        package.len() <= MAX_ARCHIVE_ENTRIES,
        "{label} contains too many entries"
    );
    let entry = package
        .by_name(name)
        .with_context(|| format!("{label} has no {name}"))?;
    ensure!(
        entry.size() <= limit as u64,
        "{label} {name} exceeds its size limit"
    );
    let mut content = Vec::new();
    entry
        .take(limit as u64 + 1)
        .read_to_end(&mut content)
        .with_context(|| format!("read {label} {name}"))?;
    ensure!(
        content.len() <= limit,
        "{label} {name} exceeds its size limit"
    );
    Ok(content)
}

/// Return (zero-based Art-Net universe, one-based address) from absolute or "u.a" text.
pub fn parse_address(text: &str) -> Result<(u32, u32)> {
    let text = text.trim();
    let invalid = || format!("invalid MVR DMX address {text:?}");
    let (universe, address) = match text.split_once('.') {
        Some((universe, address)) => (
            universe
                .parse::<u32>()
                .ok()
                .and_then(|universe| universe.checked_sub(1))
                .with_context(invalid)?,
            address.parse::<u32>().with_context(invalid)?,
        ),
        None => {
            let absolute = text.parse::<u32>().with_context(invalid)?;
            ensure!(absolute >= 1, "{}", invalid());
            (
                (absolute - 1) / DMX_UNIVERSE_SIZE,
                (absolute - 1) % DMX_UNIVERSE_SIZE + 1,
            )
        }
    };
    ensure!(
        universe <= MAX_ARTNET_UNIVERSE && (1..=DMX_UNIVERSE_SIZE).contains(&address),
        "MVR DMX address {text:?} is outside universes 1..32768, 1..512"
    );
    Ok((universe, address))
}

/// MVR Matrix text as twelve numbers: X, Y and Z axes, then position in millimetres.
pub fn parse_matrix(text: &str) -> Result<Vec<f64>> {
    let compact: String = text.chars().filter(|c| !c.is_whitespace()).collect();
    if compact.is_empty() {
        return Ok(Vec::new());
    }
    let rows = compact
        .strip_prefix('{')
        .and_then(|rest| rest.strip_suffix('}'))
        .context("MVR Matrix must contain four {x,y,z} rows")?
        .split("}{")
        .collect::<Vec<_>>();
    ensure!(
        rows.len() == 4,
        "MVR Matrix must contain four {{x,y,z}} rows"
    );
    let values = rows
        .iter()
        .flat_map(|row| row.split(','))
        .map(|value| {
            value
                .parse::<f64>()
                .context("MVR Matrix values must be numbers")
        })
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        values.len() == MATRIX_VALUES && values.iter().all(|value| value.is_finite()),
        "MVR Matrix must contain twelve finite numbers"
    );
    Ok(values)
}

fn movement_limits(root: &Element) -> Result<BTreeMap<(String, String), (f64, f64)>> {
    let mut limits = BTreeMap::new();
    let Some(user_data) = root.child("UserData") else {
        return Ok(limits);
    };
    for data in user_data
        .children("Data")
        .filter(|data| data.attribute("provider") == AUTOLUX_PROVIDER)
    {
        for movement in data.children("Movement") {
            let axis = movement.attribute("axis").to_ascii_lowercase();
            ensure!(
                axis == "pan" || axis == "tilt",
                "Autolux movement data must name Pan or Tilt"
            );
            let bound = |name, default: f64| -> Result<f64> {
                match movement.attributes.get(name) {
                    Some(value) => value
                        .parse()
                        .context("Autolux movement limits must be numbers"),
                    None => Ok(default),
                }
            };
            limits.insert(
                (movement.attribute("fixture").to_ascii_uppercase(), axis),
                (bound("minimum", 0.0)?, bound("maximum", 1.0)?),
            );
        }
    }
    Ok(limits)
}

fn invert_flag(fixture: &Element, name: &str) -> Result<bool> {
    match fixture.child_text(name).unwrap_or("false") {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => bail!("{name} must be true or false"),
    }
}

/// Express a child placement in its parent's space: R = Rp * Rc, o = Rp * oc + op.
pub fn multiply(
    parent: &[f64; MATRIX_VALUES],
    child: &[f64; MATRIX_VALUES],
) -> [f64; MATRIX_VALUES] {
    let mut result = [0.0; MATRIX_VALUES];
    for start in [0, 3, 6, 9] {
        for row in 0..3 {
            let rotated: f64 = (0..3)
                .map(|axis| parent[3 * axis + row] * child[start + axis])
                .sum();
            result[start + row] = rotated + if start == 9 { parent[9 + row] } else { 0.0 };
        }
    }
    result
}

fn local_matrix(node: &Element) -> Result<[f64; MATRIX_VALUES]> {
    let values = parse_matrix(node.child_text("Matrix").unwrap_or_default())?;
    Ok(values.try_into().unwrap_or(IDENTITY))
}

fn placed_fixtures<'a>(
    node: &'a Element,
    matrix: [f64; MATRIX_VALUES],
    placed: &mut Vec<(&'a Element, [f64; MATRIX_VALUES])>,
) -> Result<()> {
    for child in node
        .child("ChildList")
        .into_iter()
        .flat_map(|children| children.children.iter())
        .filter(|child| OBJECT_TAGS.contains(&child.name.as_str()))
    {
        let global = multiply(&matrix, &local_matrix(child)?);
        if child.name == "Fixture" {
            placed.push((child, global));
        }
        placed_fixtures(child, global, placed)?;
    }
    Ok(())
}

fn gdtf_description(archive: &[u8], spec: &str) -> Result<String> {
    let package = read_entry(archive, spec, MAX_MVR_BYTES, "MVR archive")?;
    String::from_utf8(read_entry(
        &package,
        GDTF_DESCRIPTION,
        MAX_GDTF_XML_BYTES,
        &format!("GDTF {spec}"),
    )?)
    .with_context(|| format!("GDTF {spec} description is not UTF-8"))
}

fn add_to_group(rig: &mut wire::RigDefinition, class: &str, name: &str) {
    match rig.groups.iter_mut().find(|group| group.group_id == class) {
        Some(group) => group.fixture_ids.push(name.into()),
        None => rig.groups.push(wire::FixtureGroup {
            group_id: class.into(),
            fixture_ids: vec![name.into()],
        }),
    }
}

/// Read an MVR archive into the Login rig. Every Fixture with a GDTFSpec and an Address is
/// patched wherever it is nested, with its matrix composed into scene space.
pub fn rig_definition(archive: &[u8]) -> Result<wire::RigDefinition> {
    let xml = String::from_utf8(read_entry(
        archive,
        SCENE_ENTRY,
        MAX_SCENE_BYTES,
        "MVR archive",
    )?)
    .context("MVR scene is not UTF-8")?;
    let root = parse_xml(&xml)?;
    ensure!(
        root.name == "GeneralSceneDescription" && root.attribute("verMajor") == "1",
        "MVR scene must be a GeneralSceneDescription with verMajor 1"
    );
    let scene = root.child("Scene").context("MVR scene has no Scene node")?;
    let classes: BTreeMap<String, &str> = scene
        .child("AUXData")
        .into_iter()
        .flat_map(|auxdata| auxdata.children("Class"))
        .map(|class| {
            (
                class.attribute("uuid").to_ascii_uppercase(),
                class.attribute("name"),
            )
        })
        .collect();
    let limits = movement_limits(&root)?;
    let mut placed = Vec::new();
    for layer in scene
        .child("Layers")
        .into_iter()
        .flat_map(|layers| layers.children("Layer"))
    {
        placed_fixtures(layer, local_matrix(layer)?, &mut placed)?;
    }

    let mut rig = wire::RigDefinition::default();
    let mut descriptions = BTreeMap::new();
    let mut profiles = BTreeSet::new();
    for (node, matrix) in placed {
        let spec = node.child_text("GDTFSpec").unwrap_or_default();
        let addresses: Vec<&Element> = node
            .child("Addresses")
            .into_iter()
            .flat_map(|addresses| addresses.children("Address"))
            .collect();
        if spec.is_empty() || addresses.is_empty() {
            continue; // Unpatched planning objects have nothing to render.
        }
        let name = node.attribute("name");
        let identity = node.attribute("uuid").to_ascii_uppercase();
        let fixture = (|| -> Result<wire::Fixture> {
            ensure!(
                addresses.len() == 1 && matches!(addresses[0].attribute("break"), "" | "0"),
                "Starfire renders one Address on break 0 per fixture"
            );
            let (universe, address) = parse_address(&addresses[0].text)?;
            let mode = node.child_text("GDTFMode").unwrap_or_default();
            ensure!(
                !mode.is_empty(),
                "GDTFMode is mandatory when GDTFSpec is defined"
            );
            if !descriptions.contains_key(spec) {
                descriptions.insert(spec.to_string(), gdtf_description(archive, spec)?);
            }
            let profile_id = format!("{spec}#{mode}");
            if profiles.insert(profile_id.clone()) {
                rig.profiles.push(wire::FixtureProfile {
                    profile_id: profile_id.clone(),
                    gdtf_xml: descriptions[spec].clone(),
                    dmx_mode: mode.into(),
                });
            }
            let classing = node
                .child_text("Classing")
                .unwrap_or_default()
                .to_ascii_uppercase();
            if !classing.is_empty() {
                let class = classes
                    .get(&classing)
                    .with_context(|| format!("Classing {classing} does not resolve to a Class"))?;
                add_to_group(&mut rig, class, name);
            }
            let range = |axis: &str, flag: &str| -> Result<Option<wire::fixture::MovementRange>> {
                let (minimum, maximum) = limits
                    .get(&(identity.clone(), axis.to_string()))
                    .copied()
                    .unwrap_or((0.0, 1.0));
                let inverted = invert_flag(node, flag)?;
                // Canonical form shared with cli/starfire_assets.py: defaults are omitted.
                Ok(((minimum, maximum) != (0.0, 1.0) || inverted).then_some(
                    wire::fixture::MovementRange {
                        minimum: Some(minimum),
                        maximum: Some(maximum),
                        inverted,
                    },
                ))
            };
            Ok(wire::Fixture {
                fixture_id: name.into(),
                profile_id,
                universe,
                address,
                pan: range("pan", "DMXInvertPan")?,
                tilt: range("tilt", "DMXInvertTilt")?,
                matrix: matrix.to_vec(),
            })
        })()
        .with_context(|| format!("MVR fixture {name}"))?;
        rig.fixtures.push(fixture);
    }
    Ok(rig)
}
