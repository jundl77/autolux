mod support;

use std::fs;
use std::process::{Command as ProcessCommand, Stdio};
use std::time::{Duration, Instant};

use serde_json::json;
use starfire::engine::MAX_SCRIPT_ERROR_BYTES;
use starfire::fixtures::{FixtureRig, MAX_FIXTURES};
use starfire::model::{Command, TimedCommand, Transition};
use starfire::mvr::MAX_MVR_BYTES;
use starfire::replay::{Replay, render_replay};
use starfire::script::AutoloopCatalog;

fn simple_rig() -> serde_json::Value {
    json!({"schema_version":1,"fixtures":[
        {"id":"light","profile":"generic_dimmer","universe":0,"address":1}
    ]})
}

fn fault_replay(duration_seconds: f64) -> (Replay, FixtureRig, AutoloopCatalog) {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    fs::write(
        directory.path().join("test/fault.lua"),
        "return {render=function(ctx) error(string.rep('é', 800000)) end}",
    )
    .unwrap();
    let catalog = AutoloopCatalog::load(directory.path()).unwrap();
    let rig = support::rig(simple_rig());
    let replay = Replay {
        schema_version: 1,
        duration_seconds,
        frame_rate_hz: 40,
        events: vec![TimedCommand {
            at_seconds: 0.0,
            command: Command::Select {
                bank: "test".into(),
                autoloop: "fault".into(),
                transition: Transition::Cut,
            },
        }],
    };
    (replay, rig, catalog)
}

#[test]
fn native_pattern_matching_cannot_bypass_the_script_budget() {
    const PROCESS_TIMEOUT: Duration = Duration::from_secs(5);
    const EXIT_POLL_INTERVAL: Duration = Duration::from_millis(10);

    let directory = tempfile::tempdir().unwrap();
    let autoloops = directory.path().join("autoloops");
    fs::create_dir_all(autoloops.join("test")).unwrap();
    let rig = directory.path().join("rig.mvr");
    support::write_mvr(&rig, &simple_rig());

    for expression in [
        "string.find(text, pattern)",
        "string.match(text, pattern)",
        "string.gmatch(text, pattern)()",
        "string.gsub(text, pattern, '')",
        "text:find(pattern)",
        "text:match(pattern)",
        "text:gmatch(pattern)()",
        "text:gsub(pattern, '')",
    ] {
        fs::write(
            autoloops.join("test/pattern.lua"),
            format!(
                "return {{render=function(ctx) local text=string.rep('a',24); \
                 local pattern=string.rep('a*',24)..'b'; {expression}; return {{}} end}}"
            ),
        )
        .unwrap();
        let mut child = ProcessCommand::new(env!("CARGO_BIN_EXE_starfire"))
            .arg("check")
            .arg("--rig")
            .arg(&rig)
            .arg("--autoloops")
            .arg(&autoloops)
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + PROCESS_TIMEOUT;
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            if Instant::now() >= deadline {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!("native pattern escaped the Lua budget: {expression}");
            }
            std::thread::sleep(EXIT_POLL_INTERVAL);
        };
        let output = child.wait_with_output().unwrap();
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        assert!(!status.success(), "accepted {expression}");
        assert!(
            diagnostic.contains("nil value"),
            "{expression}: {diagnostic}"
        );
    }
}

#[test]
fn a_giant_multibyte_lua_error_cannot_amplify_replay_memory_per_frame() {
    let (replay, rig, catalog) = fault_replay(90.0);
    let result = render_replay(&replay, rig, catalog).unwrap();
    assert_eq!(result.frames.len(), 3601);
    for frame in result.frames {
        let diagnostic = frame.script_error.unwrap();
        assert!(diagnostic.len() <= MAX_SCRIPT_ERROR_BYTES);
        assert!(diagnostic.ends_with("… [truncated]"));
        assert!(diagnostic.contains('é'));
        assert!(
            frame.dmx_frames_by_universe[&0]
                .iter()
                .all(|channel| *channel == 0)
        );
    }
}

#[test]
fn repeated_fault_diagnostics_have_a_total_replay_memory_budget() {
    let (replay, rig, catalog) = fault_replay(220.0);
    let error = render_replay(&replay, rig, catalog).err().unwrap();
    assert!(error.to_string().contains("diagnostic memory budget"));
}

#[test]
fn rig_size_is_bounded_before_archive_parsing_from_memory_or_file() {
    let input = vec![0; MAX_MVR_BYTES + 1];
    let error = FixtureRig::from_mvr(&input).unwrap_err();
    assert!(format!("{error:#}").contains("exceeds its size limit"));
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("oversized.mvr");
    fs::write(&path, input).unwrap();
    let error = FixtureRig::load(&path).unwrap_err();
    assert!(format!("{error:#}").contains("input byte limit"));
}

#[test]
fn fixture_and_group_counts_and_identifiers_cannot_expand_frame_storage_unboundedly() {
    let mut rig = simple_rig();
    rig["fixtures"] = json!(
        (0..=MAX_FIXTURES)
            .map(|index| json!({
                "id":format!("light_{index}"),"profile":"generic_dimmer","universe":index,
                "address":1
            }))
            .collect::<Vec<_>>()
    );
    let error = support::try_rig(rig.clone()).unwrap_err();
    assert!(error.to_string().contains("fixtures"));

    let mut rig = simple_rig();
    let groups: serde_json::Map<String, serde_json::Value> = (0..=MAX_FIXTURES)
        .map(|index| (format!("group_{index}"), json!(["light"])))
        .collect();
    rig["groups"] = groups.into();
    let error = support::try_rig(rig.clone()).unwrap_err();
    assert!(error.to_string().contains("group count"));

    let mut rig = simple_rig();
    rig["fixtures"][0]["id"] = json!("é".repeat(64));
    assert!(support::try_rig(rig.clone()).is_ok());
    rig["fixtures"][0]["id"] = json!("é".repeat(65));
    let error = support::try_rig(rig.clone()).unwrap_err();
    assert!(error.to_string().contains("identifiers"));

    let mut rig = simple_rig();
    rig["groups"] = json!({"x".repeat(129):["light"]});
    let error = support::try_rig(rig.clone()).unwrap_err();
    assert!(error.to_string().contains("identifiers"));
}
