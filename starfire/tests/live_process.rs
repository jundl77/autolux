use std::io::{Read, Write};
use std::net::UdpSocket;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use prost::Message;
use starfire::protocol::{MAX_MESSAGE_BYTES, wire};
use wire::renderer_command::Command as Operation;
use wire::renderer_event::Event;

const TEST_TIMEOUT: Duration = Duration::from_secs(5);
const SILENT_CHECK_TIMEOUT: Duration = Duration::from_millis(80);
const ARTNET_HEADER_BYTES: usize = 18;
const RIG: &str = r#"{"schema_version":1,"fixtures":[{"id":"lamp","profile":"generic_rgb","universe":0,"address":1}],"groups":{"wash":["lamp"]}}"#;

struct Renderer {
    child: Child,
    input: Option<ChildStdin>,
    events: mpsc::Receiver<wire::RendererEvent>,
    socket: UdpSocket,
    login: wire::Login,
    command_id: u64,
    _directory: tempfile::TempDir,
}

fn play(name: &str) -> Operation {
    Operation::PlaySequence(wire::PlaySequence {
        sequence: Some(wire::SequenceReference {
            bank_id: "test".into(),
            sequence_id: name.into(),
        }),
        transition: None,
    })
}

fn parameter(value: wire::set_parameter::Parameter) -> Operation {
    Operation::SetParameter(wire::SetParameter {
        parameter: Some(value),
    })
}

impl Renderer {
    fn start() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let rig = directory.path().join("rig.json");
        std::fs::write(&rig, RIG).unwrap();
        let sequences = directory.path().join("sequences");
        std::fs::create_dir_all(sequences.join("test")).unwrap();
        std::fs::write(sequences.join("test/energy.lua"),
            "return {render=function(ctx) return sf.scene {sf.colour(sf.all(), sf.rgb(1,0,0)), sf.dimmer(sf.all(), (ctx.normalized_rms_level or 1) * 0.5)} end}").unwrap();
        std::fs::write(
            sequences.join("test/fault.lua"),
            "return {render=function(ctx) error('test script fault') end}",
        )
        .unwrap();
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.set_read_timeout(Some(TEST_TIMEOUT)).unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_starfire"))
            .arg("live")
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
        let login = wire::Login {
            rig_path: rig.to_str().unwrap().into(),
            sequences_path: sequences.to_str().unwrap().into(),
            artnet_destination: socket.local_addr().unwrap().to_string(),
            broadcast: false,
            parameters: vec![],
        };
        Self {
            child,
            input,
            events,
            socket,
            login,
            command_id: 0,
            _directory: directory,
        }
    }

    fn logged_in() -> Self {
        let mut renderer = Self::start();
        assert!(
            matches!(renderer.reply(Operation::Login(renderer.login.clone())), Event::LoginReply(wire::LoginReply { error, frame_rate_hz: 40, max_message_bytes: 65536 }) if error.is_empty())
        );
        renderer
    }

    fn send(&mut self, command: Operation) -> u64 {
        self.command_id += 1;
        self.send_with_id(self.command_id, command);
        self.command_id
    }

    fn send_with_id(&mut self, command_id: u64, command: Operation) {
        let data = wire::RendererCommand {
            command_id,
            command: Some(command),
        }
        .encode_to_vec();
        let input = self.input.as_mut().unwrap();
        input.write_all(&(data.len() as u32).to_be_bytes()).unwrap();
        input.write_all(&data).unwrap();
        input.flush().unwrap();
    }

    fn receive_reply(&self, id: u64) -> Event {
        let deadline = Instant::now() + TEST_TIMEOUT;
        loop {
            let event = self
                .events
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .unwrap();
            if event.command_id == 0 {
                continue;
            }
            assert_eq!(
                event.command_id, id,
                "replies must follow accepted command order"
            );
            return event.event.unwrap();
        }
    }

    fn reply(&mut self, command: Operation) -> Event {
        let id = self.send(command);
        self.receive_reply(id)
    }

    fn next_metrics(&self) -> wire::Metrics {
        let deadline = Instant::now() + TEST_TIMEOUT;
        loop {
            let event = self
                .events
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .unwrap();
            if let Some(Event::Metrics(metrics)) = event.event {
                assert_eq!(event.command_id, 0);
                return metrics;
            }
        }
    }

    fn metrics(&mut self) -> wire::Metrics {
        assert!(matches!(
            self.reply(Operation::Heartbeat(wire::Heartbeat {})),
            Event::Heartbeat(_)
        ));
        self.next_metrics()
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

    fn assert_no_output(&self) {
        self.socket
            .set_read_timeout(Some(SILENT_CHECK_TIMEOUT))
            .unwrap();
        assert!(
            self.socket.recv(&mut [0; 1024]).is_err(),
            "output must remain unacquired before successful login"
        );
        self.socket.set_read_timeout(Some(TEST_TIMEOUT)).unwrap();
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
fn login_is_required_and_duplicate_login_preserves_owned_scene() {
    let mut renderer = Renderer::start();
    assert!(renderer.events.recv_timeout(SILENT_CHECK_TIMEOUT).is_err());
    renderer.assert_no_output();
    assert!(
        matches!(renderer.reply(play("energy")), Event::PlaySequenceReply(value) if value.error.contains("login"))
    );
    renderer.assert_no_output();
    assert!(
        matches!(renderer.reply(Operation::Login(renderer.login.clone())), Event::LoginReply(value) if value.error.is_empty())
    );
    assert!(
        matches!(renderer.reply(play("energy")), Event::PlaySequenceReply(value) if value.error.is_empty())
    );
    renderer.wait_for_red(128);
    let mut replacement = renderer.login.clone();
    replacement.parameters.push(wire::SetParameter {
        parameter: Some(wire::set_parameter::Parameter::OutputBlackout(true)),
    });
    assert!(
        matches!(renderer.reply(Operation::Login(replacement)), Event::LoginReply(value) if value.error.contains("already"))
    );
    assert!(renderer.metrics().runtime_error.is_empty());
    renderer.wait_for_red(128);
}

#[test]
fn failed_login_acquires_no_output_and_corrected_login_initializes_parameters() {
    let mut renderer = Renderer::start();
    let mut missing_rig = renderer.login.clone();
    missing_rig.rig_path.push_str(".missing");
    let mut missing_sequences = renderer.login.clone();
    missing_sequences.sequences_path.push_str(".missing");
    let mut invalid_output = renderer.login.clone();
    invalid_output.artnet_destination = "0.0.0.0:0".into();
    let mut invalid_parameter = renderer.login.clone();
    invalid_parameter.parameters = vec![
        wire::SetParameter {
            parameter: Some(wire::set_parameter::Parameter::OutputBrightness(0.1)),
        },
        wire::SetParameter {
            parameter: Some(wire::set_parameter::Parameter::AnimationSpeed(-1.0)),
        },
    ];
    for login in [
        missing_rig,
        missing_sequences,
        invalid_output,
        invalid_parameter,
    ] {
        assert!(
            matches!(renderer.reply(Operation::Login(login)), Event::LoginReply(value) if !value.error.is_empty())
        );
        renderer.assert_no_output();
        assert!(renderer.child.try_wait().unwrap().is_none());
    }
    renderer.login.parameters.push(wire::SetParameter {
        parameter: Some(wire::set_parameter::Parameter::OutputBrightness(0.6)),
    });
    assert!(
        matches!(renderer.reply(Operation::Login(renderer.login.clone())), Event::LoginReply(value) if value.error.is_empty())
    );
    assert!(
        matches!(renderer.reply(play("energy")), Event::PlaySequenceReply(value) if value.error.is_empty())
    );
    renderer.wait_for_red(77);
    let metrics = renderer.metrics();
    assert!(
        metrics.elapsed_seconds < 1.0,
        "initialization delay must not enter the session clock"
    );
    assert_eq!(metrics.missed_render_deadlines, 0);
}

#[test]
fn typed_controls_drive_artnet_idle_retains_settings_and_eof_blacks_out() {
    let mut renderer = Renderer::logged_in();
    assert!(
        matches!(renderer.reply(play("energy")), Event::PlaySequenceReply(value) if value.error.is_empty())
    );
    assert!(
        matches!(renderer.reply(parameter(wire::set_parameter::Parameter::AudioRmsNormalized(0.8))), Event::SetParameterReply(value) if value.error.is_empty())
    );
    let first = renderer.metrics();
    assert_eq!(first.active_sequence.unwrap().sequence_id, "energy");
    renderer.wait_for_red(102);
    assert!(
        matches!(renderer.reply(Operation::PlaySequence(wire::PlaySequence::default())), Event::PlaySequenceReply(value) if value.error.is_empty() && value.sequence.is_none())
    );
    let idle = renderer.metrics();
    assert!(idle.active_sequence.is_none());
    assert!(idle.rendered_frames >= first.rendered_frames);
    renderer.wait_for_red(0);
    assert!(renderer.child.try_wait().unwrap().is_none());
    renderer.reply(play("energy"));
    renderer.metrics();
    renderer.wait_for_red(102);
    renderer.input.take();
    assert!(renderer.wait_exit().success());
    renderer.wait_for_red(0);
}

#[test]
fn semantic_rejections_preserve_scene_and_replies_follow_large_bursts() {
    let mut renderer = Renderer::logged_in();
    renderer.reply(play("energy"));
    renderer.wait_for_red(128);
    assert!(
        matches!(renderer.reply(play("missing")), Event::PlaySequenceReply(value) if !value.error.is_empty() && value.sequence.as_ref().unwrap().sequence_id == "energy")
    );
    assert!(
        matches!(renderer.reply(parameter(wire::set_parameter::Parameter::OutputBrightness(-1.0))), Event::SetParameterReply(value) if !value.error.is_empty())
    );
    renderer.wait_for_red(128);
    let first_id = renderer.command_id + 1;
    const BURST_COMMANDS: u64 = 128;
    for _ in 0..BURST_COMMANDS {
        renderer.send(parameter(wire::set_parameter::Parameter::OutputBrightness(
            0.8,
        )));
    }
    for id in first_id..first_id + BURST_COMMANDS {
        assert!(
            matches!(renderer.receive_reply(id), Event::SetParameterReply(value) if value.error.is_empty())
        );
    }
    renderer.wait_for_red(102);
    assert!(renderer.metrics().runtime_error.is_empty());
}

#[test]
fn malformed_frames_and_reused_command_ids_fail_closed() {
    for reuse_id in [false, true] {
        let mut renderer = Renderer::logged_in();
        renderer.reply(play("energy"));
        renderer.metrics();
        renderer.wait_for_red(128);
        if reuse_id {
            renderer.send_with_id(
                renderer.command_id,
                parameter(wire::set_parameter::Parameter::OutputBrightness(0.9)),
            );
        } else {
            renderer
                .input
                .as_mut()
                .unwrap()
                .write_all(&((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes())
                .unwrap();
        }
        assert!(!renderer.wait_exit().success());
        renderer.wait_for_red(0);
    }
}

#[test]
fn script_fault_and_recovery_publish_metrics_without_waiting_for_heartbeat() {
    let mut renderer = Renderer::logged_in();
    renderer.reply(play("fault"));
    assert!(
        renderer
            .next_metrics()
            .script_error
            .contains("test script fault")
    );
    renderer.wait_for_red(0);
    assert!(renderer.child.try_wait().unwrap().is_none());
    renderer.reply(play("energy"));
    assert!(renderer.next_metrics().script_error.is_empty());
    renderer.wait_for_red(128);
}

#[test]
fn server_sends_ten_second_heartbeats_without_waiting_for_requests() {
    let mut renderer = Renderer::logged_in();
    let started = Instant::now();
    let event = renderer
        .events
        .recv_timeout(Duration::from_secs(12))
        .unwrap();
    assert_eq!(event.command_id, 0);
    assert!(matches!(event.event, Some(Event::Heartbeat(_))));
    assert!(started.elapsed() >= Duration::from_secs(9));
    assert!(renderer.next_metrics().rendered_frames >= 360);
    assert!(renderer.child.try_wait().unwrap().is_none());
}
