//! Fixture vocabulary: the software strobe gate, wheel colour blends, gobo slots and darkness.

mod support;

use std::fs;
use std::path::PathBuf;

use prost::Message;

use serde_json::{Value, json};
use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Rgb, Transition};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

// A binary-exact frame time keeps the accumulated gate phase exact, so counts are exact too.
const FRAMES_PER_SECOND: usize = 32;
const FRAME_SECONDS: f64 = 1.0 / FRAMES_PER_SECOND as f64;

fn engine(rig: FixtureRig, scripts: &[(&str, &str)]) -> Engine {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    for (name, source) in scripts {
        fs::write(
            directory.path().join("test").join(format!("{name}.lua")),
            source,
        )
        .unwrap();
    }
    let catalog = AutoloopCatalog::load(directory.path()).unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: 0,
                bar_phase: None,
                beats_per_bar: 4,
                bpm: 120.0,
            },
        )
        .unwrap();
    engine
}

fn select(engine: &mut Engine, at: f64, name: &str, transition: Transition) {
    engine
        .apply(
            at,
            &Command::Select {
                bank: "test".into(),
                autoloop: name.into(),
                transition,
                palette: "techno".into(),
            },
        )
        .unwrap();
}

fn rig(spec: Value) -> FixtureRig {
    support::rig(spec)
}

// 1 -> 10 Hz over 16 beats (8 s at 120 bpm), then held at 10 Hz.
const STROBE_SWEEP: &str = r#"return {render=function(ctx)
    local rate = 1 + 9 * math.min(1, ctx.autoloop_elapsed_beats / 16)
    return sf.scene {sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(1, 1, 1)),
        sf.strobe(sf.all(), rate)} end}"#;

/// Gate openings (dark to lit) in each second-long window of frames.
fn openings_per_second(engine: &mut Engine, seconds: usize) -> Vec<usize> {
    let lit: Vec<bool> = (0..seconds * FRAMES_PER_SECOND)
        .map(|frame| {
            let rendered = engine.render_at(frame as f64 * FRAME_SECONDS).unwrap();
            rendered.dmx_frames_by_universe[&0][0] > 0
        })
        .collect();
    let openings: Vec<bool> = lit.windows(2).map(|pair| !pair[0] && pair[1]).collect();
    openings
        .windows(FRAMES_PER_SECOND)
        .map(|window| window.iter().filter(|&&opened| opened).count())
        .collect()
}

#[test]
fn a_strobe_rate_sweep_never_gates_faster_than_the_rate_cap() {
    for cap in [10.0, 4.0] {
        let mut engine = engine(
            rig(json!({"fixtures": [
                {"fixture_uuid": "dimmer", "profile": "generic_dimmer", "universe": 0, "address": 1}]})),
            &[("sweep", STROBE_SWEEP)],
        );
        engine
            .apply(0.0, &Command::SetStrobeMaxRateHz { hz: cap })
            .unwrap();
        select(&mut engine, 0.0, "sweep", Transition::Cut);
        let counts = openings_per_second(&mut engine, 12);
        let busiest = counts.iter().copied().max().unwrap();
        // The held rate fills a whole second with exactly cap cycles.
        assert_eq!(busiest, cap as usize, "{counts:?}");
        assert_eq!(*counts.last().unwrap(), cap as usize, "{counts:?}");
    }
}

#[test]
fn a_strobe_burst_stops_gating_at_the_burst_cap() {
    let mut engine = engine(
        rig(json!({"fixtures": [
            {"fixture_uuid": "dimmer", "profile": "generic_dimmer", "universe": 0, "address": 1}]})),
        &[("sweep", STROBE_SWEEP)],
    );
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 2.0 })
        .unwrap();
    select(&mut engine, 0.0, "sweep", Transition::Cut);
    let counts = openings_per_second(&mut engine, 4);
    assert!(counts[0] > 0, "{counts:?}");
    assert!(
        counts[2 * FRAMES_PER_SECOND..]
            .iter()
            .all(|&count| count == 0),
        "{counts:?}"
    );
}

/// A spec's rig with synthetic compiled test profiles added under the names its fixtures use.
fn rig_with(spec: Value, profiles: &[(&str, wire::FixtureProfile)]) -> FixtureRig {
    let mut definition = support::definition(&spec);
    for (name, profile) in profiles {
        definition.profiles.push(wire::FixtureProfile {
            profile_id: (*name).into(),
            ..profile.clone()
        });
    }
    FixtureRig::from_wire(&definition).unwrap()
}

fn solid(colour: &str) -> String {
    format!(
        "return {{render=function(ctx) return sf.scene {{sf.dimmer(sf.all(), 1), \
         sf.colour(sf.all(), sf.rgb({colour}))}} end}}"
    )
}

#[test]
fn a_wheel_only_cell_changes_slot_half_way_through_a_fade_while_mixing_cells_blend() {
    // wheel_spinner's Color1 slots: Red from 64, Blue from 192.
    let (red, blue) = (("red", 64, 1.0), ("blue", 192, 0.0));
    for (from, to) in [(red, blue), (blue, red)] {
        let rig = rig_with(
            json!({"fixtures": [
                {"fixture_uuid": "wheel", "profile": "wheel", "universe": 0, "address": 1},
                {"fixture_uuid": "mixer", "profile": "generic_rgb", "universe": 0, "address": 4}]}),
            &[(
                "wheel",
                support::compiled_profile("wheel_spinner#3ch", "wheel"),
            )],
        );
        let (red_loop, blue_loop) = (solid("1, 0, 0"), solid("0, 0, 1"));
        let mut engine = engine(rig, &[("red", &red_loop), ("blue", &blue_loop)]);
        select(&mut engine, 0.0, from.0, Transition::Cut);
        engine.render_at(0.0).unwrap();
        // 120 bpm: a 4-beat crossfade from 1 s reaches half-way at 2 s.
        select(
            &mut engine,
            1.0,
            to.0,
            Transition::Crossfade {
                duration_beats: 4.0,
            },
        );
        for (seconds, amount, (_, wheel_dmx, wheel_red)) in [
            (1.5, 0.25, from),
            (1.99, 0.495, from),
            (2.0, 0.5, to),
            (2.5, 0.75, to),
        ] {
            let frame = engine.render_at(seconds).unwrap();
            let states = &frame.fixture_properties_before_strobe;
            let context = format!("{} to {} at {seconds}", from.0, to.0);
            assert_eq!(frame.dmx_frames_by_universe[&0][0], wheel_dmx, "{context}");
            assert_eq!(states["wheel"].colour.red, wheel_red, "{context}");
            let mixer_red = from.2 + (to.2 - from.2) * amount;
            assert!(
                (states["mixer"].colour.red - mixer_red).abs() < 1e-9,
                "{context}: {}",
                states["mixer"].colour.red
            );
        }
    }
}

/// A one-channel gobo wheel whose Gobo1 ChannelSets are `names`, evenly spaced in DMX order.
fn gobo_wheel(names: &[&str]) -> wire::FixtureProfile {
    let step = 256 / names.len() as u32;
    let sets = names
        .iter()
        .enumerate()
        .map(|(index, name)| wire::ChannelSet {
            name: (*name).into(),
            dmx_from: index as u32 * step,
            dmx_to: if index + 1 == names.len() {
                255
            } else {
                (index as u32 + 1) * step - 1
            },
            physical_from: index as f64,
            physical_to: index as f64,
            wheel_slot_colour: None,
        })
        .collect();
    wire::FixtureProfile {
        profile_id: String::new(),
        dmx_mode: "Gobo".into(),
        mode: Some(wire::DmxMode {
            channels: vec![wire::DmxChannel {
                geometry_path: vec!["Base".into()],
                offsets: vec![1],
                default: 0,
                functions: vec![wire::ChannelFunction {
                    path: "Base_Gobo1.Gobo1.Gobo1".into(),
                    attribute: "Gobo1".into(),
                    qualified_attribute: "Base.Gobo1".into(),
                    physical_from: 0.0,
                    physical_to: (names.len() - 1) as f64,
                    snap: true,
                    sets,
                    ..Default::default()
                }],
            }],
        }),
    }
}

/// beam_scanner's Gobo1 at DMX 6 (Open, Dots, Lines, Tunnel), an `oPEN` set in second place at
/// 7, a wheel without an open set at 8, and an RGB fixture without a gobo wheel at 9..11.
fn gobo_rig() -> FixtureRig {
    rig_with(
        json!({"fixtures": [
            {"fixture_uuid": "scanner", "profile": "scanner", "universe": 0, "address": 1},
            {"fixture_uuid": "late_open", "profile": "late_open", "universe": 0, "address": 7},
            {"fixture_uuid": "no_open", "profile": "no_open", "universe": 0, "address": 8},
            {"fixture_uuid": "plain", "profile": "generic_rgb", "universe": 0, "address": 9}]}),
        &[
            (
                "scanner",
                support::compiled_profile("beam_scanner#6ch", "scanner"),
            ),
            ("late_open", gobo_wheel(&["Dots", "oPEN", "Lines"])),
            ("no_open", gobo_wheel(&["Dots", "Lines", "Tunnel"])),
        ],
    )
}

#[test]
fn gobo_slot_zero_is_the_open_set_and_later_slots_wrap_over_the_others_in_dmx_order() {
    let rig = gobo_rig();
    let name = |cell: &str, slot: u64| rig.profile(cell).unwrap().gobo_name(slot);
    for (slot, scanner, late_open, no_open) in [
        (0, "Open", "oPEN", "Dots"),
        (1, "Dots", "Dots", "Lines"),
        (2, "Lines", "Lines", "Tunnel"),
        (3, "Tunnel", "Dots", "Lines"),
        (4, "Dots", "Lines", "Tunnel"),
        (7, "Dots", "Dots", "Lines"),
    ] {
        assert_eq!(name("scanner", slot).as_deref(), Some(scanner), "{slot}");
        assert_eq!(
            name("late_open", slot).as_deref(),
            Some(late_open),
            "{slot}"
        );
        assert_eq!(name("no_open", slot).as_deref(), Some(no_open), "{slot}");
        assert_eq!(name("plain", slot), None);
    }
}

#[test]
fn sf_gobo_sends_a_validated_preset_to_every_member_with_a_gobo_wheel() {
    let slot = |slot: i64| {
        format!(
            "return {{render=function(ctx) return sf.scene {{sf.dimmer(sf.all(), 1), \
             sf.gobo(sf.all(), {slot})}} end}}"
        )
    };
    let (two, four, negative) = (slot(2), slot(4), slot(-1));
    let mut engine = engine(
        gobo_rig(),
        &[("two", &two), ("four", &four), ("negative", &negative)],
    );
    for (name, scanner, late_open, no_open) in [("two", 128, 170, 170), ("four", 64, 170, 170)] {
        select(&mut engine, 0.0, name, Transition::Cut);
        let frame = engine.render_at(0.0).unwrap();
        assert_eq!(frame.script_error, None);
        assert_eq!(
            &frame.dmx_frames_by_universe[&0][5..8],
            &[scanner, late_open, no_open],
            "{name}"
        );
        // The RGB fixture without a gobo wheel takes the dimmer only.
        assert!(
            frame.fixture_properties_before_strobe["plain"]
                .attributes
                .is_empty()
        );
    }
    select(&mut engine, 0.0, "negative", Transition::Cut);
    let failed = engine.render_at(0.0).unwrap();
    assert!(
        failed
            .script_error
            .as_deref()
            .is_some_and(|error| error.contains("gobo slot must be 0 or more")),
        "{:?}",
        failed.script_error
    );
}

/// Every compiled mode the generic test data carries, plus wheel_spinner without its black slot,
/// whose gate shutter is then its only way to go dark.
fn test_data_profiles() -> Vec<wire::FixtureProfile> {
    let read = |path: PathBuf| {
        wire::RigDefinition::decode(fs::read(path).unwrap().as_slice())
            .unwrap()
            .profiles
    };
    let mut profiles =
        read(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/data/compiled_profiles.pb"));
    profiles.extend(read(support::sample_rig().join("generic.rig.pb")));
    let mut gated = support::compiled_profile("wheel_spinner#3ch", "wheel_spinner_gated");
    for channel in &mut gated.mode.as_mut().unwrap().channels {
        for function in &mut channel.functions {
            function.sets.retain(|set| {
                set.wheel_slot_colour
                    .as_ref()
                    .is_none_or(|colour| colour.luminance > 0.0)
            });
        }
    }
    profiles.push(gated);
    profiles
}

fn channel_value(frame: &[u8], channel: &wire::DmxChannel) -> u32 {
    channel.offsets.iter().fold(0, |value, &offset| {
        value << 8 | u32::from(frame[offset as usize - 1])
    })
}

fn has(channel: &wire::DmxChannel, test: impl Fn(&wire::ChannelFunction) -> bool) -> bool {
    channel.functions.iter().any(test)
}

fn is_colour_wheel(function: &wire::ChannelFunction) -> bool {
    function.attribute.starts_with("Color")
        && function
            .sets
            .iter()
            .any(|set| set.wheel_slot_colour.is_some())
}

fn is_black_slot(set: &wire::ChannelSet) -> bool {
    set.wheel_slot_colour
        .as_ref()
        .is_some_and(|colour| colour.luminance == 0.0)
}

/// A shutter function with no ChannelSets from DMX 0: 0 closes it.
fn is_gate(function: &wire::ChannelFunction) -> bool {
    function
        .attribute
        .strip_prefix("Shutter")
        .is_some_and(|index| !index.is_empty() && index.bytes().all(|byte| byte.is_ascii_digit()))
        && function.sets.is_empty()
        && function.dmx_from == 0
}

/// How a mode can go dark, read from its functions.
#[derive(Debug, PartialEq)]
enum DarkPath {
    Dimmer,
    Emitters,
    BlackSlot,
    Gate,
}

fn dark_path(mode: &wire::DmxMode) -> Option<DarkPath> {
    let any = |test: &dyn Fn(&wire::ChannelFunction) -> bool| {
        mode.channels.iter().any(|channel| has(channel, test))
    };
    if any(&|function| function.attribute == "Dimmer") {
        Some(DarkPath::Dimmer)
    } else if any(&|function| function.attribute.starts_with("ColorAdd_")) {
        Some(DarkPath::Emitters)
    } else if any(&|function| is_colour_wheel(function) && function.sets.iter().any(is_black_slot))
    {
        Some(DarkPath::BlackSlot)
    } else if any(&is_gate) {
        Some(DarkPath::Gate)
    } else {
        None
    }
}

/// Whether a frame can emit light: dimmers and emitters must not all be at zero, a wheel must
/// not sit on a black slot and, where a gate shutter is the only dark path, it must not be
/// closed. Elsewhere a shutter's DMX 0 can mean open, so it is not read.
fn emits_light(mode: &wire::DmxMode, path: &DarkPath, frame: &[u8]) -> bool {
    let values = |test: fn(&wire::ChannelFunction) -> bool| -> Vec<u32> {
        mode.channels
            .iter()
            .filter(|channel| has(channel, test))
            .map(|channel| channel_value(frame, channel))
            .collect()
    };
    let dimmers = values(|function| function.attribute == "Dimmer");
    let emitters = values(|function| function.attribute.starts_with("ColorAdd_"));
    let wheel_lit = mode
        .channels
        .iter()
        .filter(|channel| has(channel, is_colour_wheel))
        .all(|channel| {
            let value = channel_value(frame, channel);
            !channel
                .functions
                .iter()
                .flat_map(|function| &function.sets)
                .any(|set| (set.dmx_from..=set.dmx_to).contains(&value) && is_black_slot(set))
        });
    (dimmers.is_empty() || dimmers.iter().any(|&value| value > 0))
        && (emitters.is_empty() || emitters.iter().any(|&value| value > 0))
        && wheel_lit
        && (*path != DarkPath::Gate || values(is_gate).iter().all(|&value| value > 0))
}

#[test]
fn intensity_zero_is_dark_on_every_test_data_profile() {
    let mut paths = Vec::new();
    for profile in test_data_profiles() {
        let mode = profile.mode.clone().unwrap();
        let path = dark_path(&mode)
            .unwrap_or_else(|| panic!("{} has no way to go dark", profile.profile_id));
        let rig = FixtureRig::from_wire(&wire::RigDefinition {
            fixtures: vec![wire::Fixture {
                fixture_uuid: "fixture".into(),
                name: "fixture".into(),
                profile_id: profile.profile_id.clone(),
                universe: 0,
                address: 1,
                area: wire::StageArea::Centre as i32,
                ..Default::default()
            }],
            profiles: vec![profile.clone()],
            ..Default::default()
        })
        .unwrap_or_else(|error| panic!("{}: {error:#}", profile.profile_id));
        for (intensity, lit) in [(1.0, true), (0.0, false)] {
            let mut states = rig.empty_states();
            for state in states.values_mut() {
                state.intensity = intensity;
                state.colour = Rgb {
                    red: 1.0,
                    green: 1.0,
                    blue: 1.0,
                };
            }
            let frame = &rig.encode(&states).unwrap()[&0];
            assert_eq!(
                emits_light(&mode, &path, frame),
                lit,
                "{} ({path:?}) at intensity {intensity}: {:?}",
                profile.profile_id,
                &frame[..16]
            );
        }
        paths.push(path);
    }
    for path in [
        DarkPath::Dimmer,
        DarkPath::Emitters,
        DarkPath::BlackSlot,
        DarkPath::Gate,
    ] {
        assert!(
            paths.contains(&path),
            "no test profile goes dark by {path:?}"
        );
    }
}

/// Requested colour -> wheel_spinner#3ch Color1 DMX as the encode picks the slot, ties and
/// near-ties included. autolux/tests/test_palette_variants.py holds Python's copy of the rule
/// (palettes.nearest_slot) to this same table.
const WHEEL_SPINNER_PICKS: [([f64; 3], u8); 9] = [
    ([1.0, 0.0, 0.0], 64),
    ([1.0, 0.5, 0.0], 64),
    ([1.0, 1.0, 0.0], 128),
    ([0.0, 1.0, 1.0], 192),
    ([0.5, 0.0, 1.0], 192),
    ([1.0, 0.0, 1.0], 192),
    ([1.0, 1.0, 1.0], 128),
    ([0.2, 0.2, 0.2], 128),
    ([0.0, 0.0, 0.0], 0),
];

#[test]
fn a_wheel_only_cell_takes_the_nearest_slot_python_predicts() {
    let rig = rig_with(
        json!({"fixtures": [
            {"fixture_uuid": "wheel", "profile": "wheel", "universe": 0, "address": 1}]}),
        &[(
            "wheel",
            support::compiled_profile("wheel_spinner#3ch", "wheel"),
        )],
    );
    for ([red, green, blue], dmx) in WHEEL_SPINNER_PICKS {
        let mut states = rig.empty_states();
        let state = states.get_mut("wheel").unwrap();
        state.intensity = 1.0;
        state.colour = Rgb { red, green, blue };
        assert_eq!(
            rig.encode(&states).unwrap()[&0][0],
            dmx,
            "{red}, {green}, {blue}"
        );
    }
}
