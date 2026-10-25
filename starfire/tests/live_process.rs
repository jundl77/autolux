use std::io::{Read, Write};
use std::net::UdpSocket;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use prost::Message;
use starfire::protocol::{MAX_MESSAGE_BYTES, SCHEMA_VERSION, wire};

const TEST_TIMEOUT: Duration = Duration::from_secs(5);
const ARTNET_HEADER_BYTES: usize = 18;
const RIG: &str = r#"{"schema_version":1,"fixtures":[{"id":"lamp","profile":"generic_rgb","universe":0,"address":1}],"groups":{"wash":["lamp"]}}"#;

struct Renderer {
    child: Child,
    input: Option<ChildStdin>,
    events: mpsc::Receiver<wire::RendererEvent>,
    socket: UdpSocket,
    _directory: tempfile::TempDir,
}

impl Renderer {
    fn start() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let rig = directory.path().join("rig.json");
        std::fs::write(&rig, RIG).unwrap();
        let sequences = directory.path().join("sequences");
        std::fs::create_dir_all(sequences.join("test")).unwrap();
        std::fs::write(sequences.join("test/energy.lua"),
            "return {render=function(ctx) return sf.scene {sf.colour(sf.all(), sf.rgb(1,0,0)), sf.dimmer(sf.all(), ctx.normalized_rms_level * 0.5)} end}").unwrap();
        std::fs::write(
            sequences.join("test/fault.lua"),
            "return {render=function(ctx) error('test script fault') end}",
        )
        .unwrap();
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.set_read_timeout(Some(TEST_TIMEOUT)).unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_starfire"))
            .args(["live", "--rig"])
            .arg(&rig)
            .arg("--sequences")
            .arg(sequences)
            .arg("--artnet")
            .arg(socket.local_addr().unwrap().to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let input = child.stdin.take();
        let mut output = child.stdout.take().unwrap();
        let (sender, events) = mpsc::channel();
        thread::spawn(move || {
            loop {
                let mut prefix = [0; 4];
                if output.read_exact(&mut prefix).is_err() {
                    break;
                }
                let size = u32::from_be_bytes(prefix) as usize;
                assert!(size <= MAX_MESSAGE_BYTES);
                let mut data = vec![0; size];
                output.read_exact(&mut data).unwrap();
                if sender
                    .send(wire::RendererEvent::decode(data.as_slice()).unwrap())
                    .is_err()
                {
                    break;
                }
            }
        });
        let renderer = Self {
            child,
            input,
            events,
            socket,
            _directory: directory,
        };
        let event = renderer.events.recv_timeout(TEST_TIMEOUT).unwrap();
        assert_eq!(event.schema_version, SCHEMA_VERSION);
        assert!(matches!(
            event.event,
            Some(wire::renderer_event::Event::Ready(wire::Ready {
                frame_rate_hz: 40
            }))
        ));
        renderer
    }

    fn send(&mut self, command: wire::control::Command) {
        let data = wire::Control {
            schema_version: SCHEMA_VERSION,
            command: Some(command),
        }
        .encode_to_vec();
        let input = self.input.as_mut().unwrap();
        input.write_all(&(data.len() as u32).to_be_bytes()).unwrap();
        input.write_all(&data).unwrap();
        input.flush().unwrap();
    }

    fn health(&mut self) -> wire::Health {
        self.send(wire::control::Command::Heartbeat(wire::Empty {}));
        match self
            .events
            .recv_timeout(TEST_TIMEOUT)
            .unwrap()
            .event
            .unwrap()
        {
            wire::renderer_event::Event::Health(health) => health,
            event => panic!("expected health, got {event:?}"),
        }
    }

    fn wait_for_red(&self, expected: u8) {
        let deadline = Instant::now() + TEST_TIMEOUT;
        while Instant::now() < deadline {
            let mut packet = [0; 1024];
            let size = self.socket.recv(&mut packet).unwrap();
            assert!(size >= ARTNET_HEADER_BYTES + 3);
            if packet[ARTNET_HEADER_BYTES] == expected {
                assert_eq!(
                    &packet[ARTNET_HEADER_BYTES + 1..ARTNET_HEADER_BYTES + 3],
                    &[0, 0]
                );
                return;
            }
        }
        panic!("DMX did not reach red level {expected}");
    }

    fn wait_exit(&mut self) -> std::process::ExitStatus {
        let deadline = Instant::now() + TEST_TIMEOUT;
        while Instant::now() < deadline {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            thread::sleep(Duration::from_millis(5));
        }
        panic!("renderer did not stop promptly");
    }
}

impl Drop for Renderer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn typed_controls_drive_artnet_stop_keeps_process_and_eof_blacks_out() {
    let mut renderer = Renderer::start();
    renderer.send(wire::control::Command::Select(wire::Select {
        bank: "test".into(),
        sequence: "energy".into(),
        transition: None,
    }));
    renderer.send(wire::control::Command::SetNormalizedRmsLevel(
        wire::SetNormalizedRmsLevel { level: 0.8 },
    ));
    let first = renderer.health();
    assert_eq!(first.active_sequence, "energy");
    renderer.wait_for_red(102);
    renderer.send(wire::control::Command::Stop(wire::Empty {}));
    let stopped = renderer.health();
    assert!(stopped.active_sequence.is_empty());
    assert!(stopped.rendered_frames >= first.rendered_frames);
    renderer.wait_for_red(0);
    assert!(renderer.child.try_wait().unwrap().is_none());
    renderer.send(wire::control::Command::Select(wire::Select {
        bank: "test".into(),
        sequence: "energy".into(),
        transition: None,
    }));
    renderer.health();
    renderer.wait_for_red(102);
    renderer.input.take();
    assert!(renderer.wait_exit().success());
    renderer.wait_for_red(0);
}

#[test]
fn oversized_pipe_message_fails_closed_and_emits_blackout() {
    let mut renderer = Renderer::start();
    renderer.send(wire::control::Command::Select(wire::Select {
        bank: "test".into(),
        sequence: "energy".into(),
        transition: None,
    }));
    renderer.health();
    renderer.wait_for_red(128);
    renderer
        .input
        .as_mut()
        .unwrap()
        .write_all(&((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes())
        .unwrap();
    assert!(!renderer.wait_exit().success());
    renderer.wait_for_red(0);
}

#[test]
fn script_fault_blacks_out_and_reports_immediately_without_waiting_for_heartbeat() {
    let mut renderer = Renderer::start();
    renderer.send(wire::control::Command::Select(wire::Select {
        bank: "test".into(),
        sequence: "fault".into(),
        transition: None,
    }));
    let event = renderer
        .events
        .recv_timeout(Duration::from_secs(1))
        .unwrap();
    let Some(wire::renderer_event::Event::Health(health)) = event.event else {
        panic!("script fault must publish health immediately");
    };
    assert!(health.script_error.contains("test script fault"));
    renderer.wait_for_red(0);
    assert!(renderer.child.try_wait().unwrap().is_none());
    renderer.send(wire::control::Command::Select(wire::Select {
        bank: "test".into(),
        sequence: "energy".into(),
        transition: None,
    }));
    let event = renderer
        .events
        .recv_timeout(Duration::from_secs(1))
        .unwrap();
    let Some(wire::renderer_event::Event::Health(health)) = event.event else {
        panic!("recovery must publish health immediately");
    };
    assert!(health.script_error.is_empty());
    renderer.wait_for_red(128);
}
