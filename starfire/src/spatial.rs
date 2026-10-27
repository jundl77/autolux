//! Where each cell sits, derived once at rig load from the MVR matrices the rig carries.
//!
//! MVR (DIN SPEC 15801) defines the Matrix as "u1,u2,u3 / v1,v2,v3 / w1,w2,w3 / o1,o2,o3" in a
//! "Right-handed, Z-Up, 1 Distance Unit equals 1 mm" system; the position is the o row
//! (`matrix[9..12]`), the rotation is left for focus work. MVR names no audience side, so
//! the plan follows GDTF (DIN SPEC 15800): "X – from left (-X) to right (+X), Y – from the
//! outside of the monitor (-Y) to the inside of the monitor (+Y)", seen from the audience:
//! -X is house left, +X house right, -Y downstage (toward the audience), +Y upstage. The
//! scene's X = 0 is the stage centreline. A cell sits at its fixture's position: the rig
//! carries no GDTF geometry offsets. Positions stay in millimetres; Lua sees metres.

use std::collections::BTreeMap;

/// Partners further apart than this from a member's reflection are not its mirror image.
const MIRROR_TOLERANCE_MM: f64 = 300.0;

pub type Position = [f64; 3];

/// Thirds of the rig's X span, named from the audience.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Zone {
    HouseLeft,
    Centre,
    HouseRight,
}

/// Either side of the middle of the rig's Y span.
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
    // min X, max X, min Y, max Y over every cell.
    bounds: [f64; 4],
    warnings: Vec<String>,
}

impl Spatial {
    /// `positions` holds every cell (and fixture) ID; `groups` lists each group's cells left
    /// to right, as Autolux orders them.
    pub fn new(
        positions: BTreeMap<String, Position>,
        groups: &BTreeMap<String, Vec<String>>,
        fixture_of: impl Fn(&str) -> usize,
    ) -> Self {
        let xs = || positions.values().map(|position| position[0]);
        let straddles = xs().any(|x| x < 0.0) && xs().any(|x| x > 0.0);
        let mut warnings = Vec::new();
        if !straddles && groups.values().any(|members| members.len() > 1) {
            warnings.push(
                "the rig does not straddle the MVR centreline X = 0; mirroring each group about its own centre"
                    .to_owned(),
            );
        }
        let bounds = positions.values().fold(
            [f64::MAX, f64::MIN, f64::MAX, f64::MIN],
            |[x0, x1, y0, y1], p| [x0.min(p[0]), x1.max(p[0]), y0.min(p[1]), y1.max(p[1])],
        );
        let mut spatial = Self {
            bounds,
            ..Self::default()
        };
        let mut mirrors = BTreeMap::new();
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
            let placed: Vec<Position> = members.iter().map(|id| positions[id]).collect();
            let axis = if straddles {
                0.0
            } else {
                let (low, high) = placed.iter().fold((f64::MAX, f64::MIN), |(low, high), p| {
                    (low.min(p[0]), high.max(p[0]))
                });
                (low + high) / 2.0
            };
            let pairs = reflected_pairs(&placed, axis).unwrap_or_else(|| {
                let one_fixture = members.iter().all(|id| fixture_of(id) == fixture_of(&members[0]));
                if !one_fixture {
                    warnings.push(format!(
                        "group {group} is not symmetric about X = {axis} mm within {MIRROR_TOLERANCE_MM} mm; sf.mirror pairs it by rank"
                    ));
                }
                rank_pairs(members.len())
            });
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

    /// X thirds of the rig's span, the centre third open so a boundary member counts as
    /// house left or right; Y above the middle of the span is upstage.
    fn place(&self, position: Position) -> (Zone, Depth) {
        let [x0, x1, y0, y1] = self.bounds;
        let third = (x1 - x0) / 3.0;
        let zone = if third <= 0.0 {
            Zone::Centre
        } else if position[0] <= x0 + third {
            Zone::HouseLeft
        } else if position[0] >= x1 - third {
            Zone::HouseRight
        } else {
            Zone::Centre
        };
        let depth = if position[1] > (y0 + y1) / 2.0 {
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

fn shared<T: PartialEq>(mut values: impl Iterator<Item = T>) -> Option<T> {
    let first = values.next()?;
    values.all(|value| value == first).then_some(first)
}

fn distance(a: Position, b: Position) -> f64 {
    ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt()
}

/// Pairs each member with the member nearest its reflection about X = `axis`, or None when a
/// member has no unique partner within tolerance or the partnering is not mutual.
/// O(n log n): members are searched by X within the tolerance window only.
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
    fn zones_are_thirds_of_the_rig_span_and_depth_halves_it() {
        // Span -3000..3000: house left to -1000, house right from 1000; Y middle is 0.
        let positions = BTreeMap::from([
            ("l".to_owned(), at(-3000.0, 0.0)),
            ("c".to_owned(), at(0.0, -2000.0)),
            ("r".to_owned(), at(3000.0, 0.0)),
            ("b".to_owned(), at(-1000.0, 2000.0)),
        ]);
        let groups = BTreeMap::from([
            ("front".to_owned(), vec!["c".to_owned()]),
            ("sides".to_owned(), vec!["l".to_owned(), "r".to_owned()]),
        ]);
        let spatial = Spatial::new(positions, &groups, |_| 0);
        let zone = |id| spatial.zone(id).map(|(zone, depth, _)| (zone, depth));
        assert_eq!(zone("l"), Some((Zone::HouseLeft, Depth::Downstage)));
        assert_eq!(zone("c"), Some((Zone::Centre, Depth::Downstage)));
        assert_eq!(zone("r"), Some((Zone::HouseRight, Depth::Downstage)));
        assert_eq!(zone("b"), Some((Zone::HouseLeft, Depth::Upstage)));
        let sides = spatial.group_place("sides").unwrap();
        assert_eq!((sides.zone, sides.depth), (None, Some(Depth::Downstage)));
        assert_eq!(
            spatial.group_place("front").unwrap().zone,
            Some(Zone::Centre)
        );
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
