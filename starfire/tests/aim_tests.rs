mod support;

use serde_json::{Value, json};
use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Effect, PropertyValues, RenderContext, Target, Transition};
use starfire::protocol::wire;
use starfire::script::LuaAutoloop;

const HEAD: &str = "scanner";
const BAR: &str = "bar";
const PAN_TRAVEL: f64 = 540.0;
const TILT_TRAVEL: f64 = 270.0;
// One 8-bit step is 2.1 degrees of pan and 1.1 of tilt: about 0.04 of a unit vector together.
const DMX_STEP: f64 = 0.05;
const EXACT: f64 = 1e-9;

fn rig(fixtures: Value, groups: Value) -> FixtureRig {
    let mut definition = support::definition(&json!({ "fixtures": fixtures, "groups": groups }));
    definition.profiles.extend([
        support::compiled_profile("beam_scanner#6ch", HEAD),
        support::compiled_profile("pixel_bar#Pixels", BAR),
    ]);
    FixtureRig::from_wire(&definition).unwrap()
}

fn head(id: &str, slot: usize, matrix: Vec<f64>, focus: [f64; 3]) -> Value {
    json!({
        "fixture_uuid": id, "profile": HEAD, "universe": 0, "address": 1 + 6 * slot,
        "matrix": matrix, "focus": focus,
    })
}

fn wash(id: &str, slot: usize, position: [f64; 3]) -> Value {
    json!({
        "fixture_uuid": id, "profile": "generic_rgb", "universe": 0, "address": 1 + 6 * slot,
        "matrix": support::mounted(true, 0.0, position),
    })
}

fn add(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [0, 1, 2].map(|axis| a[axis] + b[axis])
}

fn unit(v: [f64; 3]) -> [f64; 3] {
    let length = v.iter().map(|value| value * value).sum::<f64>().sqrt();
    v.map(|value| value / length)
}

fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    (0..3).map(|axis| a[axis] * b[axis]).sum()
}

fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}

fn degrees_between(a: [f64; 3], b: [f64; 3]) -> f64 {
    dot(a, b).clamp(-1.0, 1.0).acos().to_degrees()
}

fn hanging_beam(heading: f64, lean: f64) -> [f64; 3] {
    let (sin_h, cos_h) = heading.to_radians().sin_cos();
    let (sin_l, cos_l) = lean.to_radians().sin_cos();
    [sin_l * sin_h, sin_l * cos_h, -cos_l]
}

fn assert_near(actual: [f64; 3], expected: [f64; 3], tolerance: f64, context: &str) {
    assert!(
        (0..3).all(|axis| (actual[axis] - expected[axis]).abs() < tolerance),
        "{context}: {actual:?} vs {expected:?}"
    );
}

#[test]
fn aim_at_and_beam_direction_are_inverses_over_mounts_yaws_points_and_soft_limits() {
    // Every limit pair still reaches every heading (378 of 540 degrees) and leans to 121.5.
    let limits = [
        (json!({}), json!({})),
        (
            json!({"min": 0.1, "max": 0.8}),
            json!({"min": 0.05, "max": 0.95, "inverted": true}),
        ),
        (
            json!({"min": 0.15, "max": 0.85, "inverted": true}),
            json!({"inverted": true}),
        ),
    ];
    let steep = 89.9_f64.to_radians();
    for hanging in [true, false] {
        let position = [1000.0, -500.0, if hanging { 3000.0 } else { 0.0 }];
        let side = if hanging { -1.0 } else { 1.0 };
        let offsets = [
            [1500.0, 2500.0, 2500.0 * side],
            [-2000.0, -800.0, 2500.0 * side],
            [0.0, 2000.0, 0.0],
            [-1000.0, 1500.0, -600.0 * side],
            [0.0, 0.0, 3000.0 * side],
            [
                1800.0 * steep.cos(),
                2400.0 * steep.cos(),
                3000.0 * steep.sin() * side,
            ],
        ];
        for yaw in [0.0, 37.0, 90.0, 180.0, 233.0, -90.0] {
            for (pan_limits, tilt_limits) in &limits {
                let mut fixture = head(
                    "head",
                    0,
                    support::mounted(hanging, yaw, position),
                    add(position, offsets[0]),
                );
                fixture["pan"] = pan_limits.clone();
                fixture["tilt"] = tilt_limits.clone();
                let rig = rig(json!([fixture]), Value::Null);
                let context =
                    format!("hanging {hanging} yaw {yaw} limits {pan_limits} {tilt_limits}");
                for offset in offsets {
                    let point = add(position, offset);
                    let (pan, tilt) = rig.aim_at("head", point).unwrap();
                    assert!((0.0..=1.0).contains(&pan) && (0.0..=1.0).contains(&tilt));
                    let direction = rig.beam_direction("head", pan, tilt).unwrap();
                    assert_near(
                        direction,
                        unit(offset),
                        EXACT,
                        &format!("{context} {offset:?}"),
                    );
                }
                for pan in [0.0, 0.3, 0.5, 0.77, 1.0] {
                    for tilt in [0.0, 0.2, 0.5, 0.64, 1.0] {
                        let direction = rig.beam_direction("head", pan, tilt).unwrap();
                        let point = add(position, direction.map(|value| 3000.0 * value));
                        let (pan_again, tilt_again) = rig.aim_at("head", point).unwrap();
                        assert_near(
                            rig.beam_direction("head", pan_again, tilt_again).unwrap(),
                            direction,
                            EXACT,
                            &format!("{context} pan {pan} tilt {tilt}"),
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn a_point_outside_the_soft_limits_clamps_to_the_nearest_edge() {
    // Soft limits: headings 54..162 degrees, leans 0..54 degrees.
    let position = [0.0, 0.0, 3000.0];
    let mut fixture = head("head", 0, support::mounted(true, 0.0, position), [0.0; 3]);
    fixture["pan"] = json!({"min": 0.1, "max": 0.3});
    fixture["tilt"] = json!({"min": 0.5, "max": 0.7});
    let rig = rig(json!([fixture]), Value::Null);
    for ((heading, lean), (edge_heading, edge_lean)) in [
        ((180.0, 30.0), (162.0, 30.0)),
        ((20.0, 30.0), (54.0, 30.0)),
        ((300.0, 30.0), (54.0, 30.0)),
        ((100.0, 80.0), (100.0, 54.0)),
    ] {
        let point = add(
            position,
            hanging_beam(heading, lean).map(|value| 3000.0 * value),
        );
        let (pan, tilt) = rig.aim_at("head", point).unwrap();
        assert_near(
            rig.beam_direction("head", pan, tilt).unwrap(),
            hanging_beam(edge_heading, edge_lean),
            EXACT,
            &format!("heading {heading} lean {lean}"),
        );
    }
}

#[test]
fn a_point_at_the_head_itself_rests_along_the_pan_axis() {
    for (hanging, axis) in [(true, -1.0), (false, 1.0)] {
        let position = [700.0, -300.0, if hanging { 3000.0 } else { 0.0 }];
        let rig = rig(
            json!([head(
                "head",
                0,
                support::mounted(hanging, 37.0, position),
                position
            )]),
            Value::Null,
        );
        let (pan, tilt) = rig.aim("head").unwrap();
        assert_eq!(tilt, 0.5, "hanging {hanging}");
        assert_near(
            rig.beam_direction("head", pan, tilt).unwrap(),
            [0.0, 0.0, axis],
            EXACT,
            &format!("hanging {hanging}"),
        );
    }
}

fn thrown(dmx: &[u8], matrix: &[f64]) -> [f64; 3] {
    let share = |byte: u8| f64::from(byte) / 255.0;
    support::beam_through(
        matrix,
        (share(dmx[0]) * PAN_TRAVEL).to_radians(),
        ((share(dmx[1]) - 0.5) * TILT_TRAVEL).to_radians(),
    )
}

#[test]
fn beam_direction_is_where_the_encoded_dmx_throws_the_beam() {
    for hanging in [true, false] {
        for yaw in [0.0, 37.0, 180.0, -90.0] {
            let position = [500.0, 1000.0, if hanging { 3000.0 } else { 0.0 }];
            let matrix = support::mounted(hanging, yaw, position);
            let mut fixture = head("head", 0, matrix.clone(), [0.0; 3]);
            fixture["pan"] = json!({"min": 0.1, "max": 0.8, "inverted": true});
            fixture["tilt"] = json!({"min": 0.05, "max": 0.95});
            let rig = rig(json!([fixture]), Value::Null);
            for pan in [0.0, 0.25, 0.6, 1.0] {
                for tilt in [0.1, 0.5, 0.8] {
                    let mut states = rig.empty_states();
                    let state = states.get_mut("head").unwrap();
                    (state.pan, state.tilt) = (pan, tilt);
                    let frame = &rig.encode(&states).unwrap()[&0];
                    assert_near(
                        rig.beam_direction("head", pan, tilt).unwrap(),
                        thrown(&frame[..2], &matrix),
                        DMX_STEP,
                        &format!("hanging {hanging} yaw {yaw} pan {pan} tilt {tilt}"),
                    );
                }
            }
        }
    }
}

#[test]
fn a_cell_without_a_pan_range_has_a_focus_but_no_aim_or_beam() {
    let rig = rig(
        json!([
            head(
                "head",
                0,
                support::mounted(true, 0.0, [-2000.0, 0.0, 3000.0]),
                [0.0; 3]
            ),
            wash("wash", 1, [2000.0, 1000.0, 3000.0]),
        ]),
        Value::Null,
    );
    assert_eq!(rig.focus("wash"), Some([0.0, 500.0, 0.0]));
    assert_eq!(rig.aim("wash"), None);
    assert_eq!(rig.aim_at("wash", [0.0; 3]), None);
    assert_eq!(rig.beam_direction("wash", 0.5, 0.5), None);
    assert_eq!(rig.focus("nobody"), None);
}

fn autoloop(rig: &FixtureRig, body: &str) -> LuaAutoloop {
    support::test_catalog(&[(
        "autoloop",
        &format!("return {{render = function(ctx)\n{body}\nend}}"),
    )])
    .instantiate("test", "autoloop", rig, &support::palettes())
    .unwrap()
}

fn elapsed(beats: f64) -> RenderContext {
    RenderContext {
        time_seconds: beats + 5.0,
        beat_position: beats + 13.0,
        autoloop_elapsed_beats: beats,
        autoloop_speed_scaled_elapsed_beats: beats,
        ..Default::default()
    }
}

fn aimed(effect: Effect) -> (String, f64, f64) {
    let Target::Fixture(cell) = effect.target else {
        panic!("{:?} is not one cell", effect.target)
    };
    let (Some(pan), Some(tilt)) = (effect.values.pan, effect.values.tilt) else {
        panic!("{cell} has no pan and tilt")
    };
    assert_eq!(
        effect.values,
        PropertyValues {
            pan: Some(pan),
            tilt: Some(tilt),
            ..Default::default()
        }
    );
    (cell, pan, tilt)
}

fn render(autoloop: &mut LuaAutoloop, context: &RenderContext) -> Vec<(String, f64, f64)> {
    let palette = support::palettes().slots("techno").unwrap();
    autoloop
        .render(context, &palette)
        .unwrap()
        .into_iter()
        .map(aimed)
        .collect()
}

fn fails(rig: &FixtureRig, body: &str) -> String {
    let palette = support::palettes().slots("techno").unwrap();
    let error = autoloop(rig, body)
        .render(&elapsed(0.0), &palette)
        .unwrap_err();
    format!("{error:#}")
}

fn beams(rig: &FixtureRig, aims: &[(String, f64, f64)]) -> Vec<[f64; 3]> {
    aims.iter()
        .map(|(cell, pan, tilt)| rig.beam_direction(cell, *pan, *tilt).unwrap())
        .collect()
}

fn assert_same_aims(actual: &[(String, f64, f64)], expected: &[(String, f64, f64)], context: &str) {
    assert_eq!(actual.len(), expected.len(), "{context}");
    for ((cell, pan, tilt), (expected_cell, expected_pan, expected_tilt)) in
        actual.iter().zip(expected)
    {
        assert!(
            cell == expected_cell
                && (pan - expected_pan).abs() < EXACT
                && (tilt - expected_tilt).abs() < EXACT,
            "{context}: {actual:?} vs {expected:?}"
        );
    }
}

#[test]
fn sf_aim_and_sf_focus_read_the_rig_in_metres_and_only_cells_with_pan_aim() {
    let rig = rig(
        json!([
            head("head", 0, support::mounted(true, 37.0, [-1000.0, 500.0, 4000.0]), [-2000.0, 3000.0, 0.0]),
            wash("wash", 1, [3000.0, -500.0, 3000.0]),
            {"fixture_uuid": "bar", "profile": BAR, "universe": 0, "address": 13,
             "matrix": support::mounted(true, 0.0, [1000.0, 0.0, 3000.0])},
        ]),
        Value::Null,
    );
    let [x, y, z] = rig.focus("wash").unwrap().map(|mm| mm / 1000.0);
    let mut autoloop = autoloop(
        &rig,
        &format!(
            r#"local x, y, z = sf.focus("head")
            assert(x == -2 and y == 3 and z == 0, "focus in metres")
            x, y, z = sf.focus(sf.fixture("wash"))
            assert(x == {x:?} and y == {y:?} and z == {z:?}, "the stage centre without a focus point")
            for _, aim in ipairs {{
                table.pack(sf.aim("wash", 1, 2, 0)),
                table.pack(sf.aim("bar/Pixel 1", 1, 2, 0)),
                table.pack(sf.aim_offset("wash", 10, 5)),
                table.pack(sf.aim_direction("wash", 0, 0, 1)),
            }} do
                assert(aim.n == 2 and aim[1] == nil and aim[2] == nil, "a cell without pan aims nowhere")
            end
            return {{sf.pan_tilt(sf.fixture("head"), sf.aim("head", 1.5, 2, 0))}}"#
        ),
    );
    let aims = render(&mut autoloop, &elapsed(0.0));
    let (pan, tilt) = rig.aim_at("head", [1500.0, 2000.0, 0.0]).unwrap();
    assert_same_aims(&aims, &[("head".into(), pan, tilt)], "sf.aim");
    for (call, error) in [
        (r#"sf.aim("head", 0/0, 0, 0)"#, "aim point must be finite"),
        (r#"sf.aim("nobody", 0, 0, 0)"#, "unknown cell nobody"),
        (r#"sf.aim("bar", 0, 0, 0)"#, "unknown cell bar"),
    ] {
        let message = fails(
            &rig,
            &format!(r#"return {{sf.pan_tilt(sf.fixture("head"), {call})}}"#),
        );
        assert!(message.contains(error), "{call}: {message}");
    }
}

fn rest_beams() -> FixtureRig {
    rig(
        json!([
            head(
                "down",
                0,
                support::mounted(true, 0.0, [0.0, 0.0, 4000.0]),
                [0.0, 0.0, 0.0]
            ),
            head(
                "up",
                1,
                support::mounted(false, 0.0, [0.0, 1000.0, 0.0]),
                [0.0, 1000.0, 3000.0]
            ),
            head(
                "upstage",
                2,
                support::mounted(true, 30.0, [-2000.0, 0.0, 3000.0]),
                [-2000.0, 4000.0, 3000.0]
            ),
            head(
                "across",
                3,
                support::mounted(true, -90.0, [2000.0, 0.0, 3000.0]),
                [6000.0, 0.0, 3000.0]
            ),
            head(
                "slant",
                4,
                support::mounted(true, 0.0, [1000.0, -500.0, 3000.0]),
                [-500.0, 2500.0, 0.0]
            ),
            head(
                "side",
                5,
                support::mounted(true, 90.0, [-3000.0, 0.0, 3000.0]),
                [-1000.0, 0.0, 1500.0]
            ),
            head(
                "diagonal",
                6,
                support::mounted(true, 0.0, [-3000.0, -2000.0, 3000.0]),
                [-1000.0, -1000.0, 1500.0]
            ),
        ]),
        Value::Null,
    )
}

#[test]
fn aim_offset_spreads_across_and_elevates_up_round_any_rest_beam() {
    let rig = rest_beams();
    let cells = ["down", "up", "upstage", "across", "slant"];
    let spreads = [-35.0, 0.0, 20.0];
    let elevations = [-25.0, 0.0, 30.0];
    let mut autoloop = autoloop(
        &rig,
        r#"local effects = {}
        for _, cell in ipairs { "down", "up", "upstage", "across", "slant" } do
            for _, spread in ipairs { -35, 0, 20 } do
                for _, elevation in ipairs { -25, 0, 30 } do
                    effects[#effects + 1] = sf.pan_tilt(sf.fixture(cell), sf.aim_offset(cell, spread, elevation))
                end
            end
        end
        return effects"#,
    );
    let mut aims = render(&mut autoloop, &elapsed(0.0)).into_iter();
    let slant = unit([-1500.0, 3000.0, -3000.0]);
    for cell in cells {
        for spread in spreads {
            for elevation in elevations {
                let (_, pan, tilt) = aims.next().unwrap();
                let beam = rig.beam_direction(cell, pan, tilt).unwrap();
                let context = format!("{cell} spread {spread} elevation {elevation}");
                let (sin_s, cos_s) = f64::to_radians(spread).sin_cos();
                let (sin_e, cos_e) = f64::to_radians(elevation).sin_cos();
                let expected = match cell {
                    "down" => Some([cos_e * sin_s, sin_e, -cos_e * cos_s]),
                    "up" => Some([cos_e * sin_s, -sin_e, cos_e * cos_s]),
                    "upstage" => Some([cos_e * sin_s, cos_e * cos_s, sin_e]),
                    "across" => Some([cos_e * cos_s, -cos_e * sin_s, sin_e]),
                    _ => None,
                };
                if let Some(expected) = expected {
                    assert_near(beam, expected, EXACT, &context);
                } else if elevation == 0.0 {
                    let across = cross(slant, [0.0, 0.0, 1.0]);
                    assert!(
                        (degrees_between(beam, slant) - spread.abs()).abs() < 1e-7,
                        "{context}"
                    );
                    assert!(dot(cross(slant, across), beam).abs() < EXACT, "{context}");
                    assert!(spread == 0.0 || (beam[0] - slant[0]).signum() == spread.signum());
                } else if spread == 0.0 {
                    assert!(
                        (degrees_between(beam, slant) - elevation.abs()).abs() < 1e-7,
                        "{context}"
                    );
                    assert!(
                        dot(cross(slant, [0.0, 0.0, 1.0]), beam).abs() < EXACT,
                        "{context}"
                    );
                    assert!(
                        (beam[2] - slant[2]).signum() == elevation.signum(),
                        "{context}"
                    );
                }
                if spread == 0.0 && elevation == 0.0 {
                    let (rest_pan, rest_tilt) = rig.aim(cell).unwrap();
                    assert!((pan - rest_pan).abs() < EXACT && (tilt - rest_tilt).abs() < EXACT);
                }
            }
        }
    }
}

fn turns(rest: [f64; 3], turned: &[[f64; 3]]) -> Vec<[f64; 3]> {
    turned
        .iter()
        .map(|beam| {
            let along = dot(*beam, rest);
            [0, 1, 2].map(|axis| beam[axis] - along * rest[axis])
        })
        .collect()
}

#[test]
fn spread_and_elevation_turn_at_right_angles_so_a_side_stand_circles() {
    let rig = rest_beams();
    let mut autoloop = autoloop(
        &rig,
        r#"local effects = {}
        for _, cell in ipairs { "side", "diagonal" } do
            for _, offset in ipairs { {0, 0}, {10, 0}, {0, 10} } do
                effects[#effects + 1] = sf.pan_tilt(sf.fixture(cell), sf.aim_offset(cell, offset[1], offset[2]))
            end
        end
        local circle = { shape = "circle", size = 10, measure = 4 }
        return sf.scene { effects, sf.phaser(sf.fixture("side"), ctx, circle) }"#,
    );
    let quarter = 10.0_f64.to_radians().sin();
    let rest_beams = beams(&rig, &render(&mut autoloop, &elapsed(0.0))[..6]);
    for (index, cell) in ["side", "diagonal"].into_iter().enumerate() {
        let [rest, spread, elevation] = rest_beams[3 * index..3 * index + 3] else {
            unreachable!()
        };
        let [spread, elevation] = turns(rest, &[spread, elevation])[..] else {
            unreachable!()
        };
        assert!(
            dot(spread, elevation).abs() < EXACT,
            "{cell}: {spread:?} {elevation:?}"
        );
        for turn in [spread, elevation] {
            assert!(
                (dot(turn, turn).sqrt() - quarter).abs() < EXACT,
                "{cell}: {turn:?}"
            );
        }
    }
    let side = rest_beams[0];
    let circle: Vec<_> = [0.0, 1.0]
        .map(|beats| beams(&rig, &render(&mut autoloop, &elapsed(beats))[6..])[0])
        .into();
    let [first, second] = turns(side, &circle)[..] else {
        unreachable!()
    };
    assert!(dot(first, second).abs() < EXACT, "{first:?} {second:?}");
    assert!((dot(first, first) - dot(second, second)).abs() < EXACT);
    assert!((dot(first, first).sqrt() - quarter).abs() < EXACT);
}

#[test]
fn aim_direction_throws_the_beam_along_the_vector_whatever_the_focus() {
    let rig = rest_beams();
    let throws = [
        ("down", [0.0, 0.0, -1.0]),
        ("down", [1.0, 2.0, -0.5]),
        ("slant", [-1.0, 0.0, 0.2]),
        ("up", [0.0, 0.0, 1.0]),
        ("up", [0.3, -1.0, 0.4]),
    ];
    let calls: Vec<String> = throws
        .iter()
        .map(|(cell, [x, y, z])| {
            format!("sf.pan_tilt(sf.fixture(\"{cell}\"), sf.aim_direction(\"{cell}\", {x:?}, {y:?}, {z:?}))")
        })
        .collect();
    let mut autoloop = autoloop(&rig, &format!("return {{{}}}", calls.join(", ")));
    for ((cell, direction), (_, pan, tilt)) in
        throws.iter().zip(render(&mut autoloop, &elapsed(0.0)))
    {
        assert_near(
            rig.beam_direction(cell, pan, tilt).unwrap(),
            unit(*direction),
            EXACT,
            &format!("{cell} {direction:?}"),
        );
    }
    let error = fails(
        &rig,
        r#"return {sf.pan_tilt(sf.fixture("up"), sf.aim_direction("up", 0, 0, 0))}"#,
    );
    assert!(error.contains("finite nonzero vector"), "{error}");
}

fn row(focus: impl Fn(f64) -> [f64; 3]) -> FixtureRig {
    let mut fixtures: Vec<Value> = ["a", "b", "c", "d", "m"]
        .into_iter()
        .zip([-3000.0, -1000.0, 1000.0, 3000.0, 0.0])
        .enumerate()
        .map(|(slot, (id, x))| {
            head(
                id,
                slot,
                support::mounted(true, 0.0, [x, 0.0, 4000.0]),
                focus(x),
            )
        })
        .collect();
    fixtures.extend([
        wash("wash", 5, [0.0, 0.0, 4000.0]),
        wash("wash_left", 6, [-5000.0, 0.0, 4000.0]),
        wash("wash_right", 7, [5000.0, 0.0, 4000.0]),
    ]);
    rig(
        Value::Array(fixtures),
        json!({
            "row": ["a", "b", "c", "d"],
            "odd": ["a", "m", "d"],
            "mixed": ["a", "wash", "d"],
            "ends": ["wash_left", "a", "b", "c", "d", "wash_right"],
        }),
    )
}

#[test]
fn fans_and_crosses_spread_partners_by_stage_position_whichever_way_they_throw() {
    for (throw, focus_y) in [("down", 0.0), ("upstage", 3000.0), ("downstage", -3000.0)] {
        let rig = row(|x| [x, focus_y, 0.0]);
        let mut autoloop = autoloop(
            &rig,
            r#"local across = { -1, -1 / 3, 1 / 3, 1 }
            local function offset(spread)
                return sf.each(sf.group("row"), function(head, index)
                    return sf.pan_tilt(head, sf.aim_offset(head, spread(index), 0))
                end)
            end
            return sf.scene {
                sf.phaser(sf.group("row"), ctx, { shape = "fan", size = 24 }),
                offset(function(index) return 24 * across[index] end),
                sf.phaser(sf.group("row"), ctx, { shape = "cross", size = 24 }),
                offset(function(index) return -24 * across[index] end),
                sf.phaser(sf.group("row"), ctx, { shape = "fan", size = 24, wings = false }),
                offset(function() return 24 end),
                sf.phaser(sf.group("row"), ctx, { shape = "fan", size = 24, measure = 8 }),
                offset(function() return 0 end),
            }"#,
        );
        let aims = render(&mut autoloop, &elapsed(4.0));
        let chunks: Vec<_> = aims.chunks(4).collect();
        for (index, label) in ["fan", "cross", "parallel fan", "closed fan"]
            .iter()
            .enumerate()
        {
            assert_same_aims(
                chunks[2 * index],
                chunks[2 * index + 1],
                &format!("{throw} {label}"),
            );
        }
        let fanned = beams(&rig, chunks[0]);
        for (index, cell) in ["a", "b", "c", "d"].into_iter().enumerate() {
            let (pan, tilt) = rig.aim(cell).unwrap();
            let rest = rig.beam_direction(cell, pan, tilt).unwrap();
            let outward = if index < 2 { -1.0 } else { 1.0 };
            assert!(
                (fanned[index][0] - rest[0]) * outward > 0.0,
                "{throw} {cell} spreads outward"
            );
        }
        for (left, right) in [(0, 3), (1, 2)] {
            let [x, y, z] = fanned[right];
            assert_near(fanned[left], [-x, y, z], EXACT, &format!("{throw} mirror"));
        }
    }
}

#[test]
fn washes_at_the_ends_of_a_group_neither_widen_the_row_nor_take_a_rank() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let mut autoloop = autoloop(
        &rig,
        r#"local fan = { shape = "fan", size = 24 }
        local circle = { shape = "circle", size = 10, measure = 4, phase = 1 }
        return sf.scene {
            sf.phaser(sf.group("ends"), ctx, fan), sf.phaser(sf.group("row"), ctx, fan),
            sf.phaser(sf.group("ends"), ctx, circle), sf.phaser(sf.group("row"), ctx, circle),
        }"#,
    );
    let aims = render(&mut autoloop, &elapsed(1.3));
    let chunks: Vec<_> = aims.chunks(4).collect();
    assert_same_aims(chunks[0], chunks[1], "fan");
    assert_same_aims(chunks[2], chunks[3], "circle");
}

#[test]
fn a_centre_member_circles_rather_than_nodding_in_a_line() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let mut autoloop = autoloop(
        &rig,
        r#"return sf.phaser(sf.group("odd"), ctx, { shape = "circle", size = 10, measure = 4 })"#,
    );
    let (pan, tilt) = rig.aim("m").unwrap();
    let rest = rig.beam_direction("m", pan, tilt).unwrap();
    let circle: Vec<_> = [0.0, 1.0]
        .map(|beats| beams(&rig, &render(&mut autoloop, &elapsed(beats)))[1])
        .into();
    let [first, second] = turns(rest, &circle)[..] else {
        unreachable!()
    };
    assert!(dot(first, second).abs() < EXACT, "{first:?} {second:?}");
    assert!((dot(first, first) - dot(second, second)).abs() < EXACT);
    assert!(dot(first, first) > 0.01, "{first:?}");
}

#[test]
fn phase_runs_the_wave_in_selection_order_and_mirror_partners_share_a_rank() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let phasers = |phase: f64| {
        autoloop(
            &rig,
            &format!(
                r#"return sf.scene {{
                    sf.phaser(sf.group("row"), ctx, {{ shape = "circle", size = 10, measure = 4, phase = {phase:?} }}),
                    sf.phaser(sf.group("row"), ctx, {{ shape = "circle", size = 10, measure = 4, phase = {phase:?}, wings = false }}),
                    sf.phaser(sf.group("row"), ctx, {{ shape = "tilt_wave", size = 10, measure = 4, phase = {phase:?} }}),
                }}"#
            ),
        )
    };
    let mut together = phasers(0.0);
    let lags = [
        [0.0, 2.0, 2.0, 0.0],
        [0.0, 1.0, 2.0, 3.0],
        [0.0, 1.0, 2.0, 3.0],
    ];
    for (phase, direction) in [(1.0, -1.0), (-1.0, 1.0)] {
        let mut spread = phasers(phase);
        for beats in [4.0, 5.3] {
            let aims = render(&mut spread, &elapsed(beats));
            for (chunk, lags) in lags.iter().enumerate() {
                for (member, lag) in lags.iter().enumerate() {
                    let index = 4 * chunk + member;
                    let expected =
                        &render(&mut together, &elapsed(beats + direction * lag))[index..=index];
                    let context = format!("phase {phase} phaser {chunk} member {member}");
                    assert_same_aims(&aims[index..=index], expected, &context);
                }
            }
        }
    }
}

#[test]
fn a_phaser_can_rest_on_a_point_or_along_a_direction_instead_of_the_focus() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let mut autoloop = autoloop(
        &rig,
        r#"local wall = { 0, 1, -1 }
        return sf.scene {
            sf.phaser(sf.group("row"), ctx, { shape = "fan", size = 0, at = { 0.5, 3, 0 } }),
            sf.phaser(sf.group("row"), ctx, { shape = "fan", size = 0, direction = wall }),
            sf.phaser(sf.group("row"), ctx, { shape = "circle", size = 10, measure = 4, wings = false, direction = wall }),
            sf.each(sf.group("row"), function(head)
                return sf.pan_tilt(head, sf.aim_offset(head, 10, 0, wall))
            end),
        }"#,
    );
    let aims = render(&mut autoloop, &elapsed(0.0));
    let chunks: Vec<_> = aims.chunks(4).collect();
    for (index, cell) in ["a", "b", "c", "d"].into_iter().enumerate() {
        let position = [-3000.0, -1000.0, 1000.0, 3000.0][index];
        let towards = unit([500.0 - position, 3000.0, -4000.0]);
        assert_near(
            beams(&rig, chunks[0])[index],
            towards,
            EXACT,
            &format!("{cell} at"),
        );
        assert_near(
            beams(&rig, chunks[1])[index],
            unit([0.0, 1.0, -1.0]),
            EXACT,
            cell,
        );
    }
    assert_same_aims(chunks[2], chunks[3], "circle along a direction");
    for (spec, error) in [
        ("at = { 0, 0, 0 }, direction = { 0, 0, -1 }", "not both"),
        ("direction = { 0, 0, 0 }", "finite nonzero"),
        ("at = { 0, 0 }", "phaser at must be finite"),
    ] {
        let message = fails(
            &rig,
            &format!(
                r#"return sf.phaser(sf.group("row"), ctx, {{ shape = "fan", size = 5, {spec} }})"#
            ),
        );
        assert!(message.contains(error), "{spec}: {message}");
    }
}

#[test]
fn every_shape_is_a_pure_periodic_function_of_the_elapsed_beats() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    for shape in ["fan", "cross", "circle", "figure8", "tilt_wave", "ballyhoo"] {
        let body = format!(
            r#"return sf.phaser(sf.group("mixed"), ctx, {{ shape = "{shape}", size = 15, tilt_size = 10, measure = 8, phase = 0.5 }})"#
        );
        let (mut first, mut second) = (autoloop(&rig, &body), autoloop(&rig, &body));
        let mut seen = Vec::new();
        for beats in [0.0, 0.75, 3.5, 6.25] {
            let aims = render(&mut first, &elapsed(beats));
            assert_eq!(
                aims,
                render(&mut second, &elapsed(beats)),
                "{shape} is pure"
            );
            let cycle_later = RenderContext {
                time_seconds: 1.0,
                beat_position: 2.0,
                ..elapsed(beats + 8.0)
            };
            assert_same_aims(&aims, &render(&mut first, &cycle_later), shape);
            let cells: Vec<_> = aims.iter().map(|(cell, ..)| cell.as_str()).collect();
            assert_eq!(cells, ["a", "d"], "{shape}: the wash has no pan range");
            if shape == "ballyhoo" {
                let [left, [x, y, z]] = beams(&rig, &aims)[..] else {
                    unreachable!()
                };
                assert!((0..3).any(|axis| (left[axis] - [-x, y, z][axis]).abs() > 1e-3));
            }
            seen.push(aims);
        }
        assert!(
            seen.windows(2).all(|pair| pair[0] != pair[1]),
            "{shape} moves"
        );
    }
}

#[test]
fn measures_outside_the_powers_of_two_and_malformed_specs_are_refused() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let phaser = |spec: &str| format!(r#"return sf.phaser(sf.group("row"), ctx, {{ {spec} }})"#);
    for measure in [1, 2, 4, 8, 16, 32] {
        let mut autoloop = autoloop(
            &rig,
            &phaser(&format!(
                r#"shape = "circle", size = 10, measure = {measure}"#
            )),
        );
        assert_eq!(render(&mut autoloop, &elapsed(0.5)).len(), 4);
    }
    let mut tilt_only = autoloop(
        &rig,
        &phaser(r#"shape = "tilt_wave", tilt_size = 8, measure = 16"#),
    );
    assert_eq!(render(&mut tilt_only, &elapsed(0.5)).len(), 4);
    for measure in ["0", "0.3", "3", "6", "12", "100"] {
        let message = fails(
            &rig,
            &phaser(&format!(
                r#"shape = "circle", size = 10, measure = {measure}"#
            )),
        );
        assert!(
            message.contains("measure must be 1, 2, 4, 8, 16 or 32 beats"),
            "{measure}: {message}"
        );
    }
    for (spec, error) in [
        (
            r#"shape = "spiral", size = 10, measure = 4"#,
            "unknown phaser shape",
        ),
        (
            r#"shape = "circle", size = 10"#,
            "only a fan or cross holds still",
        ),
        (
            r#"shape = "fan", size = 10, phase = 1.5"#,
            "phase must be within -1..1",
        ),
        (r#"shape = "fan""#, "sizes and centres must be finite"),
        (
            r#"shape = "tilt_wave", measure = 4"#,
            "sizes and centres must be finite",
        ),
    ] {
        let message = fails(&rig, &phaser(spec));
        assert!(message.contains(error), "{spec}: {message}");
    }
}

#[test]
fn centre_offsets_shift_every_member_before_the_shape() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let mut autoloop = autoloop(
        &rig,
        r#"return sf.scene {
            sf.phaser(sf.group("row"), ctx, { shape = "tilt_wave", size = 10, measure = 4, centre_pan = 15, centre_tilt = -20 }),
            sf.each(sf.group("row"), function(head)
                return sf.pan_tilt(head, sf.aim_offset(head, 15, -20 + 10 * math.sin(2 * math.pi / 4)))
            end),
        }"#,
    );
    let aims = render(&mut autoloop, &elapsed(1.0));
    assert_same_aims(&aims[..4], &aims[4..], "centred tilt_wave");
}

const MOVERS_SWEEP: &str = r#"return { render = function(ctx)
    local x = sf.osc.triangle(sf.section_beats(ctx), 16, -4, 4)
    local effects = { sf.dimmer(sf.group("movers"), 1) }
    for _, cell in ipairs(sf.members(sf.group("movers"))) do
        local pan, tilt = sf.aim(cell, x, -1, 0)
        effects[#effects + 1] = sf.pan_tilt(sf.fixture(cell), pan, tilt)
    end
    return sf.scene(effects)
end }"#;

fn physical_pan_degrees(frame: &starfire::engine::RenderedFrame, fixture: &wire::Fixture) -> f64 {
    let start = fixture.address as usize - 1;
    let dmx = &frame.dmx_frames_by_universe[&(fixture.universe as u16)][start..start + 2];
    let (from, to) = (-220.0, 320.0);
    from + (to - from) * f64::from(u16::from_be_bytes([dmx[0], dmx[1]])) / 65535.0
}

/// The generic stage hangs both movers facing downstage (MVR yaw 180), so a target walked along a
/// line in front of them passes the heading where one pan solution runs out at an end stop. The
/// head has the other solution within reach and must keep to it, not swing a full turn back.
#[test]
fn a_target_walked_across_the_stage_keeps_each_mover_on_a_continuous_pan() {
    let definition = support::stage_definition();
    let movers: Vec<_> = ["mover_left", "mover_right"]
        .map(|name| definition.fixtures.iter().find(|f| f.name == name).unwrap())
        .into();
    for intensity in [0.5, 1.0] {
        let catalog =
            support::directory_catalog("test", &[("sweep", support::base_loop(MOVERS_SWEEP))])
                .unwrap();
        let mut engine = Engine::new(support::stage(), catalog, support::palettes()).unwrap();
        engine.apply(0.0, &support::beat(0, None, 120.0)).unwrap();
        engine
            .apply(0.0, &Command::SetIntensity { level: intensity })
            .unwrap();
        engine
            .apply(
                0.0,
                &support::select("test", "sweep", Transition::default()),
            )
            .unwrap();
        let mut previous: Vec<Option<f64>> = vec![None; movers.len()];
        let mut travel = vec![0.0; movers.len()];
        // Two 16-beat out-and-back walks at 120 bpm, 40 frames a second.
        for frame_index in 0..=640 {
            let frame = engine.render_at(f64::from(frame_index) / 40.0).unwrap();
            for (index, fixture) in movers.iter().enumerate() {
                let pan = physical_pan_degrees(&frame, fixture);
                if let Some(previous) = previous[index] {
                    let step = (pan - previous).abs();
                    assert!(
                        step <= 90.0,
                        "{} at intensity {intensity}, frame {frame_index}: pan {previous:.1} -> {pan:.1}",
                        fixture.name
                    );
                    travel[index] += step;
                }
                previous[index] = Some(pan);
            }
        }
        // Each walk turns a head through about 95 degrees out and back; a full turn on top of
        // that is the long way round, however slowly the slew limit lets it happen.
        for (fixture, travel) in movers.iter().zip(travel) {
            assert!(
                travel < 4.0 * 130.0,
                "{} at intensity {intensity}: {travel:.0} degrees of pan travel",
                fixture.name
            );
        }
    }
}

const SPIN: &str = r#"return { render = function(ctx)
    return sf.scene {
        sf.dimmer(sf.group("movers"), 1),
        sf.phaser(sf.group("movers"), ctx, {
            shape = "fan", size = 10, spin = { measure = 8, every = 16, at = 4 },
        }),
    }
end }"#;

/// mover_left refocused upstage rests 111 degrees off pan centre, which leaves a full circle one
/// way; mover_right still rests mid-range and has 320 and 220 degrees, so it never spins.
#[test]
fn sf_spin_turns_a_member_with_a_full_circle_of_pan_once_round_and_back() {
    let mut definition = support::stage_definition();
    let left = definition
        .fixtures
        .iter_mut()
        .find(|f| f.name == "mover_left")
        .unwrap();
    left.focus = vec![-2000.0, 3000.0, 0.0];
    let movers: Vec<wire::Fixture> = definition
        .fixtures
        .iter()
        .filter(|f| f.name.starts_with("mover"))
        .cloned()
        .collect();
    let rig = FixtureRig::from_wire(&definition).unwrap();
    let reach = |name: &str| rig.pan_reach(rig.uuid_of_label(name).unwrap()).unwrap();
    let (clockwise, anticlockwise) = reach("mover_left");
    assert!((clockwise - 158.4).abs() < 0.5 && (anticlockwise - 381.6).abs() < 0.5);
    let (clockwise, anticlockwise) = reach("mover_right");
    assert!((clockwise - 320.0).abs() < 0.5 && (anticlockwise - 220.0).abs() < 0.5);

    let catalog =
        support::directory_catalog("test", &[("spin", support::base_loop(SPIN))]).unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    engine.apply(0.0, &support::beat(0, None, 120.0)).unwrap();
    engine
        .apply(0.0, &support::select("test", "spin", Transition::default()))
        .unwrap();
    let mut pans = vec![Vec::new(); movers.len()];
    // 16 beats at 120 bpm, 40 frames a second: rest, an 8-beat spin from beat 4, rest.
    for frame_index in 0..=320 {
        let frame = engine.render_at(f64::from(frame_index) / 40.0).unwrap();
        for (index, fixture) in movers.iter().enumerate() {
            pans[index].push(physical_pan_degrees(&frame, fixture));
        }
    }
    for (fixture, pans) in movers.iter().zip(&pans) {
        let steps: Vec<f64> = pans.windows(2).map(|pair| pair[1] - pair[0]).collect();
        let big: Vec<_> = steps
            .iter()
            .enumerate()
            .filter(|(_, s)| s.abs() > 20.0)
            .map(|(i, s)| (i, *s, pans[i]))
            .collect();
        assert!(big.is_empty(), "{} jumps {big:?}", fixture.name);
        let travel: f64 = steps.iter().map(|step| step.abs()).sum();
        // The spin runs over frames 80..240 and is a full circle round at beat 8, frame 160.
        let (rest, after) = (pans[40], pans[300]);
        assert!((rest - after).abs() < 1.0, "{} rests", fixture.name);
        if fixture.name == "mover_left" {
            let lowest = pans.iter().copied().fold(f64::INFINITY, f64::min);
            // At the top of the circle the look has faded: the focus aim (heading -18.4 degrees,
            // yaw 180), a whole turn below where it rests.
            let focus_pan = (-500.0_f64).atan2(1500.0).to_degrees() + 180.0;
            assert!((pans[160] - (focus_pan - 360.0)).abs() < 0.5 && lowest == pans[160]);
            let round = rest - pans[160];
            assert!(
                (travel - 2.0 * round).abs() < 30.0,
                "mover_left travels {travel:.1}"
            );
        } else {
            assert!(travel < 1.0, "{} travels {travel:.1}", fixture.name);
        }
    }
}
