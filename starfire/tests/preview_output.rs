mod support;

use serde_json::{Value, json};
use starfire::model::{Command, TimedCommand, Transition};
use starfire::protocol::wire;
use starfire::replay::{PREVIEW_KIND, REPLAY_SCHEMA_VERSION, Replay, write_preview};
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
        command: Command::Beat {
            beat_index,
            bar_phase: None,
            beats_per_bar: 4,
            bpm: 120.0,
        },
    }
}

/// Two seconds at 120 bpm, dark until a loop is selected at 0.5 s.
fn preview_lines(rate_hz: u32) -> Vec<Value> {
    let rig = support::rig(json!({
        "fixtures": [placed("left", 1, -2000.0), placed("right", 4, 2000.0)],
        "groups": {"wash": ["left", "right"]},
    }));
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "red_half".into(),
        bank: "test".into(),
        lua_source: RED_HALF.into(),
    }])
    .unwrap();
    let mut events: Vec<_> = (0..4)
        .map(|index| beat(index as f64 * 0.5, index))
        .collect();
    events.insert(
        2,
        TimedCommand {
            at_seconds: 0.5,
            command: Command::Select {
                bank: "test".into(),
                autoloop: "red_half".into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        },
    );
    let replay = Replay {
        schema_version: REPLAY_SCHEMA_VERSION,
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
        // 120 bpm: two beats a second on the grid the first beat anchors.
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
    // A cell without movement or gobo carries no direction or gobo entry.
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
