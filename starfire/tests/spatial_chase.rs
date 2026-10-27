//! The atmospheric `traverse` loop travels by MVR position, not by patch order.

mod support;

use std::collections::BTreeMap;

use starfire::fixtures::FixtureRig;
use starfire::model::RenderContext;
use starfire::protocol::wire;

const LOOP: (&str, &str) = ("atmospheric", "traverse");
const WASH: [&str; 4] = ["wash_left", "wash_mid_left", "wash_mid_right", "wash_right"];
/// starfire::script's MAX_EFFECTS_PER_FRAME, a detector the loop must stay well inside.
const EFFECT_CAP: usize = 512;
const SEAM_TOLERANCE: f64 = 1e-3;

struct Frame {
    levels: BTreeMap<String, f64>,
    effects: usize,
}

/// Each cell's dimmer at `beats` into the loop, later effects winning as in the engine.
fn render(rig: &FixtureRig, beats: f64) -> Frame {
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
    let effects = autoloop.render(&context, &palette).unwrap();
    let mut levels = BTreeMap::new();
    for effect in &effects {
        if let Some(level) = effect.values.intensity {
            for cell in rig.resolve(&effect.target).unwrap() {
                levels.insert(cell, level);
            }
        }
    }
    Frame {
        levels,
        effects: effects.len(),
    }
}

fn levels(rig: &FixtureRig, beats: f64) -> BTreeMap<String, f64> {
    render(rig, beats).levels
}

fn brightest<'a>(levels: &BTreeMap<String, f64>, among: &[&'a str]) -> &'a str {
    among
        .iter()
        .copied()
        .max_by(|a, b| levels[*a].total_cmp(&levels[*b]))
        .unwrap()
}

/// The half-way and loop seams are continuous: no level jumps.
fn assert_seamless(rig: &FixtureRig) {
    for (before, after) in [(7.999, 8.0), (15.999, 0.0)] {
        let (before, after) = (levels(rig, before), levels(rig, after));
        for (cell, level) in &before {
            let jump = (level - after[cell]).abs();
            assert!(jump < SEAM_TOLERANCE, "{cell} jumps {jump}");
        }
    }
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
    // The strip's cells have no X spread between them, so they travel in geometry order.
    let strip = levels(&room, 1.5);
    assert!(
        strip["zq06017/Beam Segment 1"] > strip["zq06017/Beam Segment 8"],
        "{strip:?}"
    );
    assert_seamless(&room);
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
fn a_lone_fixture_rises_and_falls_once_per_half() {
    let rig = support::rig(
        serde_json::from_str(
            r#"{"schema_version":1,"fixtures":[
          {"id":"solo","profile":"generic_rgb","universe":0,"address":1}],
          "groups":{"wash":["solo"]}}"#,
        )
        .unwrap(),
    );
    let solo: Vec<_> = (0..64)
        .map(|step| levels(&rig, f64::from(step) / 4.0)["solo"])
        .collect();
    let mut peaks = 0;
    for window in solo.windows(3) {
        peaks += usize::from(window[1] > window[0] && window[1] >= window[2]);
    }
    assert_eq!(peaks, 2, "{solo:?}");
    assert_seamless(&rig);
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
        let frame = render(&rig, beat);
        // Only glowing cells get their own dimmer: at most the peak's 70 % of the group.
        assert!(
            frame.effects * 4 <= EFFECT_CAP * 3,
            "{} effects",
            frame.effects
        );
        let peak = names
            .iter()
            .max_by(|a, b| frame.levels[*a].total_cmp(&frame.levels[*b]))
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
    assert_seamless(&rig);
}
