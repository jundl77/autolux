//! The atmospheric `traverse` loop travels by MVR position, not by patch order.

mod support;

use std::collections::BTreeMap;

use starfire::fixtures::FixtureRig;
use starfire::model::{RenderContext, Target};
use starfire::protocol::wire;

const LOOP: (&str, &str) = ("atmospheric", "traverse");
const WASH: [&str; 4] = ["wash_left", "wash_mid_left", "wash_mid_right", "wash_right"];

/// Each target's dimmer at `beats` into the loop.
fn levels(rig: &FixtureRig, beats: f64) -> BTreeMap<String, f64> {
    let mut autoloop = support::show_catalog()
        .instantiate(LOOP.0, LOOP.1, rig, &support::palettes())
        .unwrap();
    let context = RenderContext {
        beat_position: beats,
        time_seconds: beats / 2.0,
        normalized_rms_level: Some(0.8),
        ..Default::default()
    };
    let palette = support::palettes().slots("techno").unwrap();
    autoloop
        .render(&context, &palette)
        .unwrap()
        .into_iter()
        .filter_map(|effect| match (effect.target, effect.values.intensity) {
            (Target::Fixture(id), Some(level)) => Some((id, level)),
            _ => None,
        })
        .collect()
}

fn brightest<'a>(levels: &BTreeMap<String, f64>, among: &[&'a str]) -> &'a str {
    among
        .iter()
        .copied()
        .max_by(|a, b| levels[*a].total_cmp(&levels[*b]))
        .unwrap()
}

#[test]
fn the_sweep_crosses_the_room_washes_house_left_to_house_right() {
    let room = support::room();
    let order: Vec<_> = [0.8, 3.0, 5.0, 7.2]
        .iter()
        .map(|&beat| brightest(&levels(&room, beat), &WASH))
        .collect();
    assert_eq!(order, WASH);
    // The movers follow: house left lit first, house right last.
    let early = levels(&room, 1.0);
    assert!(early["spot_left"] > early["spot_right"]);
    // The strip's cells share one position, so they travel in geometry order.
    let strip = levels(&room, 1.5);
    assert!(
        strip["zq06017/Beam Segment 1"] > strip["zq06017/Beam Segment 8"],
        "{strip:?}"
    );
}

#[test]
fn the_mirrored_half_lights_both_sides_at_once_then_closes_in() {
    let room = support::room();
    let outer = levels(&room, 9.0);
    assert!((outer["wash_left"] - outer["wash_right"]).abs() < 1e-12);
    assert!(outer["wash_left"] > outer["wash_mid_left"]);
    let inner = levels(&room, 14.0);
    assert!((inner["wash_mid_left"] - inner["wash_mid_right"]).abs() < 1e-12);
    assert!(inner["wash_mid_left"] > inner["wash_left"]);
}

#[test]
fn a_lone_fixture_holds_still() {
    let rig = support::rig(
        serde_json::from_str(
            r#"{"schema_version":1,"fixtures":[
          {"id":"solo","profile":"generic_rgb","universe":0,"address":1}],
          "groups":{"wash":["solo"]}}"#,
        )
        .unwrap(),
    );
    let still: Vec<_> = (0..16)
        .map(|beat| levels(&rig, f64::from(beat))["solo"])
        .collect();
    assert!(still.windows(2).all(|pair| pair[0] == pair[1]), "{still:?}");
}

/// 400 washes on a 40 m truss, unevenly spaced and patched in shuffled order.
fn truss() -> FixtureRig {
    let profile = support::catalogue_profile("generic_rgb").unwrap();
    let mut rig = wire::RigDefinition {
        profiles: vec![profile],
        ..Default::default()
    };
    let count = 400;
    let mut members = Vec::new();
    for rank in 0..count {
        let index = (rank * 7) % count;
        let along = index as f64 / (count - 1) as f64;
        let x = -20000.0 + 40000.0 * along * along.sqrt();
        let id = format!("w{index:03}");
        rig.fixtures.push(wire::Fixture {
            fixture_id: id.clone(),
            profile_id: "generic_rgb".into(),
            universe: (rank / 170) as u32,
            address: ((rank % 170) * 3 + 1) as u32,
            matrix: vec![
                1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, x, -3000.0, 6000.0,
            ],
            ..Default::default()
        });
        members.push(id);
    }
    rig.groups.push(wire::FixtureGroup {
        group_id: "wash".into(),
        fixture_ids: members,
        ..Default::default()
    });
    FixtureRig::from_wire(&rig).unwrap()
}

#[test]
fn on_a_400_fixture_truss_the_peak_travels_monotonically_along_x() {
    let rig = truss();
    let names: Vec<_> = (0..400).map(|index| format!("w{index:03}")).collect();
    let mut last_x = f64::MIN;
    for step in 0..=14 {
        let beat = 0.5 + step as f64 * 0.5;
        let levels = levels(&rig, beat);
        let peak = names
            .iter()
            .max_by(|a, b| levels[*a].total_cmp(&levels[*b]))
            .unwrap();
        let x = rig.spatial().position(peak).unwrap()[0];
        // The front enters off the house-left edge, so the first frames may share a peak.
        assert!(
            x >= last_x,
            "beat {beat}: peak {peak} at {x} after {last_x}"
        );
        last_x = x;
    }
    assert!(last_x > 15000.0, "{last_x}");
}
