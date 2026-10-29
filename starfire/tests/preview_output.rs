mod support;

use serde_json::{Value, json};
use starfire::model::{
    Command, CueSection, Envelope, Overlay, SectionIntent, Step, TimedCommand, Transition,
};
use starfire::protocol::wire;
use starfire::replay::{PREVIEW_KIND, Replay, render_replay, write_preview};
use starfire::script::AutoloopCatalog;

const RED_HALF: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 0.5), sf.colour(sf.all(), sf.rgb(1, 0, 0))} end}"#;

fn placed(uuid: &str, address: u32, x_mm: f64) -> Value {
    json!({"fixture_uuid": uuid, "profile": "generic_rgb", "universe": 0, "address": address,
        "matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, x_mm, 500.0, 3000.0]})
}

fn beat(at_seconds: f64, beat_index: u64) -> TimedCommand {
    TimedCommand {
        at_seconds,
        command: support::beat(beat_index, None, 120.0),
    }
}

fn preview_lines(rate_hz: u32) -> Vec<Value> {
    let rig = support::rig(json!({
        "fixtures": [placed("left", 1, -2000.0), placed("right", 4, 2000.0)],
        "groups": {"wash": ["left", "right"]},
    }));
    let catalog = support::test_catalog(&[("red_half", RED_HALF)]);
    let mut events: Vec<_> = (0..4)
        .map(|index| beat(index as f64 * 0.5, index))
        .collect();
    events.insert(
        2,
        TimedCommand {
            at_seconds: 0.5,
            command: support::select("test", "red_half", Transition::Cut),
        },
    );
    let replay = Replay {
        duration_seconds: 2.0,
        frame_rate_hz: 40,
        palettes: support::palette_definitions(),
        events,
    };
    let mut output = Vec::new();
    let summary = write_preview(&replay, rig, catalog, rate_hz, &mut output).unwrap();
    assert!(summary.script_errors.is_empty());
    let lines: Vec<Value> = String::from_utf8(output)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(lines.len(), summary.frames + 1);
    lines
}

#[test]
fn the_header_names_cells_by_uuid_with_positions_groups_and_bar_length() {
    let lines = preview_lines(10);
    assert_eq!(
        lines[0],
        json!({
            "kind": PREVIEW_KIND, "frame_rate_hz": 10, "duration_seconds": 2.0, "frames": 21,
            "beats_per_bar": 4,
            "cells": [
                {"id": "left", "fixture": "left", "x": -2.0, "y": 0.5, "z": 3.0},
                {"id": "right", "fixture": "right", "x": 2.0, "y": 0.5, "z": 3.0},
            ],
            "groups": {"wash": [0, 1]},
        })
    );
}

#[test]
fn frames_come_at_the_preview_rate_with_one_entry_per_cell() {
    let lines = preview_lines(10);
    let frames = &lines[1..];
    assert_eq!(frames.len(), 21);
    for (index, frame) in frames.iter().enumerate() {
        assert_eq!(frame["t"], json!(index as f64 / 10.0));
        assert_eq!(frame["beat"], json!(index as f64 / 5.0));
        assert_eq!(frame["c"].as_array().unwrap().len(), 2);
        assert!(frame.get("fault").is_none());
    }
    let dark = &frames[4];
    assert_eq!(
        (&dark["bank"], &dark["autoloop"]),
        (&Value::Null, &Value::Null)
    );
    assert_eq!(
        (&dark["c"][0][0], &dark["c"][1][0]),
        (&json!(0.0), &json!(0.0))
    );
    let lit = &frames[5];
    assert_eq!(
        (&lit["bank"], &lit["autoloop"]),
        (&json!("test"), &json!("red_half"))
    );
    assert_eq!(
        lit["c"],
        json!([[0.5, 1.0, 0.0, 0.0, 0.0], [0.5, 1.0, 0.0, 0.0, 0.0]])
    );
}

#[test]
fn a_higher_rate_only_adds_frames() {
    let lines = preview_lines(20);
    assert_eq!(lines[0]["frames"], json!(41));
    assert_eq!(lines[1 + 20], preview_lines(10)[1 + 10]);
}

fn scanner_and_wash() -> starfire::fixtures::FixtureRig {
    let mut definition = support::definition(&json!({"fixtures": [
        {"fixture_uuid": "scanner", "profile": "beam_scanner", "universe": 0, "address": 1,
            "matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 3000.0]},
        placed("wash", 7, 0.0),
    ]}));
    definition.profiles.push(support::compiled_profile(
        "beam_scanner#6ch",
        "beam_scanner",
    ));
    starfire::fixtures::FixtureRig::from_wire(&definition).unwrap()
}

fn one_loop(source: &str, duration_seconds: f64) -> (AutoloopCatalog, Replay) {
    let catalog = support::test_catalog(&[("under_test", source)]);
    let select = TimedCommand {
        at_seconds: 0.0,
        command: support::select("test", "under_test", Transition::Cut),
    };
    let replay = Replay {
        duration_seconds,
        frame_rate_hz: 40,
        palettes: support::palette_definitions(),
        events: vec![beat(0.0, 0), select],
    };
    (catalog, replay)
}

fn frames_of(output: Vec<u8>) -> Vec<Value> {
    String::from_utf8(output)
        .unwrap()
        .lines()
        .skip(1)
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn a_moving_head_carries_its_beam_direction_and_gobo() {
    let (catalog, replay) = one_loop(
        r#"return {render=function(ctx) return sf.scene {
            sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(0, 0, 1)),
            sf.pan_tilt(sf.fixture("scanner"), 0.5, 0.5),
            sf.channel_preset(sf.fixture("scanner"), "Gobo1", "Dots")} end}"#,
        0.0,
    );
    let mut output = Vec::new();
    write_preview(&replay, scanner_and_wash(), catalog, 10, &mut output).unwrap();
    assert_eq!(
        frames_of(output)[0]["c"],
        json!([
            [1.0, 0.0, 0.0, 1.0, 0.0, [0.0, 0.0, -1.0], "Dots"],
            [1.0, 0.0, 0.0, 1.0, 0.0]
        ])
    );
}

#[test]
fn preview_frames_are_the_shows_own_frames_under_the_slew_limit_and_strobe_caps() {
    let (catalog, replay) = one_loop(
        r#"return {render=function(ctx) return sf.scene {
            sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(1, 1, 1)),
            sf.pan_tilt(sf.fixture("scanner"), ctx.autoloop_elapsed_beats % 2 < 1 and 0 or 1, 0.8),
            sf.strobe(sf.fixture("wash"), 8)} end}"#,
        4.0,
    );
    let mut replay = replay;
    replay.events.insert(
        0,
        TimedCommand {
            at_seconds: 0.0,
            command: Command::SetIntensity { level: 0.7 },
        },
    );
    replay.events.insert(
        0,
        TimedCommand {
            at_seconds: 0.0,
            command: Command::SetStrobeMaxBurstSeconds { seconds: 1.0 },
        },
    );
    let rig = scanner_and_wash();
    let shown = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    let mut output = Vec::new();
    write_preview(&replay, rig.clone(), catalog, 10, &mut output).unwrap();
    let frames = frames_of(output);
    assert_eq!(frames.len(), shown.len().div_ceil(4));
    let round = |value: f64| (value * 1000.0).round() / 1000.0 + 0.0;
    let (mut chased, mut strobed) = (0, 0);
    for (index, frame) in frames.iter().enumerate() {
        let states = &shown[index * 4].fixture_properties_before_strobe;
        let scanner = &states["scanner"];
        let direction = rig
            .beam_direction("scanner", scanner.pan, scanner.tilt)
            .unwrap()
            .map(round);
        assert_eq!(frame["c"][0][5], json!(direction), "frame {index}");
        assert_eq!(
            frame["c"][1][4],
            json!(round(states["wash"].strobe_hz)),
            "frame {index}"
        );
        chased += usize::from(scanner.pan > 0.0 && scanner.pan < 1.0);
        strobed += usize::from(states["wash"].strobe_hz > 0.0);
    }
    assert!(
        chased > frames.len() / 2,
        "the slew limit shapes {chased} frames"
    );
    assert!(
        strobed > 0 && strobed < frames.len(),
        "the burst cap ends the strobe ({strobed})"
    );
}

#[test]
fn a_preview_rate_that_does_not_divide_the_show_rate_is_refused() {
    let (catalog, replay) = one_loop(RED_HALF, 1.0);
    let error = write_preview(&replay, scanner_and_wash(), catalog, 30, &mut Vec::new())
        .err()
        .unwrap();
    assert!(error.to_string().contains("must divide"), "{error}");
}

#[test]
fn a_script_fault_is_flagged_on_every_dark_frame_and_named_once() {
    let (catalog, replay) = one_loop(r#"return {render=function(ctx) error("boom") end}"#, 1.0);
    let mut output = Vec::new();
    let summary = write_preview(&replay, scanner_and_wash(), catalog, 10, &mut output).unwrap();
    let frames = frames_of(output);
    assert_eq!(summary.script_errors.len(), 1);
    assert!(frames.iter().all(|frame| frame["fault"] == json!(true)));
    assert!(frames[0]["error"].as_str().unwrap().contains("boom"));
    assert!(frames[1..].iter().all(|frame| frame.get("error").is_none()));
}

#[test]
fn a_colour_wheel_shows_the_slot_its_encode_selects() {
    let rig = || {
        let mut definition = support::definition(&json!({"fixtures": [
            {"fixture_uuid": "wheel", "profile": "wheel_spinner", "universe": 0, "address": 1}]}));
        definition.profiles.push(support::compiled_profile(
            "wheel_spinner#3ch",
            "wheel_spinner",
        ));
        starfire::fixtures::FixtureRig::from_wire(&definition).unwrap()
    };
    let colour_of = |rgb: &str| {
        let source = format!(
            "return {{render=function(ctx) return sf.scene {{
                sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb({rgb}))}} end}}"
        );
        let (catalog, replay) = one_loop(&source, 0.0);
        let mut output = Vec::new();
        write_preview(&replay, rig(), catalog, 10, &mut output).unwrap();
        frames_of(output)[0]["c"][0].as_array().unwrap()[1..4].to_vec()
    };
    let pinkish = colour_of("1, 0.3, 0.3");
    assert_eq!(pinkish, colour_of("1, 0, 0"));
    assert_ne!(pinkish, [json!(1.0), json!(0.3), json!(0.3)]);
    let [red, green, blue] = [0, 1, 2].map(|channel| pinkish[channel].as_f64().unwrap());
    assert!(red > 0.5 && green < red && blue < red, "{pinkish:?}");
}

#[test]
fn frames_name_the_cues_section_and_its_sounding_overlays() {
    let definition = |name: &str, source: &str| wire::AutoloopDefinition {
        autoloop_id: name.into(),
        bank: "test".into(),
        lua_source: source.into(),
        intensity_htp: false,
    };
    let catalog = AutoloopCatalog::from_definitions(&[
        definition("base", RED_HALF),
        definition("flash", RED_HALF),
    ])
    .unwrap();
    let drop = CueSection {
        start_beat_index: 4,
        intent: SectionIntent::Drop,
        rotation: vec![Step {
            bank: "test".into(),
            autoloop: "base".into(),
            bars: 16,
            transition: Transition::Cut,
        }],
        overlays: vec![Overlay {
            bank: "test".into(),
            autoloop: "flash".into(),
            priority: 0,
            begin_beats: 0.0,
            duration_beats: 2.0,
            every_bars: 0,
            envelope: Envelope::default(),
            dip: false,
        }],
        palette: "techno".into(),
        drop_strength: None,
        seed: 7,
    };
    let mut events: Vec<_> = (0..9)
        .map(|index| beat(index as f64 * 0.5, index))
        .collect();
    events.insert(
        1,
        TimedCommand {
            at_seconds: 0.0,
            command: Command::CueSection {
                value: Box::new(drop),
            },
        },
    );
    let replay = Replay {
        duration_seconds: 4.0,
        frame_rate_hz: 40,
        palettes: support::palette_definitions(),
        events,
    };
    let mut output = Vec::new();
    write_preview(&replay, scanner_and_wash(), catalog, 10, &mut output).unwrap();
    let frames = frames_of(output);
    let at = |index: usize| (&frames[index]["intent"], frames[index].get("overlays"));
    assert_eq!(at(19), (&Value::Null, None));
    assert_eq!(at(20), (&json!("drop"), Some(&json!(["test/flash"]))));
    assert_eq!(at(29), (&json!("drop"), Some(&json!(["test/flash"]))));
    assert_eq!(at(30), (&json!("drop"), None));
    assert_eq!(frames[20]["autoloop"], json!("base"));
}
