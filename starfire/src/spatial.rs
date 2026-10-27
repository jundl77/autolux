//! Where each cell sits, derived once at rig load from the rig's MVR matrices and stage areas.
//!
//! A position is the MVR Matrix o row (`matrix[9..12]`, mm); the rotation is left for focus
//! work. The stage area comes from the rig as Autolux derived it (`stage_areas` in
//! cli/starfire_assets.py, which cites the MVR and GDTF axes). A cell sits at its fixture's
//! position and in its area: the rig carries no GDTF geometry offsets. Positions stay in
//! millimetres; Lua sees metres.

use std::collections::BTreeMap;

use crate::protocol::wire;

pub const MM_PER_METRE: f64 = 1000.0;

/// How far a partner may sit from a member's exact reflection: plots are drawn to a few
/// centimetres, while neighbouring fixtures on a bar hang half a metre or more apart, so
/// 300 mm absorbs plotting error without reaching a neighbour. Missing it only ever sends the
/// group to the rank fallback.
const MIRROR_TOLERANCE_MM: f64 = 300.0;

pub type Position = [f64; 3];

impl wire::StageArea {
    /// Column (house_left, centre, house_right), row (downstage, centre, upstage) and the area's
    /// own name; None when unspecified.
    pub fn placement(self) -> Option<(&'static str, &'static str, &'static str)> {
        use wire::StageArea as A;
        Some(match self {
            A::Unspecified => return None,
            A::DownLeft => ("house_left", "downstage", "down_left"),
            A::DownCentre => ("centre", "downstage", "down_centre"),
            A::DownRight => ("house_right", "downstage", "down_right"),
            A::CentreLeft => ("house_left", "centre", "centre_left"),
            A::Centre => ("centre", "centre", "centre"),
            A::CentreRight => ("house_right", "centre", "centre_right"),
            A::UpLeft => ("house_left", "upstage", "up_left"),
            A::UpCentre => ("centre", "upstage", "up_centre"),
            A::UpRight => ("house_right", "upstage", "up_right"),
        })
    }
}

/// Where a group's members lie: an area only when every member lies in the same one.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct GroupPlace {
    pub area: Option<wire::StageArea>,
    pub centre_x_mm: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Spatial {
    positions: BTreeMap<String, Position>,
    mirrors: BTreeMap<String, Vec<(String, String)>>,
    areas: BTreeMap<String, wire::StageArea>,
    places: BTreeMap<String, GroupPlace>,
    warnings: Vec<String>,
}

impl Spatial {
    /// `positions` and `areas` hold every cell (and fixture) ID; `groups` lists each group's
    /// cells; `fixture_of` names the fixture a cell belongs to.
    pub fn new(
        positions: BTreeMap<String, Position>,
        areas: BTreeMap<String, wire::StageArea>,
        groups: &BTreeMap<String, Vec<String>>,
        fixture_of: impl Fn(&str) -> usize,
    ) -> Self {
        let mut places = BTreeMap::new();
        let mut mirrors = BTreeMap::new();
        let mut warnings = Vec::new();
        for (group, members) in groups {
            let centre_x_mm =
                members.iter().map(|id| positions[id][0]).sum::<f64>() / members.len() as f64;
            let area = shared(members.iter().map(|id| areas[id]))
                .filter(|&area| area != wire::StageArea::Unspecified);
            places.insert(group.clone(), GroupPlace { area, centre_x_mm });
            let mut slots = BTreeMap::new();
            let mut fixtures: Vec<Vec<usize>> = Vec::new();
            for (index, id) in members.iter().enumerate() {
                let slot = *slots.entry(fixture_of(id)).or_insert_with(|| {
                    fixtures.push(Vec::new());
                    fixtures.len() - 1
                });
                fixtures[slot].push(index);
            }
            let placed: Vec<Position> = fixtures
                .iter()
                .map(|cells| positions[&members[cells[0]]])
                .collect();
            let straddles = placed.iter().any(|p| p[0] < 0.0) && placed.iter().any(|p| p[0] > 0.0);
            let axis = if straddles {
                0.0
            } else {
                // A group on one side of the centreline mirrors about its own centre.
                let (low, high) = placed.iter().fold((f64::MAX, f64::MIN), |(low, high), p| {
                    (low.min(p[0]), high.max(p[0]))
                });
                (low + high) / 2.0
            };
            let pairs = match reflected_pairs(&placed, axis) {
                Some(pairs) => pairs
                    .into_iter()
                    .flat_map(|(left, right)| {
                        cell_pairs(&fixtures[left], &fixtures[right], left == right)
                    })
                    .collect(),
                None => {
                    warnings.push(format!(
                        "group {group} is not symmetric about X = {axis} mm within {MIRROR_TOLERANCE_MM} mm; sf.mirror pairs it by rank"
                    ));
                    rank_pairs(members.len())
                }
            };
            let named = pairs
                .into_iter()
                .map(|(left, right)| (members[left].clone(), members[right].clone()))
                .collect();
            mirrors.insert(group.clone(), named);
        }
        Self {
            positions,
            mirrors,
            areas,
            places,
            warnings,
        }
    }

    /// A cell's (or fixture's) stage area.
    pub fn area(&self, id: &str) -> Option<wire::StageArea> {
        self.areas.get(id).copied()
    }

    pub fn group_place(&self, group: &str) -> Option<GroupPlace> {
        self.places.get(group).copied()
    }

    pub fn position(&self, id: &str) -> Option<Position> {
        self.positions.get(id).copied()
    }

    /// Mirror partners, outermost first, house left member first; an unpaired middle is its
    /// own partner.
    pub fn mirror(&self, group: &str) -> Option<&[(String, String)]> {
        self.mirrors.get(group).map(Vec::as_slice)
    }

    pub fn warnings(&self) -> &[String] {
        &self.warnings
    }
}

/// The i-th member from the left with the i-th from the right.
pub fn rank_pairs(count: usize) -> Vec<(usize, usize)> {
    (0..count.div_ceil(2))
        .map(|index| (index, count - 1 - index))
        .collect()
}

/// Cells of mirrored fixtures pair in reversed rank (the first of one with the last of the
/// other); a fixture on the axis mirrors its own cells by rank. Unmatched extra cells stand
/// alone.
fn cell_pairs(left: &[usize], right: &[usize], same: bool) -> Vec<(usize, usize)> {
    if same {
        return rank_pairs(left.len())
            .into_iter()
            .map(|(a, b)| (left[a], left[b]))
            .collect();
    }
    let paired = left.len().min(right.len());
    let mut pairs: Vec<_> = (0..paired)
        .map(|rank| (left[rank], right[right.len() - 1 - rank]))
        .collect();
    let extra = left[paired..].iter().chain(&right[..right.len() - paired]);
    pairs.extend(extra.map(|&cell| (cell, cell)));
    pairs
}

fn shared<T: PartialEq>(mut values: impl Iterator<Item = T>) -> Option<T> {
    let first = values.next()?;
    values.all(|value| value == first).then_some(first)
}

fn distance(a: Position, b: Position) -> f64 {
    ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt()
}

/// Pairs each member with the member nearest its reflection about X = `axis`, or None when a
/// member has no unique partner within tolerance or the partnering is not mutual.
/// Members are searched by X within the tolerance window: O(n log n) for spread members,
/// O(n²) where many share one X, since each then scans them all.
fn reflected_pairs(placed: &[Position], axis: f64) -> Option<Vec<(usize, usize)>> {
    let mut by_x: Vec<usize> = (0..placed.len()).collect();
    by_x.sort_by(|&a, &b| placed[a][0].total_cmp(&placed[b][0]));
    let mut partner = vec![0; placed.len()];
    for (member, slot) in partner.iter_mut().enumerate() {
        let p = placed[member];
        let image = [2.0 * axis - p[0], p[1], p[2]];
        let start = by_x.partition_point(|&i| placed[i][0] < image[0] - MIRROR_TOLERANCE_MM);
        let mut best: Option<(f64, usize)> = None;
        let mut tied = false;
        for &candidate in by_x[start..]
            .iter()
            .take_while(|&&i| placed[i][0] <= image[0] + MIRROR_TOLERANCE_MM)
        {
            let gap = distance(image, placed[candidate]);
            match best {
                Some((nearest, _)) if gap > nearest => {}
                Some((nearest, _)) if gap == nearest => tied = true,
                _ => {
                    best = Some((gap, candidate));
                    tied = false;
                }
            }
        }
        match best {
            Some((gap, found)) if gap <= MIRROR_TOLERANCE_MM && !tied => *slot = found,
            _ => return None,
        }
    }
    let mut pairs: Vec<(usize, usize)> = Vec::new();
    for (member, &other) in partner.iter().enumerate() {
        if partner[other] != member {
            return None;
        }
        if member <= other {
            let (left, right) = if placed[member][0] <= placed[other][0] {
                (member, other)
            } else {
                (other, member)
            };
            pairs.push((left, right));
        }
    }
    let reach = |(left, right): &(usize, usize)| {
        (placed[*left][0] - axis)
            .abs()
            .max((placed[*right][0] - axis).abs())
    };
    pairs.sort_by(|a, b| reach(b).total_cmp(&reach(a)).then(a.0.cmp(&b.0)));
    Some(pairs)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(x: f64, y: f64) -> Position {
        [x, y, 0.0]
    }

    fn named(pairs: &[(String, String)]) -> Vec<(&str, &str)> {
        pairs
            .iter()
            .map(|(left, right)| (left.as_str(), right.as_str()))
            .collect()
    }

    /// Each ID's fixture is its first letter, so "a1" and "a2" are cells of fixture a.
    fn fixture_of(id: &str) -> usize {
        id.as_bytes()[0] as usize
    }

    fn spatial(placed: &[(&str, Position)], groups: &[(&str, &[&str])]) -> Spatial {
        spatial_in(placed, &[], groups)
    }

    /// Unlisted IDs have an unspecified area.
    fn spatial_in(
        placed: &[(&str, Position)],
        areas: &[(&str, wire::StageArea)],
        groups: &[(&str, &[&str])],
    ) -> Spatial {
        let positions = placed
            .iter()
            .map(|(id, position)| ((*id).to_owned(), *position))
            .collect();
        let areas = placed
            .iter()
            .map(|(id, _)| {
                let area = areas.iter().find(|(named, _)| named == id);
                (
                    (*id).to_owned(),
                    area.map_or(wire::StageArea::Unspecified, |(_, area)| *area),
                )
            })
            .collect();
        let groups = groups
            .iter()
            .map(|(group, members)| {
                (
                    (*group).to_owned(),
                    members.iter().map(|&id| id.to_owned()).collect(),
                )
            })
            .collect();
        Spatial::new(positions, areas, &groups, fixture_of)
    }

    #[test]
    fn partners_are_nearest_reflections_not_ranks() {
        // Sorted by X the rank partners would be (a, d) and (b, c); the reflections say
        // a↔c (same row) and b↔d.
        let placed = [
            at(-1000.0, 0.0),
            at(-900.0, -2000.0),
            at(900.0, 0.0),
            at(1000.0, -2000.0),
        ];
        assert_eq!(reflected_pairs(&placed, 0.0), Some(vec![(0, 2), (1, 3)]));
    }

    #[test]
    fn unevenly_spaced_members_pair_within_tolerance_and_the_middle_stands_alone() {
        let placed = [
            at(-3000.0, 0.0),
            at(-1100.0, 0.0),
            at(50.0, 0.0),
            at(1000.0, 0.0),
            at(2850.0, 0.0),
        ];
        assert_eq!(
            reflected_pairs(&placed, 0.0),
            Some(vec![(0, 4), (1, 3), (2, 2)])
        );
    }

    #[test]
    fn a_group_has_an_area_only_when_every_member_shares_a_specified_one() {
        use wire::StageArea as A;
        let spatial = spatial_in(
            &[
                ("a", at(-1000.0, 0.0)),
                ("b", at(1000.0, 0.0)),
                ("c", at(3000.0, 0.0)),
                ("u", at(0.0, 0.0)),
            ],
            &[("a", A::UpLeft), ("b", A::UpLeft), ("c", A::UpRight)],
            &[
                ("same", &["a", "b"]),
                ("split", &["a", "c"]),
                ("unplaced", &["u"]),
            ],
        );
        let area = |group| spatial.group_place(group).unwrap().area;
        assert_eq!(
            [area("same"), area("split"), area("unplaced")],
            [Some(A::UpLeft), None, None]
        );
        assert_eq!(spatial.group_place("same").unwrap().centre_x_mm, 0.0);
        assert_eq!(spatial.area("c"), Some(A::UpRight));
        assert_eq!(
            A::CentreLeft.placement(),
            Some(("house_left", "centre", "centre_left"))
        );
        assert_eq!(A::Unspecified.placement(), None);
    }

    #[test]
    fn a_rig_off_the_centreline_mirrors_each_group_about_its_own_centre() {
        let spatial = spatial(
            &[
                ("a", at(0.0, 0.0)),
                ("b", at(2000.0, 0.0)),
                ("c", at(4100.0, 0.0)),
                ("d", at(6000.0, 0.0)),
                ("e", at(10000.0, 0.0)),
            ],
            &[("low", &["c", "a", "b"]), ("high", &["d", "e"])],
        );
        assert!(spatial.warnings().is_empty(), "{:?}", spatial.warnings());
        assert_eq!(
            named(spatial.mirror("low").unwrap()),
            [("a", "c"), ("b", "b")]
        );
        assert_eq!(named(spatial.mirror("high").unwrap()), [("d", "e")]);
    }

    #[test]
    fn mirrored_fixtures_pair_their_cells_in_reversed_rank_without_warning() {
        let spatial = spatial(
            &[
                ("a1", at(-1000.0, 0.0)),
                ("a2", at(-1000.0, 0.0)),
                ("m1", at(0.0, 0.0)),
                ("m2", at(0.0, 0.0)),
                ("m3", at(0.0, 0.0)),
                ("z1", at(1000.0, 0.0)),
                ("z2", at(1000.0, 0.0)),
                ("q1", at(5000.0, 0.0)),
                ("q2", at(5000.0, 0.0)),
            ],
            &[
                ("bars", &["a1", "a2", "m1", "m2", "m3", "z1", "z2"]),
                ("one", &["q1", "q2"]),
            ],
        );
        assert!(spatial.warnings().is_empty(), "{:?}", spatial.warnings());
        assert_eq!(
            named(spatial.mirror("bars").unwrap()),
            [("a1", "z2"), ("a2", "z1"), ("m1", "m3"), ("m2", "m2")]
        );
        assert_eq!(named(spatial.mirror("one").unwrap()), [("q1", "q2")]);
    }

    #[test]
    fn asymmetric_or_coincident_members_have_no_reflection() {
        assert_eq!(
            reflected_pairs(&[at(-1000.0, 0.0), at(200.0, 0.0)], 0.0),
            None
        );
        assert_eq!(reflected_pairs(&[at(0.0, 0.0), at(0.0, 0.0)], 0.0), None);
        assert_eq!(rank_pairs(3), [(0, 2), (1, 1)]);
    }
}
