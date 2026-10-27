mod support;

use std::collections::BTreeMap;
use std::time::Instant;

use prost::Message;
use starfire::fixtures::FixtureRig;
use starfire::protocol::{MAX_MESSAGE_BYTES, wire};

const ROW_GROUPS: usize = 22;
const ACCENT_GROUPS: usize = 6;
const FIXTURES: usize = 400;
const PER_UNIVERSE: usize = 170;
// Rows sit at Y -6000..4500 in 500 mm steps; with the side and interleaved rows the box is
// X -8500..8500, Y -9000..4500, so the X thirds cut at ±2833 and the Y thirds at -4500 and 0
// (row 12, at Y = 0, sits on the cut and so in the centre).
const ROWS_CENTRE_FROM: usize = 3;
const ROWS_UPSTAGE_FROM: usize = 13;

struct Venue {
    definition: wire::RigDefinition,
    at: BTreeMap<String, [f64; 2]>,
}

impl Venue {
    /// `area` is what Autolux derives for every member (each group lies in one row of areas or
    /// in one area); Starfire only carries it.
    fn group(
        &mut self,
        id: String,
        role: wire::GroupRole,
        placed: &[[f64; 2]],
        area: impl Fn([f64; 2]) -> wire::StageArea,
    ) {
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
                area: area([x, y]) as i32,
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

/// The stage area of a plan position in this venue's box (see `ROWS_CENTRE_FROM`), on a cut
/// to the centre.
fn area([x, y]: [f64; 2]) -> wire::StageArea {
    use wire::StageArea as A;
    let column =
        usize::from(x >= -8500.0 + 17000.0 / 3.0) + usize::from(x > 8500.0 - 17000.0 / 3.0);
    let row = usize::from(y >= -4500.0) + usize::from(y > 0.0);
    [
        [A::DownLeft, A::DownCentre, A::DownRight],
        [A::CentreLeft, A::Centre, A::CentreRight],
        [A::UpLeft, A::UpCentre, A::UpRight],
    ][row][column]
}

/// 24 groups, 400 fixtures: 22 rows centred on X = 0 a metre apart (6 accent), two rows
/// interleaved in X that mirror within their own row, and one group wholly house right.
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
        venue.group(format!("row{row:02}"), role, &placed, area);
    }
    // Front row house-left members sit 100 mm further out than their partners, the back row
    // the other way, so in X the rows alternate and rank would pair across them.
    let mut interleaved = Vec::new();
    for k in 1..=4 {
        let x = k as f64 * 1000.0;
        interleaved.extend([[-x, -8000.0], [x - 100.0, -8000.0]]);
        interleaved.extend([[-(x - 100.0), -7500.0], [x, -7500.0]]);
    }
    venue.group(
        "interleaved".into(),
        wire::GroupRole::Loop,
        &interleaved,
        area,
    );
    let side: Vec<_> = (0..4)
        .map(|index| [4000.0 + index as f64 * 1000.0, -9000.0])
        .collect();
    venue.group("house_right".into(), wire::GroupRole::Loop, &side, area);
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
    use wire::StageArea as A;
    // Every row spans the three columns of its area row, so no row group has an area; each
    // member does, house left to house right.
    for row in 0..ROW_GROUPS {
        let group = format!("row{row:02}");
        assert_eq!(spatial.group_place(&group).unwrap().area, None, "{group}");
        let areas = if row >= ROWS_UPSTAGE_FROM {
            [A::UpLeft, A::UpCentre, A::UpRight]
        } else if row >= ROWS_CENTRE_FROM {
            [A::CentreLeft, A::Centre, A::CentreRight]
        } else {
            [A::DownLeft, A::DownCentre, A::DownRight]
        };
        let mut members = rig.groups()[&group].clone();
        members.sort_by(|a, b| venue.at[a][0].total_cmp(&venue.at[b][0]));
        let (first, last) = (&members[0], &members[members.len() - 1]);
        let middle = &members[members.len() / 2];
        assert_eq!(
            [first, middle, last].map(|id| spatial.area(id).unwrap()),
            areas,
            "{group}"
        );
    }
    // 4000..7000 lies wholly in the house-right third, downstage.
    let area = |group: &str| spatial.group_place(group).unwrap().area;
    assert_eq!(area("house_right"), Some(A::DownRight));
    assert_eq!(area("interleaved"), None);
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
