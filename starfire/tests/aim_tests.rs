//! Aim geometry (`FixtureRig::aim_at`, `beam_direction`) and the Lua aim and phaser helpers, on
//! synthetic heads placed on grids.

mod support;

use serde_json::{Value, json};
use starfire::fixtures::FixtureRig;
use starfire::model::{Effect, PropertyValues, RenderContext, Target};
use starfire::protocol::wire;
use starfire::script::{AutoloopCatalog, LuaAutoloop};

const HEAD: &str = "scanner";
const PAN_TRAVEL: f64 = 540.0;
const TILT_TRAVEL: f64 = 270.0;
// One 8-bit step is 2.1 degrees of pan and 1.1 of tilt: about 0.04 of a unit vector together.
const DMX_STEP: f64 = 0.05;
const EXACT: f64 = 1e-9;

/// Heads are the compiled `beam_scanner` test description (Pan 0..540, Tilt 0..270, 8 bit);
/// washes are Generic RGB.
fn rig(fixtures: Value, groups: Value) -> FixtureRig {
    let mut definition = support::definition(&json!({ "fixtures": fixtures, "groups": groups }));
    definition
        .profiles
        .push(support::compiled_profile("beam_scanner#6ch", HEAD));
    FixtureRig::from_wire(&definition).unwrap()
}

/// An MVR Matrix yawed by `yaw` degrees at `position`, flipped about its facing when standing.
fn mounted(hanging: bool, yaw: f64, position: [f64; 3]) -> Vec<f64> {
    let (sin, cos) = yaw.to_radians().sin_cos();
    let flip = if hanging { 1.0 } else { -1.0 };
    [
        [flip * cos, flip * sin, 0.0],
        [-sin, cos, 0.0],
        [0.0, 0.0, flip],
        position,
    ]
    .concat()
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
        "matrix": mounted(true, 0.0, position),
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
        // Points on the side the head throws to: below a hanging one, above a standing one.
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
                    mounted(hanging, yaw, position),
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

/// The scene direction an 8-bit Pan and Tilt pair throws the beam, decoded independently of
/// the encoder: physical pan 0 faces local +Y and turns clockwise about local +Z, mid tilt runs
/// along local -Z and leans towards the pan heading, and standing is the rigid flip.
fn thrown(dmx: &[u8], matrix: &[f64]) -> [f64; 3] {
    let share = |byte: u8| f64::from(byte) / 255.0;
    let pan = (share(dmx[0]) * PAN_TRAVEL).to_radians();
    let tilt = ((share(dmx[1]) - 0.5) * TILT_TRAVEL).to_radians();
    let body = [tilt.sin() * pan.sin(), tilt.sin() * pan.cos(), -tilt.cos()];
    [0, 1, 2].map(|axis| (0..3).map(|row| matrix[3 * row + axis] * body[row]).sum())
}

#[test]
fn beam_direction_is_where_the_encoded_dmx_throws_the_beam() {
    for hanging in [true, false] {
        for yaw in [0.0, 37.0, 180.0, -90.0] {
            let position = [500.0, 1000.0, if hanging { 3000.0 } else { 0.0 }];
            let matrix = mounted(hanging, yaw, position);
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
                mounted(true, 0.0, [-2000.0, 0.0, 3000.0]),
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
    AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "autoloop".into(),
        bank: "test".into(),
        lua_source: format!("return {{render = function(ctx)\n{body}\nend}}"),
    }])
    .unwrap()
    .instantiate("test", "autoloop", rig, &support::palettes())
    .unwrap()
}

/// `beats` since the loop started, with the beat position and clock elsewhere: phasers count
/// from the section or loop start.
fn elapsed(beats: f64) -> RenderContext {
    RenderContext {
        time_seconds: beats + 5.0,
        beat_position: beats + 13.0,
        autoloop_elapsed_beats: beats,
        autoloop_speed_scaled_elapsed_beats: beats,
        ..Default::default()
    }
}

/// A plain sf.pan_tilt effect's cell, pan and tilt: it sets nothing else, so composition, slew
/// and soft limits treat it as any loop's.
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
fn sf_aim_and_sf_focus_read_the_rig_in_metres_and_a_wash_aims_nowhere() {
    let rig = rig(
        json!([
            head(
                "head",
                0,
                mounted(true, 37.0, [-1000.0, 500.0, 4000.0]),
                [-2000.0, 3000.0, 0.0]
            ),
            wash("wash", 1, [3000.0, -500.0, 3000.0]),
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
                table.pack(sf.aim_offset("wash", 10, 5)),
                table.pack(sf.aim_direction("wash", 0, 0, 1)),
            }} do
                assert(aim.n == 2 and aim[1] == nil and aim[2] == nil, "a wash aims nowhere")
            end
            return {{sf.pan_tilt(sf.fixture("head"), sf.aim("head", 1.5, 2, 0))}}"#
        ),
    );
    let aims = render(&mut autoloop, &elapsed(0.0));
    let (pan, tilt) = rig.aim_at("head", [1500.0, 2000.0, 0.0]).unwrap();
    assert_same_aims(&aims, &[("head".into(), pan, tilt)], "sf.aim");
    let error = fails(
        &rig,
        r#"return {sf.pan_tilt(sf.fixture("head"), sf.aim("head", 0/0, 0, 0))}"#,
    );
    assert!(error.contains("aim point must be finite"), "{error}");
    let error = fails(
        &rig,
        r#"return {sf.pan_tilt(sf.fixture("head"), sf.aim("nobody", 0, 0, 0))}"#,
    );
    assert!(error.contains("unknown cell nobody"), "{error}");
}

/// Heads whose rest beams run straight down, straight up, upstage, cross-stage and on a slant.
fn rest_beams() -> FixtureRig {
    rig(
        json!([
            head(
                "down",
                0,
                mounted(true, 0.0, [0.0, 0.0, 4000.0]),
                [0.0, 0.0, 0.0]
            ),
            head(
                "up",
                1,
                mounted(false, 0.0, [0.0, 1000.0, 0.0]),
                [0.0, 1000.0, 3000.0]
            ),
            head(
                "upstage",
                2,
                mounted(true, 30.0, [-2000.0, 0.0, 3000.0]),
                [-2000.0, 4000.0, 3000.0]
            ),
            head(
                "across",
                3,
                mounted(true, -90.0, [2000.0, 0.0, 3000.0]),
                [6000.0, 0.0, 3000.0]
            ),
            head(
                "slant",
                4,
                mounted(true, 0.0, [1000.0, -500.0, 3000.0]),
                [-500.0, 2500.0, 0.0]
            ),
        ]),
        Value::Null,
    )
}

#[test]
fn aim_offset_spreads_towards_x_and_elevates_towards_z_round_any_rest_beam() {
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
                // Along Z the elevation turns about X; along X the spread turns about Z.
                let expected = match cell {
                    "down" => Some([cos_e * sin_s, sin_e, -cos_e * cos_s]),
                    "up" => Some([cos_e * sin_s, -sin_e, cos_e * cos_s]),
                    "upstage" => Some([cos_e * sin_s, cos_e * cos_s, sin_e]),
                    "across" => Some([cos_e * cos_s, cos_e * sin_s, sin_e]),
                    _ => None,
                };
                if let Some(expected) = expected {
                    assert_near(beam, expected, EXACT, &context);
                } else if elevation == 0.0 {
                    let turned = dot(beam, slant).clamp(-1.0, 1.0).acos().to_degrees();
                    assert!((turned - spread.abs()).abs() < 1e-7, "{context}: {turned}");
                    assert!(
                        dot(cross(slant, [1.0, 0.0, 0.0]), beam).abs() < EXACT,
                        "{context}"
                    );
                    assert!(spread == 0.0 || (beam[0] - slant[0]).signum() == spread.signum());
                } else if spread == 0.0 {
                    let turned = dot(beam, slant).clamp(-1.0, 1.0).acos().to_degrees();
                    assert!(
                        (turned - elevation.abs()).abs() < 1e-7,
                        "{context}: {turned}"
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

/// Hanging heads a, b, c, d across the stage at x = -3, -1, 1, 3 m, each focused at `focus(x)`,
/// as group "row"; "mixed" is the outer two with a wash between them.
fn row(focus: impl Fn(f64) -> [f64; 3]) -> FixtureRig {
    let mut fixtures: Vec<Value> = ["a", "b", "c", "d"]
        .into_iter()
        .zip([-3000.0, -1000.0, 1000.0, 3000.0])
        .enumerate()
        .map(|(slot, (id, x))| head(id, slot, mounted(true, 0.0, [x, 0.0, 4000.0]), focus(x)))
        .collect();
    fixtures.push(wash("wash", 4, [0.0, 0.0, 4000.0]));
    rig(
        Value::Array(fixtures),
        json!({"row": ["a", "b", "c", "d"], "mixed": ["a", "wash", "d"]}),
    )
}

#[test]
fn fans_and_crosses_spread_partners_by_stage_position_whichever_way_they_throw() {
    // Focus points on the floor straight below each head, upstage of it and downstage of it.
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
        // Half way through an 8-beat cycle a moving fan is closed.
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
        let beams: Vec<_> = chunks[0]
            .iter()
            .map(|(cell, pan, tilt)| rig.beam_direction(cell, *pan, *tilt).unwrap())
            .collect();
        for (index, cell) in ["a", "b", "c", "d"].into_iter().enumerate() {
            let (pan, tilt) = rig.aim(cell).unwrap();
            let rest = rig.beam_direction(cell, pan, tilt).unwrap();
            let outward = if index < 2 { -1.0 } else { 1.0 };
            assert!(
                (beams[index][0] - rest[0]) * outward > 0.0,
                "{throw} {cell} spreads outward"
            );
        }
        for (left, right) in [(0, 3), (1, 2)] {
            let [x, y, z] = beams[right];
            assert_near(beams[left], [-x, y, z], EXACT, &format!("{throw} mirror"));
        }
    }
}

#[test]
fn phase_spreads_ranks_across_the_cycle_and_mirror_partners_share_one() {
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
    let (mut spread, mut together) = (phasers(1.0), phasers(0.0));
    // In beats of the 4-beat cycle: two ranks with wings, four without, and tilt_wave has none.
    let shifts = [
        [0.0, 2.0, 2.0, 0.0],
        [0.0, 1.0, 2.0, 3.0],
        [0.0, 1.0, 2.0, 3.0],
    ];
    for beats in [0.0, 1.3] {
        let aims = render(&mut spread, &elapsed(beats));
        for (chunk, shifts) in shifts.iter().enumerate() {
            for (member, shift) in shifts.iter().enumerate() {
                let index = 4 * chunk + member;
                let expected = &render(&mut together, &elapsed(beats + shift))[index..=index];
                assert_same_aims(
                    &aims[index..=index],
                    expected,
                    &format!("phaser {chunk} member {member}"),
                );
            }
        }
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
            seen.push(aims);
        }
        assert!(
            seen.windows(2).all(|pair| pair[0] != pair[1]),
            "{shape} moves"
        );
    }
}

#[test]
fn measures_snap_to_powers_of_two_and_malformed_specs_are_refused() {
    let rig = row(|x| [x, 2000.0, 0.0]);
    let circle = |measure: &str| {
        autoloop(
            &rig,
            &format!(
                r#"return sf.phaser(sf.group("row"), ctx, {{ shape = "circle", size = 10, measure = {measure} }})"#
            ),
        )
    };
    for (asked, snapped) in [
        ("0.3", "1"),
        ("3", "4"),
        ("6", "8"),
        ("11", "8"),
        ("12", "16"),
        ("100", "32"),
    ] {
        let (mut asked_loop, mut snapped_loop) = (circle(asked), circle(snapped));
        for beats in [0.5, 2.25, 7.0] {
            assert_eq!(
                render(&mut asked_loop, &elapsed(beats)),
                render(&mut snapped_loop, &elapsed(beats)),
                "measure {asked}"
            );
        }
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
            r#"shape = "circle", size = 10, measure = 0"#,
            "measure must be positive",
        ),
        (
            r#"shape = "fan", size = 10, phase = 1.5"#,
            "phase must be within 0..1",
        ),
        (r#"shape = "fan""#, "sizes and centres must be finite"),
    ] {
        let message = fails(
            &rig,
            &format!(r#"return sf.phaser(sf.group("row"), ctx, {{ {spec} }})"#),
        );
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
