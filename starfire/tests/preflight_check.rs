mod support;

use std::fs;
use std::path::Path;
use std::process::{Command, Output};

use prost::Message;
use serde_json::Value;
use starfire::script::AutoloopCatalog;

fn check(rig: &Path, autoloops: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_starfire"))
        .args(["check", "--rig"])
        .arg(rig)
        .arg("--autoloops")
        .arg(autoloops)
        .arg("--palettes")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/palettes.json"))
        .output()
        .unwrap()
}

#[test]
fn preflight_renders_every_shipped_autoloop_and_reports_patch_universes() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let output = check(
        &root.join("../autolux/tests/data/room.rig.pb"),
        &root.join("../show/autoloops"),
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["universes"], serde_json::json!([0]));
    let shipped = AutoloopCatalog::load(&root.join("../show/autoloops"))
        .unwrap()
        .list()
        .len();
    assert_eq!(
        report["checked_autoloops"].as_array().unwrap().len(),
        shipped
    );
}

#[test]
fn preflight_warns_about_autoloop_groups_the_rig_lacks() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let directory = tempfile::tempdir().unwrap();
    let mut rig = support::room_definition();
    for group in &mut rig.groups {
        if group.group_id == "strip" {
            group.group_id = "pixels".into();
        }
    }
    let path = directory.path().join("missing-strip.pb");
    fs::write(&path, rig.encode_to_vec()).unwrap();

    let output = check(&path, &root.join("../show/autoloops"));
    let warning = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{warning}");
    assert!(
        warning.contains("groups not in this rig: strip"),
        "{warning}"
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["missing_groups"], serde_json::json!(["strip"]));
}

#[test]
fn preflight_executes_render_callbacks_in_every_autoloop() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let directory = tempfile::tempdir().unwrap();
    let bank = directory.path().join("test");
    fs::create_dir(&bank).unwrap();
    fs::write(
        bank.join("a_valid.lua"),
        "return { render = function(ctx) return {} end }",
    )
    .unwrap();
    fs::write(
        bank.join("z_failure.lua"),
        "return { render = function(ctx) error('render callback failed') end }",
    )
    .unwrap();

    let output = check(
        &root.join("../autolux/tests/data/room.rig.pb"),
        directory.path(),
    );
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(!output.status.success());
    assert!(error.contains("test/z_failure"), "{error}");
    assert!(error.contains("render callback failed"), "{error}");
}
