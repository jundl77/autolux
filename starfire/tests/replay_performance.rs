//! Coarse regression budgets for the complete replay renderer, including Lua.

use std::path::Path;
use std::process::Command as Process;

use serde_json::{Value, json};
use starfire::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Target, TimedCommand, Transition,
};
use starfire::replay::{REPLAY_SCHEMA_VERSION, Replay};

const SHOW_DURATION_SECONDS: f64 = 90.0;
const FRAME_RATE_HZ: u32 = 40;
const MIN_ROOM_REALTIME_FACTOR: f64 = 10.0;
const MIN_SCALED_REALTIME_FACTOR: f64 = 2.0;
const SCALED_FIXTURE_COUNT: usize = 64;
const SCALED_MOVER_COUNT: usize = 16;
const SCALED_STRIP_COUNT: usize = 8;
const FIXTURES_PER_UNIVERSE: usize = 32;
const FIXTURE_ADDRESS_STRIDE: usize = 16;
const BEAT_ANCHOR_INTERVAL_SECONDS: f64 = 0.5;
const TEMPO_CHANGE_SECONDS: f64 = 45.0;
const INITIAL_TEMPO_BPM: f64 = 120.0;
const FINAL_TEMPO_BPM: f64 = 180.0;
const SECONDS_PER_MINUTE: f64 = 60.0;
const FACTOR_RELATIVE_TOLERANCE: f64 = 1e-12;

fn mixed_show() -> Replay {
    let mut events = Vec::new();
    let mut push = |at_seconds, command| {
        events.push(TimedCommand {
            at_seconds,
            command,
        })
    };
    let anchor_count = (SHOW_DURATION_SECONDS / BEAT_ANCHOR_INTERVAL_SECONDS) as usize;
    for index in 0..=anchor_count {
        let at_seconds = index as f64 * BEAT_ANCHOR_INTERVAL_SECONDS;
        let position_beats = at_seconds.min(TEMPO_CHANGE_SECONDS) * INITIAL_TEMPO_BPM
            / SECONDS_PER_MINUTE
            + (at_seconds - TEMPO_CHANGE_SECONDS).max(0.0) * FINAL_TEMPO_BPM / SECONDS_PER_MINUTE;
        push(
            at_seconds,
            Command::Beat {
                position_beats,
                bpm: if at_seconds < TEMPO_CHANGE_SECONDS {
                    INITIAL_TEMPO_BPM
                } else {
                    FINAL_TEMPO_BPM
                },
            },
        );
    }
    for (at_seconds, bank, autoloop) in [
        (0.0, "atmospheric", "blue_tide"),
        (15.0, "breakdown", "violet_space"),
        (30.0, "buildup", "current"),
        (45.0, "drop", "blue_impact"),
        (60.0, "buildup", "current"),
        (75.0, "drop", "blue_impact"),
    ] {
        push(
            at_seconds,
            Command::Select {
                bank: bank.into(),
                autoloop: autoloop.into(),
                transition: if at_seconds == 0.0 {
                    Transition::Cut
                } else {
                    Transition::Crossfade {
                        duration_beats: 4.0,
                    }
                },
            },
        );
    }
    for (at_seconds, level) in [(5.0, 0.2), (25.0, 0.85), (45.0, 1.0), (80.0, 0.4)] {
        push(at_seconds, Command::SetNormalizedRmsLevel { level });
    }
    push(12.0, Command::SetMaster { intensity: 0.8 });
    push(64.0, Command::SetMaster { intensity: 0.9 });
    push(36.0, Command::SetSpeed { multiplier: 1.4 });
    push(70.0, Command::SetSpeed { multiplier: 0.5 });
    for at_seconds in [8.0, 32.0, 46.0, 62.0, 76.0] {
        push(
            at_seconds,
            Command::SetOverride {
                value: LightingOverride {
                    id: "arrival".into(),
                    target: Target::Group("wash".into()),
                    values: PropertyValues {
                        intensity: Some(0.9),
                        strobe_hz: Some(5.0),
                        ..Default::default()
                    },
                    intensity_multiplier: None,
                    lifetime: OverrideLifetime::Seconds {
                        duration_seconds: 1.0,
                    },
                    priority: 100,
                },
            },
        );
    }
    push(SHOW_DURATION_SECONDS, Command::Stop);
    events.sort_by(|left, right| left.at_seconds.total_cmp(&right.at_seconds));
    Replay {
        schema_version: REPLAY_SCHEMA_VERSION,
        duration_seconds: SHOW_DURATION_SECONDS,
        frame_rate_hz: FRAME_RATE_HZ,
        events,
    }
}

fn scaled_rig() -> Value {
    let mut fixtures = Vec::new();
    let mut movers = Vec::new();
    let mut wash = Vec::new();
    let mut strip = Vec::new();
    for index in 0..SCALED_FIXTURE_COUNT {
        let id = format!("fixture_{index}");
        let mover = index < SCALED_MOVER_COUNT;
        let pixel = index >= SCALED_FIXTURE_COUNT - SCALED_STRIP_COUNT;
        fixtures.push(json!({
            "id": id,
            "profile": if mover {
                "chauvet_intimidator_spot_110_12ch"
            } else if pixel {
                "generic_rgb"
            } else {
                "fun_generation_separ_quad_uv_8ch"
            },
            "universe": index / FIXTURES_PER_UNIVERSE,
            "address": 1 + index % FIXTURES_PER_UNIVERSE * FIXTURE_ADDRESS_STRIDE,
        }));
        if mover {
            movers.push(id);
        } else if pixel {
            strip.push(id);
        } else {
            wash.push(id);
        }
    }
    json!({"schema_version": 1, "fixtures": fixtures, "groups": {"movers": movers, "wash": wash, "strip": strip}})
}

fn assert_render_budget(name: &str, rig: &Value, minimum_factor: f64) {
    let directory = tempfile::tempdir().unwrap();
    let replay_path = directory.path().join("show.json");
    let rig_path = directory.path().join("rig.json");
    std::fs::write(&replay_path, serde_json::to_vec(&mixed_show()).unwrap()).unwrap();
    std::fs::write(&rig_path, serde_json::to_vec(rig).unwrap()).unwrap();
    let output = Process::new(env!("CARGO_BIN_EXE_starfire"))
        .arg("replay")
        .arg(replay_path)
        .arg("--rig")
        .arg(rig_path)
        .arg("--profiles")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("../rigs/profiles.json"))
        .arg("--autoloops")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloop"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        report["frames"],
        (SHOW_DURATION_SECONDS * f64::from(FRAME_RATE_HZ)) as u64 + 1
    );
    assert_eq!(report["script_errors"], json!([]));
    let elapsed = report["render_elapsed_seconds"].as_f64().unwrap();
    let factor = report["render_realtime_factor"].as_f64().unwrap();
    assert!(elapsed.is_finite() && elapsed > 0.0);
    assert!((factor - SHOW_DURATION_SECONDS / elapsed).abs() <= factor * FACTOR_RELATIVE_TOLERANCE);
    println!(
        "{name}: {SHOW_DURATION_SECONDS:.0}s show rendered in {elapsed:.3}s ({factor:.1}x realtime; minimum {minimum_factor:.1}x)"
    );
    assert!(
        factor >= minimum_factor,
        "{name} renderer regressed to {factor:.1}x realtime; required >= {minimum_factor:.1}x"
    );
}

#[test]
fn room_show_keeps_ten_times_realtime_headroom() {
    let rig: Value = serde_json::from_str(include_str!("../../rigs/room.json")).unwrap();
    assert_render_budget("room / 14 fixtures", &rig, MIN_ROOM_REALTIME_FACTOR);
}

#[test]
fn sixty_four_fixture_show_keeps_twice_realtime_headroom() {
    assert_render_budget(
        "scaled / 64 fixtures",
        &scaled_rig(),
        MIN_SCALED_REALTIME_FACTOR,
    );
}
