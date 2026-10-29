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

// The live render rate.
const FRAMES_PER_SECOND: usize = 40;

fn frame_time(frame: usize) -> f64 {
    frame as f64 / FRAMES_PER_SECOND as f64
}

fn engine(rig: FixtureRig, scripts: &[(&str, &str)]) -> Engine {
    engine_at(rig, scripts, 120.0)
}

fn beat(engine: &mut Engine, at: f64, beat_index: u64, bar_phase: Option<u32>, bpm: f64) {
    engine
        .apply(
            at,
            &Command::Beat {
                beat_index,
                bar_phase,
                beats_per_bar: 4,
                bpm,
                features: None,
            },
        )
        .unwrap();
}

/// An engine with one beat at 0 s, so its grid starts at 0 at `bpm`.
fn engine_at(rig: FixtureRig, scripts: &[(&str, &str)], bpm: f64) -> Engine {
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
    beat(&mut engine, 0.0, 0, None, bpm);
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

fn dimmers(count: usize) -> FixtureRig {
    let fixtures: Vec<Value> = (0..count)
        .map(|index| {
            json!({"fixture_uuid": format!("dimmer{index}"), "profile": "generic_dimmer",
                   "universe": 0, "address": index + 1})
        })
        .collect();
    rig(json!({ "fixtures": fixtures }))
}

/// Whether each dimmer is lit on each frame of `frames`.
fn lit(engine: &mut Engine, frames: std::ops::Range<usize>, cells: usize) -> Vec<Vec<bool>> {
    frames
        .map(|frame| {
            let rendered = engine.render_at(frame_time(frame)).unwrap();
            rendered.dmx_frames_by_universe[&0][..cells]
                .iter()
                .map(|&level| level > 0)
                .collect()
        })
        .collect()
}

/// Gate openings (dark to lit) in each second-long window of frames.
fn openings_per_second(engine: &mut Engine, seconds: usize) -> Vec<usize> {
    let lit: Vec<bool> = lit(engine, 0..seconds * FRAMES_PER_SECOND, 1)
        .into_iter()
        .map(|cells| cells[0])
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
        let mut engine = engine(dimmers(1), &[("sweep", STROBE_SWEEP)]);
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
fn every_flash_at_a_rate_dividing_the_frame_rate_lasts_two_frames() {
    const START_FRAME: usize = 12_345;
    for hz in [1, 2, 4, 8, 10] {
        let script = format!(
            "return {{render=function(ctx) return sf.scene {{sf.dimmer(sf.all(), 1), \
             sf.strobe(sf.all(), {hz})}} end}}"
        );
        let mut engine = engine(dimmers(1), &[("strobe", &script)]);
        select(
            &mut engine,
            frame_time(START_FRAME),
            "strobe",
            Transition::Cut,
        );
        let frames = lit(
            &mut engine,
            START_FRAME..START_FRAME + 4 * FRAMES_PER_SECOND,
            1,
        );
        // 50 ms on, the rest of the cycle off, cycles on the beat grid from 0 s.
        for (frame, cells) in (START_FRAME..).zip(&frames) {
            assert_eq!(
                cells[0],
                (frame * hz) % FRAMES_PER_SECOND < 2 * hz,
                "{hz} Hz, {frame}"
            );
        }
    }
}

#[test]
fn flashes_at_beat_divisions_start_on_the_beat_grid_and_follow_a_bar_phase_correction() {
    // 60 bpm, so a beat is 40 frames. Beats arrive on time until index 5 comes 0.3 s late as bar
    // phase 2, moving the grid on by 1.3 beats; its next bar line is grid beat 8, at 7.3 s.
    let beats = [
        (40, 1, None),
        (80, 2, None),
        (120, 3, None),
        (160, 4, None),
        (212, 5, Some(2)),
        (252, 6, None),
        (292, 7, None),
        (332, 8, None),
        (372, 9, None),
    ];
    for per_beat in [4, 8] {
        let script = format!(
            "return {{render=function(ctx) return sf.scene {{sf.dimmer(sf.all(), 1), \
             sf.strobe(sf.all(), {per_beat} * ctx.bpm / 60)}} end}}"
        );
        let mut engine = engine_at(dimmers(1), &[("grid", &script)], 60.0);
        select(&mut engine, 0.0, "grid", Transition::Cut);
        let cycle = FRAMES_PER_SECOND / per_beat;
        for frame in 0..400 {
            if let Some(&(_, index, bar_phase)) = beats.iter().find(|(at, ..)| *at == frame) {
                beat(&mut engine, frame_time(frame), index, bar_phase, 60.0);
            }
            let rendered = engine.render_at(frame_time(frame)).unwrap();
            let lit = rendered.dmx_frames_by_universe[&0][0] > 0;
            let grid_frame = match frame {
                0..200 => Some(frame),
                292.. => Some(frame - 292),
                _ => None,
            };
            if let Some(grid_frame) = grid_frame {
                assert_eq!(
                    lit,
                    grid_frame % cycle < 2,
                    "{per_beat} per beat, frame {frame}"
                );
            }
        }
    }
}

// Each dimmer starts strobing one frame after the one before it, at 10 Hz.
const STAGGERED_STROBE: &str = r#"return {render=function(ctx)
    return sf.each(sf.all(), function(cell, index)
        local started = ctx.autoloop_elapsed_beats >= (index - 1) * 0.05
        return sf.scene {sf.dimmer(cell, 1), started and sf.strobe(cell, 10) or nil}
    end) end}"#;

#[test]
fn cells_at_one_rate_flash_together_whenever_each_started() {
    let mut engine = engine(dimmers(4), &[("staggered", STAGGERED_STROBE)]);
    select(&mut engine, 0.0, "staggered", Transition::Cut);
    let frames = lit(&mut engine, 0..2 * FRAMES_PER_SECOND, 4);
    // The first cell's burst opens at frame 0; the others join its gate as they start, by
    // frame 4 at the latest.
    for (frame, cells) in frames.iter().enumerate().skip(4) {
        assert!(
            cells.iter().all(|&cell| cell == (frame % 4 < 2)),
            "{frame}: {cells:?}"
        );
    }
}

// Strobes for 4 s (8 beats at 120 bpm), then holds steady.
const STROBE_THEN_STEADY: &str = r#"return {render=function(ctx)
    local strobing = ctx.autoloop_elapsed_beats < 8
    return sf.scene {sf.dimmer(sf.all(), 1), strobing and sf.strobe(sf.all(), 10) or nil} end}"#;

#[test]
fn a_strobe_past_its_burst_cap_stays_dark_until_it_is_no_longer_asked_for() {
    let mut engine = engine(dimmers(1), &[("burst", STROBE_THEN_STEADY)]);
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 2.0 })
        .unwrap();
    select(&mut engine, 0.0, "burst", Transition::Cut);
    let frames: Vec<bool> = lit(&mut engine, 0..6 * FRAMES_PER_SECOND, 1)
        .into_iter()
        .map(|cells| cells[0])
        .collect();
    let second = |index: usize| &frames[index * FRAMES_PER_SECOND..(index + 1) * FRAMES_PER_SECOND];
    assert_eq!(second(0).iter().filter(|&&lit| lit).count(), 20);
    assert!(second(2).iter().chain(second(3)).all(|&lit| !lit));
    assert!(second(5).iter().all(|&lit| lit));
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

/// A one-channel Gobo1 whose ChannelSets are `sets` (name, selects a wheel slot), evenly spaced
/// in DMX order. A set that selects no slot stands for a shake or spin range packed into Gobo1.
fn gobo_wheel(sets: &[(&str, bool)]) -> wire::FixtureProfile {
    let step = 256 / sets.len() as u32;
    let sets = sets
        .iter()
        .enumerate()
        .map(|(index, (name, slot))| wire::ChannelSet {
            name: (*name).into(),
            dmx_from: index as u32 * step,
            dmx_to: if index + 1 == sets.len() {
                255
            } else {
                (index as u32 + 1) * step - 1
            },
            physical_from: index as f64,
            physical_to: index as f64,
            // GDTF's default slot colour, as Autolux compiles a gobo slot without one.
            wheel_slot_colour: slot.then_some(wire::CieColor {
                x: 0.3127,
                y: 0.329,
                luminance: 100.0,
            }),
        })
        .collect::<Vec<_>>();
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
                    physical_to: (sets.len() - 1) as f64,
                    snap: true,
                    sets,
                    ..Default::default()
                }],
            }],
        }),
    }
}

/// Gobo wheels at DMX 1..3: Open first with a shake range packed in after the slots, an `oPEN`
/// slot in second place, and no open slot behind a spin range; an RGB fixture at 4..6.
fn gobo_rig() -> FixtureRig {
    rig_with(
        json!({"fixtures": [
            {"fixture_uuid": "open_first", "profile": "open_first", "universe": 0, "address": 1},
            {"fixture_uuid": "late_open", "profile": "late_open", "universe": 0, "address": 2},
            {"fixture_uuid": "no_open", "profile": "no_open", "universe": 0, "address": 3},
            {"fixture_uuid": "plain", "profile": "generic_rgb", "universe": 0, "address": 4}]}),
        &[
            (
                "open_first",
                gobo_wheel(&[
                    ("Open", true),
                    ("Dots", true),
                    ("Lines", true),
                    ("Tunnel", true),
                    ("Shake", false),
                ]),
            ),
            (
                "late_open",
                gobo_wheel(&[("Dots", true), ("oPEN", true), ("Lines", true)]),
            ),
            (
                "no_open",
                gobo_wheel(&[
                    ("Spin", false),
                    ("Dots", true),
                    ("Lines", true),
                    ("Tunnel", true),
                ]),
            ),
        ],
    )
}

#[test]
fn gobo_slot_zero_is_the_open_slot_and_later_slots_wrap_over_the_others_in_dmx_order() {
    let rig = gobo_rig();
    let name = |cell: &str, slot: u64| rig.profile(cell).unwrap().gobo_name(slot);
    for (slot, open_first, late_open, no_open) in [
        (0, "Open", "oPEN", "Dots"),
        (1, "Dots", "Dots", "Lines"),
        (2, "Lines", "Lines", "Tunnel"),
        (3, "Tunnel", "Dots", "Lines"),
        (4, "Dots", "Lines", "Tunnel"),
        (7, "Dots", "Dots", "Lines"),
    ] {
        for (cell, expected) in [
            ("open_first", open_first),
            ("late_open", late_open),
            ("no_open", no_open),
        ] {
            assert_eq!(name(cell, slot).as_deref(), Some(expected), "{cell} {slot}");
        }
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
    // Lines, Lines, Tunnel and Dots, Lines, Tunnel.
    for (name, expected) in [("two", [102, 170, 192]), ("four", [51, 170, 192])] {
        select(&mut engine, 0.0, name, Transition::Cut);
        let frame = engine.render_at(0.0).unwrap();
        assert_eq!(frame.script_error, None);
        assert_eq!(frame.dmx_frames_by_universe[&0][..3], expected, "{name}");
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

/// Every compiled mode the generic test data carries (the stage's patched modes are the shipped
/// fixture types), plus wheel_spinner without its black slot, whose gate shutter is then its
/// only way to go dark.
fn test_data_profiles() -> Vec<wire::FixtureProfile> {
    let read =
        |path: PathBuf| wire::RigDefinition::decode(fs::read(path).unwrap().as_slice()).unwrap();
    let mut profiles =
        read(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/data/compiled_profiles.pb"))
            .profiles;
    profiles.extend(read(support::sample_rig().join("generic.rig.pb")).profiles);
    let stage = read(support::stage_rig());
    profiles.extend(stage.profiles.into_iter().filter(|profile| {
        stage
            .fixtures
            .iter()
            .any(|fixture| fixture.profile_id == profile.profile_id)
    }));
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

#[test]
fn colour_and_gobo_wheels_change_together_half_way_through_a_fade() {
    // wheel_spinner's Color1 at DMX 1 (Red 64, Blue 192) and a gobo wheel at DMX 4.
    let mut wheels = support::compiled_profile("wheel_spinner#3ch", "wheels");
    let mut gobo = gobo_wheel(&[("Open", true), ("Dots", true), ("Lines", true)])
        .mode
        .unwrap()
        .channels
        .remove(0);
    gobo.offsets = vec![4];
    wheels.mode.as_mut().unwrap().channels.push(gobo);
    let rig = rig_with(
        json!({"fixtures": [
            {"fixture_uuid": "wheels", "profile": "wheels", "universe": 0, "address": 1}]}),
        &[("wheels", wheels)],
    );
    let look = |colour: &str, gobo: &str| {
        format!(
            "return {{render=function(ctx) return sf.scene {{sf.dimmer(sf.all(), 1), \
             sf.colour(sf.all(), sf.rgb({colour})), {gobo}}} end}}"
        )
    };
    let red_dots = look("1, 0, 0", "sf.gobo(sf.all(), 1)");
    let blue_lines = look("0, 0, 1", "sf.gobo(sf.all(), 2)");
    let blue_open = look("0, 0, 1", "nil");
    // (colour DMX, gobo DMX) before and from half-way; an unset gobo rests on Open at 0.
    for (from, to, before, after) in [
        (
            ("red_dots", &red_dots),
            ("blue_lines", &blue_lines),
            (64, 85),
            (192, 170),
        ),
        (
            ("red_dots", &red_dots),
            ("blue_open", &blue_open),
            (64, 85),
            (192, 0),
        ),
        (
            ("blue_open", &blue_open),
            ("red_dots", &red_dots),
            (192, 0),
            (64, 85),
        ),
    ] {
        let mut engine = engine(
            rig.clone(),
            &[(from.0, from.1.as_str()), (to.0, to.1.as_str())],
        );
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
        for (seconds, expected) in [(1.5, before), (1.99, before), (2.0, after), (2.5, after)] {
            let frame = &engine.render_at(seconds).unwrap().dmx_frames_by_universe[&0];
            assert_eq!(
                (frame[0], frame[3]),
                expected,
                "{} to {} at {seconds}",
                from.0,
                to.0
            );
        }
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

/// Requested colour -> the stage Spot's Color1 DMX (Open 0, Red 32, Blue 111, Yellow 128,
/// Magenta 160, Amber 192, Cyan 224). Only white lands on Open; a pale colour takes its nearest
/// coloured slot. autolux/tests/test_palette_variants.py holds palettes.nearest_slot to it.
const SPOT_PICKS: [([f64; 3], u8); 9] = [
    ([1.0, 0.82, 0.6], 192),
    ([1.0, 1.0, 1.0], 0),
    ([0.5, 0.5, 0.5], 0),
    ([1.0, 0.0, 0.0], 32),
    ([0.0, 0.7, 1.0], 224),
    ([0.85, 0.92, 1.0], 160),
    ([1.0, 1.0, 0.0], 128),
    ([1.0, 1.0, 0.7], 128),
    ([0.5, 0.0, 1.0], 111),
];

fn spot() -> FixtureRig {
    rig(
        json!({"fixtures": [{"fixture_uuid": "spot", "universe": 0, "address": 1,
        "profile": "chauvet_intimidator_spot_110_12ch"}]}),
    )
}

/// The Spot's Color1 is its sixth channel.
const SPOT_COLOUR: usize = 5;

#[test]
fn only_white_lands_on_the_open_slot_of_a_colour_wheel() {
    let rig = spot();
    for ([red, green, blue], dmx) in SPOT_PICKS {
        let mut states = rig.empty_states();
        let state = states.get_mut("spot").unwrap();
        state.intensity = 1.0;
        state.colour = Rgb { red, green, blue };
        assert_eq!(
            rig.encode(&states).unwrap()[&0][SPOT_COLOUR],
            dmx,
            "{red}, {green}, {blue}"
        );
    }
}

#[test]
fn sf_white_is_a_fixed_palette_slot_that_opens_the_wheel_and_a_blend_snaps_to_it() {
    let white = "return {render=function(ctx)
        assert(sf.white() == sf.palette().white and sf.palette('house').white == sf.white())
        return sf.scene {sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.white())} end}";
    let pale = solid("1, 0.82, 0.6");
    let mut engine = engine(spot(), &[("white", white), ("pale", &pale)]);
    select(&mut engine, 0.0, "white", Transition::Cut);
    let frame = engine.render_at(0.0).unwrap();
    assert_eq!(frame.script_error, None);
    assert_eq!(
        frame.fixture_properties_before_strobe["spot"].colour,
        Rgb {
            red: 1.0,
            green: 1.0,
            blue: 1.0
        }
    );
    // The white slot ignores palette changes: mid palette crossfade it is still full white.
    engine
        .apply(
            0.0,
            &Command::SetPalette {
                name: "house".into(),
            },
        )
        .unwrap();
    let frame = engine.render_at(0.25).unwrap();
    assert_eq!(
        frame.fixture_properties_before_strobe["spot"].colour.green,
        1.0
    );
    assert_eq!(frame.dmx_frames_by_universe[&0][SPOT_COLOUR], 0);
    // 120 bpm: a 4-beat crossfade from 1 s reaches half-way at 2 s; Open, then Amber.
    select(
        &mut engine,
        1.0,
        "pale",
        Transition::Crossfade {
            duration_beats: 4.0,
        },
    );
    for (seconds, dmx) in [(1.5, 0), (1.99, 0), (2.0, 192), (2.5, 192)] {
        let frame = engine.render_at(seconds).unwrap();
        assert_eq!(frame.script_error, None);
        assert_eq!(
            frame.dmx_frames_by_universe[&0][SPOT_COLOUR], dmx,
            "{seconds}"
        );
    }
}
