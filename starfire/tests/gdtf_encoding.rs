mod support;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::gdtf::GdtfProfile;
use starfire::model::{
    AttributeValue, Command, FixtureState, LightingOverride, OverrideLifetime, PropertyValues, Rgb,
    Target, Transition,
};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

// Modes are built as Autolux's GDTF compiler emits them; parsing is tested in Python.
const ROOT_GEOMETRY: &str = "Base";

fn function(attribute: &str, name: &str) -> wire::ChannelFunction {
    wire::ChannelFunction {
        path: name.into(),
        attribute: attribute.into(),
        physical_from: 0.0,
        physical_to: 100.0,
        ..Default::default()
    }
}

fn starting(mut function: wire::ChannelFunction, dmx_from: u32) -> wire::ChannelFunction {
    function.dmx_from = dmx_from;
    function
}

fn mastered(
    mut function: wire::ChannelFunction,
    channel: u32,
    master_function: Option<u32>,
    range: (u32, u32),
) -> wire::ChannelFunction {
    function.mode_master = Some(wire::ModeMaster {
        channel,
        function: master_function,
        dmx_from: range.0,
        dmx_to: range.1,
    });
    function
}

fn set(name: &str, dmx_from: u32, dmx_to: u32, physical: (f64, f64)) -> wire::ChannelSet {
    wire::ChannelSet {
        name: name.into(),
        dmx_from,
        dmx_to,
        physical_from: physical.0,
        physical_to: physical.1,
        wheel_slot_colour: None,
    }
}

/// One DMX channel whose first logical channel is `logical`; function paths and geometry
/// qualification follow GDTF naming (`Geometry_Logical.Logical.Function`).
fn channel_with(
    logical: &[(&str, Vec<wire::ChannelFunction>)],
    geometry: &str,
    offsets: &[u32],
) -> wire::DmxChannel {
    let channel_name = format!("{geometry}_{}", logical[0].0);
    let functions = logical
        .iter()
        .flat_map(|(attribute, functions)| {
            let channel_name = &channel_name;
            functions.iter().cloned().map(move |mut function| {
                function.path = format!("{channel_name}.{attribute}.{}", function.path);
                function.qualified_attribute = format!("{geometry}.{}", function.attribute);
                function
            })
        })
        .collect();
    let mut geometry_path = vec![ROOT_GEOMETRY.to_owned()];
    if geometry != ROOT_GEOMETRY {
        geometry_path.push(geometry.into());
    }
    wire::DmxChannel {
        geometry_path,
        offsets: offsets.to_vec(),
        default: 0,
        functions,
    }
}

fn channel(
    attribute: &str,
    geometry: &str,
    offsets: &[u32],
    functions: Vec<wire::ChannelFunction>,
) -> wire::DmxChannel {
    channel_with(&[(attribute, functions)], geometry, offsets)
}

fn plain(attribute: &str, geometry: &str, offset: u32) -> wire::DmxChannel {
    channel(
        attribute,
        geometry,
        &[offset],
        vec![function(attribute, "Control")],
    )
}

fn defaulted(mut channel: wire::DmxChannel, default: u32) -> wire::DmxChannel {
    channel.default = default;
    channel
}

fn mode(channels: Vec<wire::DmxChannel>) -> wire::DmxMode {
    wire::DmxMode { channels }
}

fn profile(channels: Vec<wire::DmxChannel>) -> GdtfProfile {
    GdtfProfile::from_wire(&mode(channels)).unwrap()
}

fn values(items: &[(&str, AttributeValue)]) -> FixtureState {
    FixtureState {
        attributes: items
            .iter()
            .map(|(key, value)| ((*key).into(), value.clone()))
            .collect(),
        ..FixtureState::default()
    }
}

fn encode(profile: &GdtfProfile, state: &FixtureState) -> Vec<u8> {
    let mut output = vec![0; profile.footprint()];
    profile.encode(state, &mut output).unwrap();
    output
}

#[test]
fn physical_values_preserve_all_channel_resolutions_and_arbitrary_offsets() {
    for (offsets, expected) in [
        (vec![1], vec![128]),
        (vec![2, 1], vec![0, 128]),
        (vec![3, 1, 2], vec![0, 0, 128]),
        (vec![4, 1, 3, 2], vec![0, 0, 0, 128]),
    ] {
        let profile = profile(vec![channel(
            "Fog1",
            "Base",
            &offsets,
            vec![function("Fog1", "Control")],
        )]);
        assert_eq!(
            encode(&profile, &values(&[("Fog1", AttributeValue::Number(50.0))])),
            expected
        );
        assert_eq!(
            encode(
                &profile,
                &values(&[("Fog1", AttributeValue::Number(100.0))])
            ),
            vec![255; expected.len()]
        );
    }
}

#[test]
fn compiled_defaults_are_written_at_channel_resolution() {
    for (default, expected) in [(0x1212, [18, 18]), (0x1200, [18, 0])] {
        let profile = profile(vec![defaulted(
            channel("Fog1", "Base", &[1, 2], vec![function("Fog1", "Control")]),
            default,
        )]);
        assert_eq!(encode(&profile, &FixtureState::default()), expected);
    }
}

fn rig_with(mode: wire::DmxMode, fixture: wire::Fixture) -> FixtureRig {
    FixtureRig::from_wire(&wire::RigDefinition {
        fixtures: vec![wire::Fixture {
            area: wire::StageArea::Centre as i32,
            profile_id: "test".into(),
            address: 1,
            ..fixture
        }],
        profiles: vec![wire::FixtureProfile {
            profile_id: "test".into(),
            dmx_mode: "Control".into(),
            mode: Some(mode),
        }],
        ..Default::default()
    })
    .unwrap()
}

#[test]
fn physical_pan_values_obey_patch_calibration() {
    let rig = rig_with(
        mode(vec![channel(
            "Pan",
            "Base",
            &[1, 2],
            vec![function("Pan", "Control")],
        )]),
        wire::Fixture {
            area: wire::StageArea::Centre as i32,
            fixture_uuid: "mover".into(),
            name: "mover".into(),
            pan: Some(wire::fixture::MovementRange {
                minimum: Some(0.25),
                maximum: Some(0.75),
                inverted: true,
            }),
            ..Default::default()
        },
    );
    let mut states = rig.empty_states();
    states.insert(
        "mover".into(),
        values(&[("Pan", AttributeValue::Number(0.0))]),
    );
    assert_eq!(&rig.encode(&states).unwrap()[&0][..2], [191, 255]);
    states.insert(
        "mover".into(),
        values(&[("Pan", AttributeValue::Number(100.0))]),
    );
    assert_eq!(&rig.encode(&states).unwrap()[&0][..2], [64, 0]);
}

#[test]
fn a_pan_sweep_covers_exactly_the_soft_limits_on_an_eight_second_triangle() {
    let rig = rig_with(
        mode(vec![channel(
            "Pan",
            "Base",
            &[1, 2],
            vec![function("Pan", "Control")],
        )]),
        wire::Fixture {
            area: wire::StageArea::Centre as i32,
            fixture_uuid: "mover".into(),
            name: "mover".into(),
            pan: Some(wire::fixture::MovementRange {
                minimum: Some(0.25),
                maximum: Some(0.75),
                inverted: false,
            }),
            ..Default::default()
        },
    );
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "idle".into(),
        bank: "test".into(),
        lua_source: "return {render=function(ctx) return sf.scene {} end}".into(),
    }])
    .unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    let sweep = LightingOverride {
        id: "highlight".into(),
        target: Target::Fixture("mover".into()),
        values: PropertyValues {
            tilt: Some(0.5),
            ..Default::default()
        },
        intensity_multiplier: None,
        lifetime: OverrideLifetime::UntilCleared,
        priority: 0,
        special_fx: None,
        pan_sweep: true,
        aim_upstage: false,
    };
    engine
        .apply(0.0, &Command::SetOverride { value: sweep })
        .unwrap();
    // Two full cycles on the render clock, 10 ms frames.
    let pan: Vec<u16> = (0..=1600)
        .map(|frame| {
            let dmx = &engine
                .render_at(f64::from(frame) / 100.0)
                .unwrap()
                .dmx_frames_by_universe[&0];
            u16::from_be_bytes([dmx[0], dmx[1]])
        })
        .collect();
    // The soft limits 0.25 and 0.75 at 16 bits, as the patch calibration encodes them.
    let (low, high) = (16384, 49151);
    assert_eq!(pan.iter().min(), Some(&low));
    assert_eq!(pan.iter().max(), Some(&high));
    assert_eq!(
        [pan[0], pan[400], pan[800], pan[1200]],
        [low, high, low, high]
    );
}

#[test]
fn an_upstage_aim_throws_the_beam_towards_plus_y_at_any_yaw_and_mount() {
    // Independent of the encoder: decode the DMX to body angles and carry the beam through the
    // Matrix, as fixture_encoding's mount test does. The pan range centre faces local +Y, the
    // display, and pan turns clockwise about local +Z; mid tilt is on the pan axis and tilts
    // towards the pan heading; standing is the rigid flip.
    let probe = support::rig(serde_json::json!({"fixtures": [{
        "fixture_uuid": "test", "profile": "chauvet_intimidator_spot_110_12ch",
        "universe": 0, "address": 1,
    }]}));
    let ranges =
        ["Pan", "Tilt"].map(|axis| probe.profile("test").unwrap().movement_range(axis).unwrap());
    // The 16-bit channel pair at `at` as a body angle off its range centre.
    let decode = |frame: &[u8], at: usize, (from, to): (f64, f64)| {
        let share = f64::from(u16::from_be_bytes([frame[at], frame[at + 1]])) / 65535.0;
        (from + share * (to - from) - (from + to) / 2.0).to_radians()
    };
    let aimed = |hanging: bool, yaw: f64, inverted: bool| {
        let (sin, cos) = f64::to_radians(yaw).sin_cos();
        let flip = if hanging { 1.0 } else { -1.0 };
        let [u, v, w] = [
            [flip * cos, flip * sin, 0.0],
            [-sin, cos, 0.0],
            [0.0, 0.0, flip],
        ];
        let rig = support::rig(serde_json::json!({"fixtures": [{
            "fixture_uuid": "test", "profile": "chauvet_intimidator_spot_110_12ch",
            "universe": 0, "address": 1, "matrix": ([u, v, w, [0.0; 3]].concat()),
            "pan": {"min": 0.1, "max": 0.8, "inverted": inverted},
            "tilt": {"inverted": inverted},
        }]}));
        let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
            autoloop_id: "idle".into(),
            bank: "test".into(),
            lua_source: "return {render=function(ctx) return sf.scene {} end}".into(),
        }])
        .unwrap();
        let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
        let aim = LightingOverride {
            id: "highlight".into(),
            target: Target::Fixture("test".into()),
            values: PropertyValues {
                intensity: Some(1.0),
                ..Default::default()
            },
            intensity_multiplier: None,
            lifetime: OverrideLifetime::UntilCleared,
            priority: 0,
            special_fx: None,
            pan_sweep: false,
            aim_upstage: true,
        };
        engine
            .apply(0.0, &Command::SetOverride { value: aim })
            .unwrap();
        let frame = &engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0];
        let (pan, tilt) = (decode(frame, 0, ranges[0]), decode(frame, 2, ranges[1]));
        let body = [tilt.sin() * pan.sin(), tilt.sin() * pan.cos()];
        [0, 1].map(|axis| u[axis] * body[0] + v[axis] * body[1])
    };
    let upstage = [0.0, f64::to_radians(45.0).sin()];
    for yaw in [0.0, 37.0, 90.0, 130.0, 180.0, 233.0, 270.0, -90.0, -130.0] {
        for hanging in [true, false] {
            for inverted in [false, true] {
                let beam = aimed(hanging, yaw, inverted);
                assert!(
                    (0..2).all(|axis| (beam[axis] - upstage[axis]).abs() < 1e-3),
                    "hanging {hanging} yaw {yaw} inverted {inverted}: {beam:?}"
                );
            }
        }
    }
}

#[test]
fn geometry_qualification_keeps_cells_independent_and_rejects_ambiguous_names() {
    let profile = profile(vec![plain("Fog1", "Head1", 1), plain("Fog1", "Head2", 3)]);
    let state = values(&[
        ("Head1.Fog1", AttributeValue::Number(100.0)),
        ("Head2_Fog1.Fog1.Control", AttributeValue::Number(50.0)),
    ]);
    assert_eq!(encode(&profile, &state), [255, 0, 128]);
    assert!(
        profile
            .validate_attributes(&values(&[("Fog1", AttributeValue::Number(50.0))]).attributes)
            .is_err()
    );
}

#[test]
fn physical_ranges_can_be_inverted_and_must_not_be_exceeded() {
    let mut zoom = function("Zoom", "Zoom");
    zoom.physical_from = 60.0;
    zoom.physical_to = 10.0;
    let profile = profile(vec![channel("Zoom", "Base", &[1, 2], vec![zoom])]);
    assert_eq!(
        encode(&profile, &values(&[("Zoom", AttributeValue::Number(60.0))])),
        [0, 0]
    );
    assert_eq!(
        encode(&profile, &values(&[("Zoom", AttributeValue::Number(10.0))])),
        [255, 255]
    );
    for invalid in [f64::NAN, 9.0, 61.0] {
        assert!(
            profile
                .validate_attributes(
                    &values(&[("Zoom", AttributeValue::Number(invalid))]).attributes
                )
                .is_err()
        );
    }
}

#[test]
fn channel_sets_are_explicit_presets_and_do_not_crossfade_through_other_gobos() {
    let mut gobo = function("Gobo1", "Gobo");
    gobo.physical_to = 1.0;
    gobo.sets = vec![
        set("Open", 0, 127, (0.0, 1.0)),
        set("Stars", 128, 255, (0.0, 1.0)),
    ];
    let profile = profile(vec![channel("Gobo1", "Base", &[1], vec![gobo])]);
    let open = AttributeValue::Preset("Open".into());
    let stars = AttributeValue::Preset("Stars".into());
    assert_eq!(
        encode(&profile, &values(&[("Gobo1", stars.clone())])),
        [128]
    );
    assert_eq!(
        profile.interpolate_attribute("Gobo1", &open, &stars, 0.75),
        open
    );
    assert_eq!(
        profile.interpolate_attribute("Gobo1", &open, &stars, 1.0),
        stars
    );
    assert!(
        profile
            .validate_attributes(&values(&[("Gobo1", AttributeValue::Number(0.5))]).attributes)
            .is_err()
    );
}

#[test]
fn mode_master_is_resolved_before_dependent_channels_and_inactive_values_fail() {
    let dependent = channel(
        "Fog1",
        "Base",
        &[1],
        vec![mastered(function("Fog1", "Control"), 1, None, (128, 255))],
    );
    let profile = profile(vec![dependent, plain("Mode", "Base", 2)]);
    let active = values(&[
        ("Mode", AttributeValue::Number(100.0)),
        ("Fog1", AttributeValue::Number(50.0)),
    ]);
    assert_eq!(encode(&profile, &active), [128, 255]);
    assert!(
        profile
            .encode(
                &values(&[("Fog1", AttributeValue::Number(50.0))]),
                &mut [0; 2]
            )
            .is_err()
    );
}

#[test]
fn function_ranges_follow_numeric_order_at_every_resolution_without_changing_initial_function() {
    for bytes in 1..=4_u32 {
        let split = 1_u32 << (8 * bytes - 1);
        let offsets: Vec<u32> = (1..=bytes).collect();
        let profile = profile(vec![defaulted(
            channel(
                "Fog1",
                "Base",
                &offsets,
                vec![
                    starting(function("Zoom", "High"), split),
                    function("Fog1", "Low"),
                ],
            ),
            split,
        )]);
        let width = bytes as usize;
        let mut initial = vec![0; width];
        initial[0] = 128;
        assert_eq!(encode(&profile, &FixtureState::default()), initial);
        let mut expected = vec![255; width];
        expected[0] = 127;
        assert_eq!(
            encode(
                &profile,
                &values(&[("Fog1", AttributeValue::Number(100.0))])
            ),
            expected
        );
        assert_eq!(
            encode(
                &profile,
                &values(&[("Zoom", AttributeValue::Number(100.0))])
            ),
            vec![255; width]
        );
    }
}

#[test]
fn function_bounds_cross_logical_channels_on_the_same_physical_channel() {
    let profile = profile(vec![channel_with(
        &[
            ("Zoom", vec![starting(function("Zoom", "High"), 128)]),
            ("Fog1", vec![function("Fog1", "Low")]),
        ],
        "Base",
        &[1],
    )]);
    assert_eq!(
        encode(
            &profile,
            &values(&[("Fog1", AttributeValue::Number(100.0))])
        ),
        [127]
    );
    assert_eq!(
        encode(&profile, &values(&[("Zoom", AttributeValue::Number(0.0))])),
        [128]
    );
}

#[test]
fn repeated_function_starts_are_valid_in_mutually_exclusive_modes() {
    let dependent = channel(
        "Fog1",
        "Base",
        &[1],
        vec![
            mastered(function("Fog1", "LowMode"), 1, None, (0, 127)),
            mastered(function("Fog1", "HighMode"), 1, None, (128, 255)),
            mastered(starting(function("Zoom", "Zoom"), 128), 1, None, (128, 255)),
        ],
    );
    let profile = profile(vec![dependent, plain("Mode", "Base", 2)]);
    for (mode, expected) in [(0.0, [128, 0]), (100.0, [64, 255])] {
        assert_eq!(
            encode(
                &profile,
                &values(&[
                    ("Fog1", AttributeValue::Number(50.0)),
                    ("Mode", AttributeValue::Number(mode))
                ])
            ),
            expected
        );
    }
}

#[test]
fn unconditional_function_range_expands_when_the_next_function_is_inactive() {
    let dependent = channel(
        "Fog1",
        "Base",
        &[1],
        vec![
            function("Fog1", "Fog"),
            mastered(starting(function("Zoom", "Zoom"), 128), 1, None, (128, 255)),
        ],
    );
    let profile = profile(vec![dependent, plain("Mode", "Base", 2)]);
    for (mode, expected) in [(0.0, [128, 0]), (100.0, [64, 255])] {
        assert_eq!(
            encode(
                &profile,
                &values(&[
                    ("Fog1", AttributeValue::Number(50.0)),
                    ("Mode", AttributeValue::Number(mode))
                ])
            ),
            expected
        );
    }
}

#[test]
fn a_function_mode_master_requires_its_own_dmx_range_to_be_active() {
    let dependent = channel(
        "Fog1",
        "Base",
        &[1],
        vec![mastered(function("Fog1", "Control"), 1, Some(1), (0, 255))],
    );
    let master = channel(
        "Mode",
        "Base",
        &[2],
        vec![
            function("Mode", "Idle"),
            starting(function("Mode", "Enabled"), 128),
        ],
    );
    let profile = profile(vec![dependent, master]);
    let fog = ("Fog1", AttributeValue::Number(50.0));
    assert!(
        profile
            .encode(
                &values(&[
                    fog.clone(),
                    ("Base_Mode.Mode.Idle", AttributeValue::Number(100.0))
                ]),
                &mut [0; 2]
            )
            .is_err()
    );
    assert_eq!(
        encode(
            &profile,
            &values(&[fog, ("Base_Mode.Mode.Enabled", AttributeValue::Number(0.0))])
        ),
        [128, 128]
    );
}

#[test]
fn a_function_mode_master_requires_its_own_mode_condition_to_be_active() {
    let dependent = channel(
        "Fog1",
        "Base",
        &[1],
        vec![mastered(function("Fog1", "Control"), 1, Some(1), (0, 255))],
    );
    let unit = |name| {
        let mut function = function("Mode", name);
        function.physical_to = 1.0;
        function
    };
    let master = channel(
        "Mode",
        "Base",
        &[2],
        vec![
            mastered(unit("Idle"), 2, None, (0, 127)),
            mastered(unit("Enabled"), 2, None, (128, 255)),
        ],
    );
    let profile = profile(vec![dependent, master, plain("Mode", "Head1", 3)]);
    let fog = ("Fog1", AttributeValue::Number(50.0));
    assert!(
        profile
            .encode(&values(std::slice::from_ref(&fog)), &mut [0; 3])
            .is_err()
    );
    assert_eq!(
        encode(
            &profile,
            &values(&[fog, ("Head1.Mode", AttributeValue::Number(100.0))])
        ),
        [128, 0, 255]
    );
}

#[test]
fn channel_sets_cannot_escape_a_mode_dependent_function_range() {
    let mut fog = function("Fog1", "Fog");
    fog.sets = vec![
        set("Low", 0, 199, (0.0, 49.0)),
        set("Boost", 200, 255, (50.0, 100.0)),
    ];
    let mut zoom = mastered(starting(function("Zoom", "Zoom"), 128), 1, None, (128, 255));
    zoom.physical_to = 1.0;
    let profile = profile(vec![
        channel("Fog1", "Base", &[1], vec![fog, zoom]),
        plain("Mode", "Base", 2),
    ]);
    assert_eq!(
        encode(
            &profile,
            &values(&[("Fog1", AttributeValue::Preset("Boost".into()))])
        ),
        [200, 0]
    );
    assert!(
        profile
            .encode(
                &values(&[
                    ("Fog1", AttributeValue::Preset("Boost".into())),
                    ("Mode", AttributeValue::Number(100.0))
                ]),
                &mut [0; 2]
            )
            .is_err()
    );
    assert_eq!(
        encode(
            &profile,
            &values(&[
                ("Fog1", AttributeValue::Number(49.0)),
                ("Mode", AttributeValue::Number(100.0))
            ])
        ),
        [127, 255]
    );
}

#[test]
fn malformed_compiled_modes_and_cycles_fail_at_initialization() {
    let normal = plain("Fog1", "Base", 1);
    let with_offsets = |offsets: &[u32]| {
        let mut channel = normal.clone();
        channel.offsets = offsets.to_vec();
        channel
    };
    let mut wide_default = normal.clone();
    wide_default.default = 256;
    let mut empty = normal.clone();
    empty.functions.clear();
    let mut overlapping_sets = function("Gobo1", "Gobo");
    overlapping_sets.sets = vec![set("A", 0, 200, (0.0, 1.0)), set("B", 128, 255, (0.0, 1.0))];
    for invalid in [
        vec![with_offsets(&[0])],
        vec![with_offsets(&[1, 1])],
        vec![with_offsets(&[513])],
        vec![with_offsets(&[1, 2, 3, 4, 5])],
        vec![with_offsets(&[])],
        vec![wide_default],
        vec![empty],
        vec![channel("Gobo1", "Base", &[1], vec![overlapping_sets])],
        vec![channel(
            "Fog1",
            "Base",
            &[1],
            vec![mastered(function("Fog1", "Control"), 0, None, (0, 255))],
        )],
        vec![channel(
            "Fog1",
            "Base",
            &[1],
            vec![mastered(function("Fog1", "Control"), 7, None, (0, 255))],
        )],
    ] {
        assert!(
            GdtfProfile::from_wire(&mode(invalid.clone())).is_err(),
            "{invalid:?}"
        );
    }
    assert!(GdtfProfile::from_wire(&mode(Vec::new())).is_err());
}

fn rig(channels: Vec<wire::DmxChannel>) -> FixtureRig {
    rig_with(
        mode(channels),
        wire::Fixture {
            area: wire::StageArea::Centre as i32,
            fixture_uuid: "fixture".into(),
            name: "fixture".into(),
            ..Default::default()
        },
    )
}

fn engine(channels: Vec<wire::DmxChannel>, source: &str) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "test".into(),
        bank: "test".into(),
        lua_source: source.into(),
    }])
    .unwrap();
    let mut engine = Engine::new(rig(channels), catalog, support::palettes()).unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "test".into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
    engine
}

#[test]
fn lua_controls_non_lighting_attributes_and_blackout_restores_defaults() {
    let mut engine = engine(
        vec![plain("Fog1", "Base", 1)],
        "return {render=function(ctx) return sf.scene {sf.physical_attribute(sf.all(), 'Fog1', 50)} end}",
    );
    assert_eq!(
        engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
        128
    );
    engine
        .apply(0.1, &Command::SetBlackout { enabled: true })
        .unwrap();
    assert_eq!(
        engine.render_at(0.1).unwrap().dmx_frames_by_universe[&0][0],
        0
    );
}

#[test]
fn explicit_dimmer_values_respect_master_and_blackout() {
    let mut engine = engine(
        vec![plain("Dimmer", "Base", 1)],
        "return {render=function(ctx) return sf.scene {sf.physical_attribute(sf.all(), 'Dimmer', 100)} end}",
    );
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity: 0.5,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
    assert_eq!(
        engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
        64
    );
    engine
        .apply(0.1, &Command::SetBlackout { enabled: true })
        .unwrap();
    assert_eq!(
        engine.render_at(0.1).unwrap().dmx_frames_by_universe[&0][0],
        0
    );
}

#[test]
fn white_only_emitters_follow_intensity_without_applying_colour_brightness_twice() {
    for with_dimmer in [false, true] {
        let mut channels = vec![defaulted(plain("ColorAdd_W", "Base", 1), 255)];
        if with_dimmer {
            channels.push(plain("Dimmer", "Base", 2));
        }
        let profile = profile(channels);
        for (intensity, expected) in [(0.0, 0), (0.5, 128), (1.0, 255)] {
            let output = encode(
                &profile,
                &FixtureState {
                    intensity,
                    ..Default::default()
                },
            );
            assert_eq!(
                output,
                if with_dimmer {
                    vec![255, expected]
                } else {
                    vec![expected]
                }
            );
        }
        let half_white = FixtureState {
            intensity: 0.5,
            colour: Rgb {
                red: 0.5,
                green: 0.5,
                blue: 0.5,
            },
            ..Default::default()
        };
        assert_eq!(
            encode(&profile, &half_white),
            if with_dimmer {
                vec![128, 128]
            } else {
                vec![64]
            }
        );
    }
}

#[test]
fn extra_emitters_ignore_lit_defaults_and_obey_master_blackout_idle_and_faults() {
    for (attribute, default, dark, dimmed) in
        [("ColorAdd_A", 255, 0, 32), ("ColorSub_UV", 0, 255, 223)]
    {
        let channels = vec![defaulted(plain(attribute, "Base", 1), default)];
        assert_eq!(
            encode(&profile(channels.clone()), &FixtureState::default()),
            [dark]
        );
        let mut engine = engine(
            channels,
            &format!(
                "return {{render=function(ctx) if ctx.time_seconds >= 1 then error('fixture fault') end; return {{sf.physical_attribute(sf.all(), '{attribute}', 50)}} end}}"
            ),
        );
        assert_eq!(
            engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
            128
        );
        for (master, expected) in [(0.5, dimmed), (0.0, dark), (1.0, 128)] {
            engine
                .apply(
                    0.0,
                    &Command::SetMaster {
                        intensity: master,
                        fade_seconds: 0.0,
                    },
                )
                .unwrap();
            assert_eq!(
                engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
                expected
            );
        }
        engine
            .apply(0.1, &Command::SetBlackout { enabled: true })
            .unwrap();
        assert_eq!(
            engine.render_at(0.1).unwrap().dmx_frames_by_universe[&0][0],
            dark
        );
        engine
            .apply(0.2, &Command::SetBlackout { enabled: false })
            .unwrap();
        assert_eq!(
            engine.render_at(0.2).unwrap().dmx_frames_by_universe[&0][0],
            128
        );
        engine.apply(0.3, &Command::Stop).unwrap();
        assert_eq!(
            engine.render_at(0.3).unwrap().dmx_frames_by_universe[&0][0],
            dark
        );
        engine
            .apply(
                0.4,
                &Command::Select {
                    bank: "test".into(),
                    autoloop: "test".into(),
                    transition: Transition::Cut,
                    palette: "techno".into(),
                },
            )
            .unwrap();
        assert_eq!(
            engine.render_at(0.4).unwrap().dmx_frames_by_universe[&0][0],
            128
        );
        let failed = engine.render_at(1.0).unwrap();
        assert!(failed.script_error.unwrap().contains("fixture fault"));
        assert_eq!(failed.dmx_frames_by_universe[&0][0], dark);
    }
}

#[test]
fn encoding_faults_latch_darkness_keep_safe_movement_and_allow_a_healthy_autoloop() {
    for (conditional, extra_effect, diagnostic) in [
        (
            true,
            "sf.physical_attribute(sf.all(), 'Zoom', 50)",
            "inactive",
        ),
        (
            false,
            "sf.physical_attribute(sf.all(), 'Zoom', 50), sf.physical_attribute(sf.all(), 'Base.Zoom', 75)",
            "conflicting",
        ),
    ] {
        let zoom = if conditional {
            mastered(function("Zoom", "Control"), 2, None, (128, 255))
        } else {
            function("Zoom", "Control")
        };
        let channels = vec![
            plain("Dimmer", "Base", 1),
            plain("Pan", "Base", 2),
            plain("Mode", "Base", 3),
            channel("Zoom", "Base", &[4], vec![zoom]),
        ];
        let broken = format!(
            "return {{render=function(ctx) if ctx.time_seconds == 0 then return {{sf.dimmer(sf.all(), 0.5), sf.pan_tilt(sf.all(), 0.25, 0.5)}} end; return {{sf.dimmer(sf.all(), 1), sf.pan_tilt(sf.all(), 0.9, 0.5), {extra_effect}}} end}}"
        );
        let catalog = AutoloopCatalog::from_definitions(&[
            wire::AutoloopDefinition { bank: "test".into(), autoloop_id: "broken".into(), lua_source: broken },
            wire::AutoloopDefinition { bank: "test".into(), autoloop_id: "healthy".into(), lua_source: "return {render=function(ctx) return {sf.dimmer(sf.all(), 0.75), sf.pan_tilt(sf.all(), 0.75, 0.5)} end}".into() },
        ]).unwrap();
        let mut engine = Engine::new(rig(channels), catalog, support::palettes()).unwrap();
        engine
            .play_autoloop(0.0, "broken", Transition::Cut, "techno")
            .unwrap();
        assert_eq!(
            &engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][..2],
            &[128, 64]
        );
        let failed = engine.render_at(0.1).unwrap();
        assert!(failed.script_error.as_ref().unwrap().contains(diagnostic));
        assert_eq!(&failed.dmx_frames_by_universe[&0][..2], &[0, 64]);
        let latched = engine.render_at(0.2).unwrap();
        assert_eq!(latched.script_error, failed.script_error);
        assert_eq!(
            latched.dmx_frames_by_universe,
            failed.dmx_frames_by_universe
        );
        engine
            .play_autoloop(0.3, "healthy", Transition::Cut, "techno")
            .unwrap();
        let recovered = engine.render_at(0.3).unwrap();
        assert!(recovered.script_error.is_none());
        assert_eq!(&recovered.dmx_frames_by_universe[&0][..2], &[191, 191]);
    }
}

#[test]
fn unsupported_brightness_ranges_are_rejected_before_they_can_bypass_blackout() {
    let mut full = function("Dimmer", "Dimmer");
    full.sets = vec![set("Full", 0, 255, (0.0, 100.0))];
    let unsafe_dimmer = vec![defaulted(channel("Dimmer", "Base", &[1], vec![full]), 255)];
    let conditional_dimmer = vec![
        channel(
            "Dimmer",
            "Base",
            &[1],
            vec![mastered(function("Dimmer", "Control"), 1, None, (128, 255))],
        ),
        plain("Mode", "Base", 2),
    ];
    for channels in [unsafe_dimmer, conditional_dimmer] {
        let error = GdtfProfile::from_wire(&mode(channels)).unwrap_err();
        assert!(
            error.to_string().contains("brightness attribute"),
            "{error:#}"
        );
    }
}

#[test]
fn unrelated_cells_keep_independent_brightness_and_blackout() {
    let mut channels = vec![plain("Dimmer", "Head1", 1)];
    for (geometry, offset) in [("Head1", 2), ("Head2", 5)] {
        for (index, attribute) in ["ColorAdd_R", "ColorAdd_G", "ColorAdd_B"]
            .iter()
            .enumerate()
        {
            channels.push(plain(attribute, geometry, offset + index as u32));
        }
    }
    let profile = profile(channels);
    let state = FixtureState {
        intensity: 0.5,
        ..Default::default()
    };
    assert_eq!(
        encode(&profile, &state),
        [128, 255, 255, 255, 128, 128, 128]
    );
    assert_eq!(
        encode(&profile, &FixtureState::default()),
        [0, 255, 255, 255, 0, 0, 0]
    );
}
