//! Independent SoundSwitch output values, sampled without Art-Net cadence error.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde::Deserialize;
use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, DMX_CHANNELS, Transition};
use starfire::script::SequenceCatalog;

const REFERENCE_SCHEMA_VERSION: u32 = 1;
const MAX_REPORTED_FAILURES: usize = 30;
const BITS_PER_DMX_CHANNEL: u32 = 8;

#[derive(Deserialize)]
struct Reference {
    schema_version: u32,
    bpm: f64,
    quantization_error_units: u32,
    continuous_properties: BTreeSet<String>,
    samples: Vec<Anchor>,
}

#[derive(Deserialize)]
struct Anchor {
    bank: String,
    sequence: String,
    source_elapsed_beats: f64,
    render_at_seconds: f64,
    expected_dmx_hex: String,
    maximum_errors_by_group: BTreeMap<String, u32>,
}

#[derive(Deserialize)]
struct ChannelGroup {
    channels: Vec<Vec<usize>>,
}

fn channel_value(frame: &[u8], component: &[usize]) -> u32 {
    assert!((1..=2).contains(&component.len()));
    component.iter().fold(0, |value, address| {
        assert!((1..=DMX_CHANNELS).contains(address));
        (value << BITS_PER_DMX_CHANNEL) | u32::from(frame[address - 1])
    })
}

#[test]
fn all_four_sequences_match_independent_sound_switch_value_anchors() {
    let reference: Reference =
        serde_json::from_str(include_str!("reference/soundswitch/value-anchors.json")).unwrap();
    let groups: BTreeMap<String, ChannelGroup> =
        serde_json::from_str(include_str!("reference/soundswitch/channel-groups.json")).unwrap();
    assert_eq!(reference.schema_version, REFERENCE_SCHEMA_VERSION);
    assert!(!reference.samples.is_empty());
    let rig = FixtureRig::from_json(include_str!("../../rigs/room.json")).unwrap();
    let catalog =
        SequenceCatalog::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("sequences")).unwrap();
    let mut engines = BTreeMap::new();
    let mut failures = Vec::new();
    for anchor in reference.samples {
        let engine = engines.entry(anchor.bank.clone()).or_insert_with(|| {
            let mut engine = Engine::new(rig.clone(), catalog.clone()).unwrap();
            engine
                .apply(
                    0.0,
                    &Command::Beat {
                        position_beats: 0.0,
                        bpm: reference.bpm,
                    },
                )
                .unwrap();
            engine
                .apply(
                    0.0,
                    &Command::Select {
                        bank: anchor.bank.clone(),
                        sequence: anchor.sequence.clone(),
                        transition: Transition::Cut,
                    },
                )
                .unwrap();
            engine
        });
        let rendered = engine.render_at(anchor.render_at_seconds).unwrap();
        assert!(
            rendered.script_error.is_none(),
            "{:?}",
            rendered.script_error
        );
        let actual = &rendered.dmx_frames_by_universe[&0];
        assert_eq!(anchor.expected_dmx_hex.len(), DMX_CHANNELS * 2);
        let expected: Vec<u8> = (0..anchor.expected_dmx_hex.len())
            .step_by(2)
            .map(|index| {
                u8::from_str_radix(&anchor.expected_dmx_hex[index..index + 2], 16).unwrap()
            })
            .collect();
        let mut covered = [false; DMX_CHANNELS];
        for (name, group) in &groups {
            let property = name.rsplit('.').next().unwrap();
            let limit = if reference.continuous_properties.contains(property) {
                *anchor
                    .maximum_errors_by_group
                    .get(name)
                    .unwrap_or(&reference.quantization_error_units)
            } else {
                0
            };
            for component in &group.channels {
                for address in component {
                    assert!(!covered[address - 1], "duplicate reference channel");
                    covered[address - 1] = true;
                }
                let target = channel_value(&expected, component);
                let value = channel_value(actual, component);
                if target.abs_diff(value) > limit {
                    failures.push(format!("{} beat {:.4} {name} {component:?}: expected {target}, got {value}, allowance {limit}",
                        anchor.bank, anchor.source_elapsed_beats));
                }
            }
        }
        for (index, is_covered) in covered.iter().enumerate() {
            if !is_covered {
                assert_eq!(
                    actual[index],
                    expected[index],
                    "unassigned channel {}",
                    index + 1
                );
            }
        }
    }
    assert_eq!(engines.len(), 4, "reference must cover all four banks");
    assert!(
        failures.is_empty(),
        "{} independent value mismatches:\n{}",
        failures.len(),
        failures
            .iter()
            .take(MAX_REPORTED_FAILURES)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
