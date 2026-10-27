//! Where each cell sits, derived once at rig load from the MVR matrices the rig carries.
//!
//! MVR (DIN SPEC 15801) defines the Matrix as "u1,u2,u3 / v1,v2,v3 / w1,w2,w3 / o1,o2,o3" in a
//! "Right-handed, Z-Up, 1 Distance Unit equals 1 mm" system; the position is the o row
//! (`matrix[9..12]`), the rotation is left for focus work. MVR names no audience side, so
//! the plan follows GDTF (DIN SPEC 15800): "X – from left (-X) to right (+X), Y – from the
//! outside of the monitor (-Y) to the inside of the monitor (+Y)", seen from the audience:
//! -X is house left, +X house right, -Y downstage (toward the audience), +Y upstage. The
//! scene origin is the reference: X = 0 is the stage centreline and Y = 0 splits downstage
//! from upstage (Y >= 0 is upstage). A cell sits at its fixture's position: the rig carries
//! no GDTF geometry offsets. Positions stay in millimetres; Lua sees metres.

use std::collections::BTreeMap;

pub const MM_PER_METRE: f64 = 1000.0;

/// How far a partner may sit from a member's exact reflection: plots are drawn to a few
/// centimetres, while neighbouring fixtures on a bar hang half a metre or more apart, so
/// 300 mm absorbs plotting error without reaching a neighbour. Missing it only ever sends the
/// group to the rank fallback.
const MIRROR_TOLERANCE_MM: f64 = 300.0;

pub type Position = [f64; 3];

/// Seen from the audience. About the centreline when the rig straddles X = 0, else thirds of
/// the rig's X span.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Zone {
    HouseLeft,
    Centre,
    HouseRight,
}

/// Either side of the scene's Y = 0.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Depth {
    Downstage,
    Upstage,
}

impl Zone {
    pub fn name(self) -> &'static str {
        match self {
            Self::HouseLeft => "house_left",
            Self::Centre => "centre",
            Self::HouseRight => "house_right",
        }
    }
}

impl Depth {
    pub fn name(self) -> &'static str {
        match self {
            Self::Downstage => "downstage",
            Self::Upstage => "upstage",
        }
    }
}

/// Where a group's members lie: a zone or depth only when every member shares it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct GroupPlace {
    pub zone: Option<Zone>,
    pub depth: Option<Depth>,
    pub centre_x_mm: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Spatial {
    positions: BTreeMap<String, Position>,
    mirrors: BTreeMap<String, Vec<(String, String)>>,
    places: BTreeMap<String, GroupPlace>,
    // Some(max |X|) when the rig straddles X = 0, else None and the zones use `span_x`.
    reach_x: Option<f64>,
    span_x: [f64; 2],
    warnings: Vec<String>,
}

impl Spatial {
    /// `positions` holds every cell (and fixture) ID; `groups` lists each group's cells;
    /// `fixture_of` names the fixture a cell belongs to.
    pub fn new(
        positions: BTreeMap<String, Position>,
        groups: &BTreeMap<String, Vec<String>>,
        fixture_of: impl Fn(&str) -> usize,
    ) -> Self {
        let xs = || positions.values().map(|position| position[0]);
        let straddles = xs().any(|x| x < 0.0) && xs().any(|x| x > 0.0);
        let mut spatial = Self {
            reach_x: straddles.then(|| xs().fold(0.0, |reach: f64, x| reach.max(x.abs()))),
            span_x: xs().fold([f64::MAX, f64::MIN], |[low, high], x| {
                [low.min(x), high.max(x)]
            }),
            ..Self::default()
        };
        let mut mirrors = BTreeMap::new();
        let mut warnings = Vec::new();
        for (group, members) in groups {
            let zones: Vec<_> = members
                .iter()
                .map(|id| spatial.place(positions[id]))
                .collect();
            let centre_x_mm =
                members.iter().map(|id| positions[id][0]).sum::<f64>() / members.len() as f64;
            spatial.places.insert(
                group.clone(),
                GroupPlace {
                    zone: shared(zones.iter().map(|place| place.0)),
                    depth: shared(zones.iter().map(|place| place.1)),
                    centre_x_mm,
                },
            );
            // Each fixture once, in member order, with the indices of its cells.
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
            // A group on one side of the centreline mirrors about its own centre.
            let sided = placed.iter().any(|p| p[0] < 0.0) && placed.iter().any(|p| p[0] > 0.0);
            let axis = if sided {
                0.0
            } else {
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
            warnings,
            ..spatial
        }
    }

    /// About the centreline, |X| up to a third of the rig's reach is centre; without one, X
    /// thirds of the span with the centre third open. Y >= 0 is upstage.
    fn place(&self, position: Position) -> (Zone, Depth) {
        let x = position[0];
        let zone = match self.reach_x {
            Some(reach) if x.abs() <= reach / 3.0 => Zone::Centre,
            Some(_) if x < 0.0 => Zone::HouseLeft,
            Some(_) => Zone::HouseRight,
            None => {
                let [low, high] = self.span_x;
                let third = (high - low) / 3.0;
                if third <= 0.0 {
                    Zone::Centre
                } else if x <= low + third {
                    Zone::HouseLeft
                } else if x >= high - third {
                    Zone::HouseRight
                } else {
                    Zone::Centre
                }
            }
        };
        let depth = if position[1] >= 0.0 {
            Depth::Upstage
        } else {
            Depth::Downstage
        };
        (zone, depth)
    }

    /// A cell's (or fixture's) zone, depth and height (Z, mm).
    pub fn zone(&self, id: &str) -> Option<(Zone, Depth, f64)> {
        let position = self.position(id)?;
        let (zone, depth) = self.place(position);
        Some((zone, depth, position[2]))
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
        let positions = placed
            .iter()
            .map(|(id, position)| ((*id).to_owned(), *position))
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
        Spatial::new(positions, &groups, fixture_of)
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
    fn zones_split_about_the_centreline_and_depth_at_y_zero() {
        // Reach 3000 mm: centre within ±1000 (inclusive); Y >= 0 is upstage.
        let spatial = spatial(
            &[
                ("l", at(-3000.0, 0.0)),
                ("c", at(0.0, -2000.0)),
                ("r", at(3000.0, 0.0)),
                ("b", at(-1000.0, 2000.0)),
            ],
            &[("front", &["c"]), ("sides", &["l", "r"])],
        );
        let zone = |id| spatial.zone(id).map(|(zone, depth, _)| (zone, depth));
        assert_eq!(zone("l"), Some((Zone::HouseLeft, Depth::Upstage)));
        assert_eq!(zone("c"), Some((Zone::Centre, Depth::Downstage)));
        assert_eq!(zone("r"), Some((Zone::HouseRight, Depth::Upstage)));
        assert_eq!(zone("b"), Some((Zone::Centre, Depth::Upstage)));
        let sides = spatial.group_place("sides").unwrap();
        assert_eq!((sides.zone, sides.depth), (None, Some(Depth::Upstage)));
        assert_eq!(
            spatial.group_place("front").unwrap().zone,
            Some(Zone::Centre)
        );
    }

    #[test]
    fn a_rig_off_the_centreline_mirrors_each_group_about_its_own_centre() {
        // X 0..10000: no centreline placement, so zones are span thirds (3333 / 6667).
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
        let zone = |id| spatial.zone(id).unwrap().0;
        assert_eq!(
            [zone("a"), zone("c"), zone("e")],
            [Zone::HouseLeft, Zone::Centre, Zone::HouseRight]
        );
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
