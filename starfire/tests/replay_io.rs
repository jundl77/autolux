use std::fs;
use std::net::UdpSocket;
use std::path::Path;
use std::process::{Command as Process, Stdio};
use std::time::Duration;

use starfire::fixtures::FixtureRig;
use starfire::model::{Command, TimedCommand, Transition};
use starfire::replay::{Replay, dmx_digest, render_replay, write_capture};
use starfire::script::AutoloopCatalog;

fn replay(duration_seconds: f64) -> Replay {
    Replay {
        schema_version: 1,
        duration_seconds,
        frame_rate_hz: 40,
        events: vec![
            TimedCommand {
                at_seconds: 0.0,
                command: Command::Beat {
                    position_beats: 0.0,
                    bpm: 120.0,
                },
            },
            TimedCommand {
                at_seconds: 0.0,
                command: Command::Select {
                    bank: "atmospheric".into(),
                    autoloop: "cold_breathe".into(),
                    transition: Transition::Cut,
                },
            },
        ],
    }
}

fn render(input: &Replay) -> starfire::replay::ReplayResult {
    render_replay(
        input,
        FixtureRig::from_mvr(include_bytes!("../../rigs/room.mvr")).unwrap(),
        AutoloopCatalog::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloops")).unwrap(),
    )
    .unwrap()
}

#[test]
fn final_fractional_frame_applies_pending_commands_and_capture_is_identical() {
    let mut input = replay(0.033);
    input.events.push(TimedCommand {
        at_seconds: 0.03,
        command: Command::Stop,
    });
    let first = render(&input);
    assert_eq!(first.frames.len(), 3);
    assert_eq!(first.frames[2].time_seconds, 0.033);
    assert!(
        first.frames[0]
            .fixture_properties_before_strobe
            .values()
            .any(|value| value.intensity > 0.0)
    );
    assert!(
        first.frames[2]
            .fixture_properties_before_strobe
            .values()
            .all(|value| value.intensity == 0.0)
    );
    let second = render(&input);
    assert_eq!(dmx_digest(&first.frames), dmx_digest(&second.frames));
    let directory = tempfile::tempdir().unwrap();
    let one = directory.path().join("one.dmx");
    let two = directory.path().join("two.dmx");
    write_capture(&one, &first.frames).unwrap();
    write_capture(&two, &second.frames).unwrap();
    let bytes = fs::read(&one).unwrap();
    assert_eq!(bytes, fs::read(two).unwrap());
    assert_eq!(&bytes[..8], b"STARFIRE");
    assert_eq!(u32::from_le_bytes(bytes[8..12].try_into().unwrap()), 1);
    assert_eq!(u64::from_le_bytes(bytes[12..20].try_into().unwrap()), 3);
    assert_eq!(bytes.len(), 20 + 3 * (8 + 8 + 2 + 2 + 512));
    assert_eq!(&bytes[40..552], &first.frames[0].dmx_frames_by_universe[&0]);
}

#[test]
fn replay_rejects_backwards_out_of_bounds_and_unbounded_input() {
    let mut input = replay(1.0);
    input.events.push(TimedCommand {
        at_seconds: -0.1,
        command: Command::Stop,
    });
    assert!(input.validate().is_err());
    input.events.last_mut().unwrap().at_seconds = 1.1;
    assert!(input.validate().is_err());
    input.events.pop();
    input.duration_seconds = f64::INFINITY;
    assert!(input.validate().is_err());
    input.duration_seconds = 100_000.0;
    assert!(input.validate().is_err());
    input.duration_seconds = 1.0;
    input.frame_rate_hz = 0;
    assert!(input.validate().is_err());
    input.frame_rate_hz = 40;
    input.events.push(TimedCommand {
        at_seconds: 0.0,
        command: Command::SetBlackout { enabled: true },
    });
    assert!(
        render(&input).frames[0]
            .fixture_properties_before_strobe
            .values()
            .all(|state| state.intensity == 0.0)
    );
}

#[test]
fn replay_memory_budget_is_checked_before_rendering() {
    let mut input = replay(2_000.0);
    input.frame_rate_hz = 50;
    input.duration_seconds -= 0.02;
    assert!(input.validate().is_ok());
    let rig = FixtureRig::from_mvr(include_bytes!("../../rigs/room.mvr")).unwrap();
    let catalogue =
        AutoloopCatalog::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloops")).unwrap();
    assert!(
        render_replay(&input, rig, catalogue)
            .err()
            .unwrap()
            .to_string()
            .contains("memory budget")
    );
}

fn cli(input: &Path) -> Process {
    let mut command = Process::new(env!("CARGO_BIN_EXE_starfire"));
    command
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .args(["replay"])
        .arg(input)
        .args(["--rig", "../rigs/room.mvr", "--autoloops", "../autoloops"]);
    command
}

#[test]
fn native_cli_emits_paced_artnet_then_blackout_without_moving_fixtures() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("replay.json");
    fs::write(&input, serde_json::to_vec(&replay(0.1)).unwrap()).unwrap();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut child = cli(&input)
        .arg("--artnet")
        .arg(receiver.local_addr().unwrap().to_string())
        .arg("--realtime")
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let mut packets = Vec::new();
    loop {
        let mut packet = [0; 530];
        let count = receiver.recv(&mut packet).unwrap();
        assert_eq!(count, packet.len());
        let is_dark = [7, 19, 68, 76, 84, 92]
            .iter()
            .all(|offset| packet[18 + offset] == 0);
        packets.push(packet);
        if is_dark {
            break;
        }
    }
    assert!(child.wait().unwrap().success());
    assert!(packets.len() >= 2);
    let last = packets.last().unwrap();
    let before = &packets[packets.len() - 2];
    assert_eq!(&last[18..22], &before[18..22]);
    assert_eq!(&last[30..34], &before[30..34]);
    assert_eq!(&last[..8], b"Art-Net\0");
}

#[test]
fn native_cli_rejects_unpaced_network_output_without_sending() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("replay.json");
    fs::write(&input, serde_json::to_vec(&replay(0.0)).unwrap()).unwrap();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_millis(50)))
        .unwrap();
    let output = cli(&input)
        .arg("--artnet")
        .arg(receiver.local_addr().unwrap().to_string())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("requires --realtime"));
    assert!(receiver.recv(&mut [0; 1024]).is_err());
}

#[test]
fn native_cli_rejects_output_collisions_before_overwriting_inputs() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("replay.json");
    let rig = directory.path().join("rig.mvr");
    let shared = directory.path().join("capture");
    let source = serde_json::to_vec(&replay(0.0)).unwrap();
    let patch = include_bytes!("../../rigs/room.mvr");
    fs::write(&input, &source).unwrap();
    fs::write(&rig, patch).unwrap();
    for destination in [&input, &rig, &shared] {
        let mut command = Process::new(env!("CARGO_BIN_EXE_starfire"));
        command
            .current_dir(env!("CARGO_MANIFEST_DIR"))
            .arg("replay")
            .arg(&input)
            .arg("--rig")
            .arg(&rig)
            .args(["--autoloops", "../autoloops", "--output"])
            .arg(destination);
        if destination == &shared {
            command.arg("--summary").arg(&shared);
        }
        let output = command.output().unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("must not overwrite"));
        assert_eq!(fs::read(&input).unwrap(), source);
        assert_eq!(fs::read(&rig).unwrap(), patch);
        assert!(!shared.exists());
    }
}
