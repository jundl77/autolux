use std::fs;
use std::path::Path;
use std::process::{Command, Output};

use serde_json::Value;
use starfire::script::AutoloopCatalog;

fn check(rig: &Path, autoloops: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_starfire"))
        .args(["check", "--rig"])
        .arg(rig)
        .args(["--profiles", "../rigs/profiles.json"])
        .arg("--autoloops")
        .arg(autoloops)
        .output()
        .unwrap()
}

#[test]
fn preflight_renders_every_shipped_autoloop_and_reports_patch_universes() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let output = check(&root.join("../rigs/room.json"), &root.join("../autoloops"));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["universes"], serde_json::json!([0]));
    // Counted from disk, so adding a loop never needs an edit here.
    let shipped = AutoloopCatalog::load(&root.join("../autoloops"))
        .unwrap()
        .list()
        .len();
    assert_eq!(
        report["checked_autoloops"].as_array().unwrap().len(),
        shipped
    );
}

#[test]
fn preflight_rejects_a_valid_patch_missing_a_autoloop_group() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let directory = tempfile::tempdir().unwrap();
    let mut rig: Value = serde_json::from_str(include_str!("../../rigs/room.json")).unwrap();
    rig["groups"].as_object_mut().unwrap().remove("strip");
    let path = directory.path().join("missing-strip.json");
    fs::write(&path, serde_json::to_vec(&rig).unwrap()).unwrap();

    let output = check(&path, &root.join("../autoloops"));
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(!output.status.success());
    assert!(error.contains("autoloop "), "{error}");
    assert!(error.contains("unknown fixture group strip"), "{error}");
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

    let output = check(&root.join("../rigs/room.json"), directory.path());
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(!output.status.success());
    assert!(error.contains("test/z_failure"), "{error}");
    assert!(error.contains("render callback failed"), "{error}");
}
