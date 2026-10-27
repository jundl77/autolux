mod support;

use std::collections::BTreeMap;
use std::time::Instant;

use prost::Message;
use starfire::fixtures::FixtureRig;
use starfire::protocol::{MAX_MESSAGE_BYTES, wire};
use starfire::spatial::{Depth, Zone};

const ROW_GROUPS: usize = 22;
const ACCENT_GROUPS: usize = 6;
const FIXTURES: usize = 400;
const PER_UNIVERSE: usize = 170;
const ROWS_UPSTAGE_FROM: usize = 12;

struct Venue {
    definition: wire::RigDefinition,
    at: BTreeMap<String, [f64; 2]>,
}

impl Venue {
    fn group(&mut self, id: String, role: wire::GroupRole, placed: &[[f64; 2]]) {
        let n = placed.len();
        let mut members = Vec::new();
        // Shuffled member order (stride 7 is coprime with every group size here), so the
        // order Starfire receives says nothing about who mirrors whom.
        for rank in 0..n {
            let [x, y] = placed[(rank * 7) % n];
            let next = self.definition.fixtures.len();
            let fixture = format!("f{next:03}");
            self.definition.fixtures.push(wire::Fixture {
                fixture_id: fixture.clone(),
                profile_id: "generic_rgb".into(),
                universe: (next / PER_UNIVERSE) as u32,
                address: ((next % PER_UNIVERSE) * 3 + 1) as u32,
                matrix: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, x, y, 3000.0],
                ..Default::default()
            });
            self.at.insert(fixture.clone(), [x, y]);
            members.push(fixture);
        }
        self.definition.groups.push(wire::FixtureGroup {
            group_id: id,
            fixture_ids: members,
            role: role as i32,
        });
    }
}

/// 24 groups, 400 fixtures: 22 rows centred on X = 0 a metre apart (6 accent; rows 12 on at
/// Y >= 0, upstage), two rows interleaved in X that mirror within their own row, and one
/// group wholly house right.
fn venue() -> Venue {
    let mut venue = Venue {
        definition: wire::RigDefinition {
            profiles: vec![support::catalogue_profile("generic_rgb").unwrap()],
            ..Default::default()
        },
        at: BTreeMap::new(),
    };
    for row in 0..ROW_GROUPS {
        let count = if row < 6 { 18 } else { 17 };
        let y = row as f64 * 500.0 - 6000.0;
        let placed: Vec<_> = (0..count)
            .map(|index| [(index as f64 - (count - 1) as f64 / 2.0) * 1000.0, y])
            .collect();
        let role = if row < ACCENT_GROUPS {
            wire::GroupRole::Accent
        } else {
            wire::GroupRole::Loop
        };
        venue.group(format!("row{row:02}"), role, &placed);
    }
    // Front row house-left members sit 100 mm further out than their partners, the back row
    // the other way, so in X the rows alternate and rank would pair across them.
    let mut interleaved = Vec::new();
    for k in 1..=4 {
        let x = k as f64 * 1000.0;
        interleaved.extend([[-x, -8000.0], [x - 100.0, -8000.0]]);
        interleaved.extend([[-(x - 100.0), -7500.0], [x, -7500.0]]);
    }
    venue.group("interleaved".into(), wire::GroupRole::Loop, &interleaved);
    let side: Vec<_> = (0..4)
        .map(|index| [4000.0 + index as f64 * 1000.0, -9000.0])
        .collect();
    venue.group("house_right".into(), wire::GroupRole::Loop, &side);
    assert_eq!(venue.definition.fixtures.len(), FIXTURES);
    venue
}

#[test]
fn a_400_fixture_venue_pairs_by_reflection_and_places_its_groups() {
    let venue = venue();
    let rig = FixtureRig::from_wire(&venue.definition).unwrap();
    let spatial = rig.spatial();
    assert!(spatial.warnings().is_empty(), "{:?}", spatial.warnings());
    for (group, members) in rig.groups() {
        let pairs = spatial.mirror(group).unwrap();
        let axis = if group == "house_right" { 5500.0 } else { 0.0 };
        let mut reach = f64::MAX;
        for (left, right) in pairs {
            let ([lx, ly], [rx, ry]) = (venue.at[left], venue.at[right]);
            // Same row, about the axis within the 100 mm offset, outermost first.
            assert_eq!(ly, ry, "{group}: {left} {right}");
            assert!(
                (lx + rx - 2.0 * axis).abs() <= 100.0,
                "{group}: {left} {right}"
            );
            assert!(lx <= rx);
            let outer = (lx - axis).abs().max((rx - axis).abs());
            assert!(outer <= reach, "{group}: {left} {right}");
            reach = outer;
        }
        assert_eq!(pairs.len(), members.len().div_ceil(2), "{group}");
    }
    let place = |group: &str| spatial.group_place(group).unwrap();
    for row in 0..ROW_GROUPS {
        let place = place(&format!("row{row:02}"));
        assert_eq!(place.zone, None);
        let depth = if row >= ROWS_UPSTAGE_FROM {
            Depth::Upstage
        } else {
            Depth::Downstage
        };
        assert_eq!(place.depth, Some(depth), "row {row}");
    }
    // Reach 8500 mm: centre within ±2833, so 4000..7000 is wholly house right.
    let side = place("house_right");
    assert_eq!(
        (side.zone, side.depth),
        (Some(Zone::HouseRight), Some(Depth::Downstage))
    );
    assert_eq!(place("interleaved").zone, None);
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
    let definition = venue().definition;
    let started = Instant::now();
    FixtureRig::from_wire(&definition).unwrap();
    let elapsed = started.elapsed();
    assert!(elapsed.as_millis() < 100, "{elapsed:?}");
}
