use std::io::Write;
use std::net::UdpSocket;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use starfire::protocol::ClientMessage as Operation;
use starfire::protocol::ServerMessage as Event;
use starfire::protocol::{MAX_MESSAGE_BYTES, read_event, wire};

const TEST_TIMEOUT: Duration = Duration::from_secs(5);
const SILENT_CHECK_TIMEOUT: Duration = Duration::from_millis(80);
const ARTNET_HEADER_BYTES: usize = 18;

struct Renderer {
    child: Child,
    input: Option<ChildStdin>,
    events: mpsc::Receiver<Event>,
    socket: UdpSocket,
    login: wire::Login,
    command_id: u64,
}

fn play(name: &str) -> Operation {
    Operation::PlaySequence(wire::PlaySequence {
        command_id: 1,
        sequence_id: name.into(),
        transition: None,
    })
}

fn parameter(value: wire::Parameter) -> Operation {
    Operation::SetParameter(wire::SetParameter {
        command_id: 1,
        parameter: Some(value),
    })
}

fn setting(kind: wire::ParameterType, value: wire::parameter::Value) -> wire::Parameter {
    wire::Parameter {
        r#type: kind as i32,
        value: Some(value),
    }
}

impl Renderer {
    fn start() -> Self {
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
            while let Some(event) = read_event(&mut output).unwrap() {
                if sender.send(event).is_err() {
                    break;
                }
            }
        });
        let login = wire::Login {
            command_id: 0,
            rig: Some(wire::RigDefinition {
                fixtures: vec![wire::Fixture {
                    fixture_id: "lamp".into(), profile_id: "rgb".into(), address: 1, ..Default::default()
                }],
                groups: vec![wire::FixtureGroup { group_id: "wash".into(), fixture_ids: vec!["lamp".into()] }],
                profiles: vec![wire::FixtureProfile {
                    profile_id: "rgb".into(), source: "test".into(), notes: "RGB fixture".into(),
                    safe_values: vec![0;3], rgb: vec![0,1,2], ..Default::default()
                }],
            }),
            scripts: vec![
                wire::Script { sequence_id: "energy".into(), bank: "test".into(), lua_source: "return {render=function(ctx) return sf.scene {sf.colour(sf.all(), sf.rgb(1,0,0)), sf.dimmer(sf.all(), (ctx.normalized_rms_level or 1) * 0.5)} end}".into() },
                wire::Script { sequence_id: "fault".into(), bank: "test".into(), lua_source: "return {render=function(ctx) if ctx.time_seconds > 0 then error('test script fault') end return sf.scene {} end}".into() },
            ],
            artnet_destination: socket.local_addr().unwrap().to_string(),
            parameters: vec![],
        };
        Self {
            child,
            input,
            events,
            socket,
            login,
            command_id: 0,
        }
    }

    fn logged_in() -> Self {
        let mut renderer = Self::start();
        assert!(matches!(
            renderer.reply(Operation::Login(renderer.login.clone())),
            Event::LoginReply(wire::LoginReply {
                frame_rate_hz: 40,
                max_message_bytes: 65536,
                ..
            })
        ));
        assert_eq!(
            renderer.next_metrics().last_processed_command_id,
            renderer.command_id
        );
        renderer
    }

    fn send(&mut self, command: Operation) -> u64 {
        if command.command_id().is_some() {
            self.command_id += 1;
        }
        self.send_with_id(self.command_id, command);
        self.command_id
    }

    fn send_with_id(&mut self, command_id: u64, mut command: Operation) {
        command.set_command_id(command_id);
        command.write(self.input.as_mut().unwrap()).unwrap();
    }
    fn receive_reply(&self, id: u64) -> Event {
        let deadline = Instant::now() + TEST_TIMEOUT;
        loop {
            let event = self
                .events
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .unwrap();
            if event.command_id().unwrap_or(0) == 0 {
                continue;
            }
            assert_eq!(
                event.command_id(),
                Some(id),
                "replies must follow accepted command order"
            );
            return event;
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
            if let Event::Metrics(metrics) = event
                && metrics.last_processed_command_id >= self.command_id
            {
                return metrics;
            }
        }
    }

    fn metrics(&mut self) -> wire::Metrics {
        self.send(Operation::Heartbeat(wire::Heartbeat {}));
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
        matches!(renderer.reply(play("energy")), Event::ErrorReply(value) if value.message.contains("login"))
    );
    renderer.assert_no_output();
    assert!(matches!(
        renderer.reply(Operation::Login(renderer.login.clone())),
        Event::LoginReply(_)
    ));
    renderer.send(play("energy"));
    renderer.wait_for_red(128);
    let mut replacement = renderer.login.clone();
    replacement.parameters.push(setting(
        wire::ParameterType::OutputBlackout,
        wire::parameter::Value::Boolean(true),
    ));
    assert!(
        matches!(renderer.reply(Operation::Login(replacement)), Event::ErrorReply(value) if value.message.contains("already"))
    );
    assert!(renderer.metrics().runtime_error.is_empty());
    renderer.wait_for_red(128);
}

#[test]
fn failed_login_acquires_no_output_and_corrected_login_initializes_parameters() {
    let mut renderer = Renderer::start();
    let mut missing_rig = renderer.login.clone();
    missing_rig.rig = None;
    let mut missing_sequences = renderer.login.clone();
    missing_sequences.scripts.clear();
    let mut invalid_output = renderer.login.clone();
    invalid_output.artnet_destination = "0.0.0.0:0".into();
    let mut invalid_parameter = renderer.login.clone();
    invalid_parameter.parameters = vec![
        setting(
            wire::ParameterType::OutputBrightness,
            wire::parameter::Value::Number(0.1),
        ),
        setting(
            wire::ParameterType::AnimationSpeed,
            wire::parameter::Value::Number(-1.0),
        ),
    ];
    for login in [
        missing_rig,
        missing_sequences,
        invalid_output,
        invalid_parameter,
    ] {
        assert!(
            matches!(renderer.reply(Operation::Login(login)), Event::ErrorReply(value) if !value.message.is_empty())
        );
        renderer.assert_no_output();
        assert!(renderer.child.try_wait().unwrap().is_none());
    }
    renderer.login.parameters.push(setting(
        wire::ParameterType::OutputBrightness,
        wire::parameter::Value::Number(0.6),
    ));
    assert!(matches!(
        renderer.reply(Operation::Login(renderer.login.clone())),
        Event::LoginReply(_)
    ));
    renderer.send(play("energy"));
    renderer.wait_for_red(77);
    let metrics = renderer.metrics();
    assert!(
        metrics.elapsed_seconds < 1.0,
        "initialization delay must not enter the session clock"
    );
    assert_eq!(metrics.missed_render_deadlines, 0);
}

#[test]
fn login_probes_every_script_without_output_and_uses_initial_parameters() {
    let mut renderer = Renderer::start();
    for source in [
        "this is not Lua",
        "return {render=function(ctx) return sf.scene {sf.dimmer(sf.group('missing'), 1)} end}",
        "return {render=function(ctx) error('invalid supplied script') end}",
    ] {
        let mut login = renderer.login.clone();
        login.scripts[0].lua_source = source.into();
        assert!(matches!(
            renderer.reply(Operation::Login(login)),
            Event::ErrorReply(_)
        ));
        renderer.assert_no_output();
    }
    renderer.login.scripts[0].lua_source = "return {render=function(ctx) return sf.scene {sf.colour(sf.all(), sf.rgb(1,0,0)), sf.dimmer(sf.all(), ctx.normalized_rms_level * 0.5)} end}".into();
    renderer.login.parameters.push(setting(
        wire::ParameterType::AudioRmsNormalized,
        wire::parameter::Value::Number(0.8),
    ));
    assert!(matches!(
        renderer.reply(Operation::Login(renderer.login.clone())),
        Event::LoginReply(_)
    ));
    renderer.send(play("energy"));
    renderer.wait_for_red(102);
}

#[test]
fn typed_controls_drive_artnet_idle_retains_settings_and_eof_blacks_out() {
    let mut renderer = Renderer::logged_in();
    renderer.send(play("energy"));
    renderer.send(parameter(setting(
        wire::ParameterType::AudioRmsNormalized,
        wire::parameter::Value::Number(0.8),
    )));
    let first = renderer.metrics();
    assert_eq!(first.active_sequence, "energy");
    renderer.wait_for_red(102);
    renderer.send(Operation::PlaySequence(wire::PlaySequence::default()));
    let idle = renderer.metrics();
    assert!(idle.active_sequence.is_empty());
    assert!(idle.rendered_frames >= first.rendered_frames);
    renderer.wait_for_red(0);
    assert!(renderer.child.try_wait().unwrap().is_none());
    renderer.send(play("energy"));
    renderer.metrics();
    renderer.wait_for_red(102);
    renderer.input.take();
    assert!(renderer.wait_exit().success());
    renderer.wait_for_red(0);
}

#[test]
fn semantic_rejections_preserve_scene_and_metrics_track_silent_command_bursts() {
    let mut renderer = Renderer::logged_in();
    renderer.send(play("energy"));
    renderer.wait_for_red(128);
    assert!(
        matches!(renderer.reply(play("missing")), Event::ErrorReply(value) if !value.message.is_empty())
    );
    assert!(
        matches!(renderer.reply(parameter(setting(wire::ParameterType::OutputBrightness, wire::parameter::Value::Number(-1.0)))), Event::ErrorReply(value) if !value.message.is_empty())
    );
    let rejected_id = renderer.command_id;
    assert_eq!(renderer.metrics().last_processed_command_id, rejected_id);
    renderer.wait_for_red(128);
    const BURST_COMMANDS: u64 = 128;
    for _ in 0..BURST_COMMANDS {
        renderer.send(parameter(setting(
            wire::ParameterType::OutputBrightness,
            wire::parameter::Value::Number(0.8),
        )));
    }
    let metrics = renderer.metrics();
    assert_eq!(metrics.last_processed_command_id, renderer.command_id);
    assert!(
        renderer.events.recv_timeout(SILENT_CHECK_TIMEOUT).is_err(),
        "successful commands must not produce replies"
    );
    renderer.wait_for_red(102);
    assert!(renderer.metrics().runtime_error.is_empty());
}

#[test]
fn malformed_frames_and_reused_command_ids_fail_closed() {
    for reuse_id in [false, true] {
        let mut renderer = Renderer::logged_in();
        renderer.send(play("energy"));
        renderer.metrics();
        renderer.wait_for_red(128);
        if reuse_id {
            renderer.send_with_id(
                renderer.command_id,
                parameter(setting(
                    wire::ParameterType::OutputBrightness,
                    wire::parameter::Value::Number(0.9),
                )),
            );
        } else {
            let mut header = vec![wire::MessageType::Heartbeat as u8];
            header.extend(((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes());
            renderer.input.as_mut().unwrap().write_all(&header).unwrap();
        }
        assert!(!renderer.wait_exit().success());
        renderer.wait_for_red(0);
    }
}

#[test]
fn script_fault_and_recovery_publish_metrics_without_waiting_for_heartbeat() {
    let mut renderer = Renderer::logged_in();
    renderer.send(play("fault"));
    assert!(
        renderer
            .next_metrics()
            .script_error
            .contains("test script fault")
    );
    renderer.wait_for_red(0);
    assert!(renderer.child.try_wait().unwrap().is_none());
    renderer.send(play("energy"));
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
    assert_eq!(event.command_id(), None);
    assert!(matches!(event, Event::Heartbeat(_)));
    assert!(started.elapsed() >= Duration::from_secs(9));
    assert!(renderer.next_metrics().rendered_frames >= 360);
    assert!(renderer.child.try_wait().unwrap().is_none());
}
