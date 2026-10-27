mod support;

use std::time::Instant;

use prost::Message;
use starfire::fixtures::FixtureRig;
use starfire::protocol::{MAX_MESSAGE_BYTES, wire};
use starfire::spatial::Zone;

const GROUPS: usize = 24;
const ACCENT_GROUPS: usize = 6;
const FIXTURES: usize = 400;
const SPACING_MM: f64 = 1000.0;
const PER_UNIVERSE: usize = 170;

/// 24 rows, one per group, each centred on X = 0 with members a metre apart: 16 rows of 17
/// and 8 of 16, so every mirror pair and zone is known by construction.
fn venue() -> wire::RigDefinition {
    let profile = support::catalogue_profile("generic_rgb").unwrap();
    let mut rig = wire::RigDefinition {
        profiles: vec![profile],
        ..Default::default()
    };
    let mut next = 0;
    for group in 0..GROUPS {
        let count = if group < 16 { 17 } else { 16 };
        let mut members = Vec::new();
        for index in 0..count {
            let id = format!("f{next:03}");
            let x = (index as f64 - (count - 1) as f64 / 2.0) * SPACING_MM;
            let y = group as f64 * 500.0 - 6000.0;
            rig.fixtures.push(wire::Fixture {
                fixture_id: id.clone(),
                profile_id: "generic_rgb".into(),
                universe: (next / PER_UNIVERSE) as u32,
                address: ((next % PER_UNIVERSE) * 3 + 1) as u32,
                matrix: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, x, y, 3000.0],
                ..Default::default()
            });
            members.push(id);
            next += 1;
        }
        rig.groups.push(wire::FixtureGroup {
            group_id: format!("g{group:02}"),
            fixture_ids: members,
            role: if group < ACCENT_GROUPS {
                wire::GroupRole::Accent
            } else {
                wire::GroupRole::Loop
            } as i32,
        });
    }
    assert_eq!(next, FIXTURES);
    rig
}

#[test]
fn a_400_fixture_venue_derives_mirrors_and_zones_by_construction() {
    let rig = FixtureRig::from_wire(&venue()).unwrap();
    let spatial = rig.spatial();
    assert!(spatial.warnings().is_empty(), "{:?}", spatial.warnings());
    for (group, members) in rig.groups() {
        let pairs = spatial.mirror(group).unwrap();
        assert_eq!(pairs.len(), members.len().div_ceil(2));
        for (rank, (left, right)) in pairs.iter().enumerate() {
            assert_eq!(
                (left, right),
                (&members[rank], &members[members.len() - 1 - rank])
            );
        }
        let place = spatial.group_place(group).unwrap();
        assert_eq!(place.zone, None);
        assert!(place.centre_x_mm.abs() < 1e-9);
    }
    // X span -8000..8000: thirds at ±2667 mm.
    let zone = |id: &str| spatial.zone(id).unwrap().0;
    assert_eq!(zone("f000"), Zone::HouseLeft);
    assert_eq!(zone("f008"), Zone::Centre);
    assert_eq!(zone("f016"), Zone::HouseRight);
    assert_eq!(spatial.zone("f000").unwrap().2, 3000.0);
    let capabilities = starfire::special::capabilities(&rig);
    let accents = capabilities
        .iter()
        .filter(|group| group.role() == wire::GroupRole::Accent)
        .count();
    assert_eq!(accents, ACCENT_GROUPS);
    let reply = wire::LoginReply {
        groups: capabilities,
        ..Default::default()
    };
    assert!(reply.encoded_len() <= MAX_MESSAGE_BYTES);
}

/// Opt-in wall-clock check (`cargo test -p starfire --test spatial_scale -- --ignored`): the
/// derivation runs once at rig load, so a generous bound only guards against a quadratic slip.
#[test]
#[ignore = "timing"]
fn a_400_fixture_venue_loads_well_within_100_ms() {
    let definition = venue();
    let started = Instant::now();
    FixtureRig::from_wire(&definition).unwrap();
    let elapsed = started.elapsed();
    assert!(elapsed.as_millis() < 100, "{elapsed:?}");
}
