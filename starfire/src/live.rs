//! Owned rendering sessions and paced process I/O. Login acquires output resources.

use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};

use crate::artnet::ArtNetOutput;
use crate::engine::Engine;
use crate::fixtures::FixtureRig;
use crate::model::{DEFAULT_FRAME_RATE_HZ, FixtureStates};
use crate::protocol::{
    MAX_MESSAGE_BYTES, apply_command, error_message, read_control, wire, write_event,
};
use crate::script::SequenceCatalog;

const MAX_QUEUED_CONTROLS: usize = 1024;
const MAX_CONTROLS_PER_FRAME: usize = 32;
// Each queued command can produce its reply plus a metrics event.
const MAX_QUEUED_EVENTS: usize = 2 * MAX_QUEUED_CONTROLS + 4;
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_POLL_INTERVAL: Duration = Duration::from_millis(10);
const NANOSECONDS_PER_SECOND: u128 = 1_000_000_000;

type SharedFailure = Arc<Mutex<Option<String>>>;

struct LiveSession {
    rig: FixtureRig,
    engine: Engine,
    output: Option<ArtNetOutput>,
    last_states: FixtureStates,
    started: Instant,
}

impl LiveSession {
    fn login(login: &wire::Login) -> Result<Self> {
        ensure!(
            !login.rig_path.is_empty() && !login.sequences_path.is_empty(),
            "login requires rig and sequence paths"
        );
        let destination: SocketAddrV4 = login
            .artnet_destination
            .parse()
            .context("invalid Art-Net IPv4 destination")?;
        let rig = FixtureRig::load(Path::new(&login.rig_path))?;
        let catalog = SequenceCatalog::load(Path::new(&login.sequences_path))?;
        let mut engine = Engine::new(rig.clone(), catalog)?;
        for parameter in &login.parameters {
            engine.set_parameter(0.0, parameter)?;
        }
        let local = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0);
        let output = if login.broadcast {
            ArtNetOutput::bind_broadcast(local, destination)?
        } else {
            ArtNetOutput::bind(local, destination)?
        };
        let last_states = rig.empty_states();
        Ok(Self {
            rig,
            engine,
            output: Some(output),
            last_states,
            started: Instant::now(),
        })
    }

    fn render(
        &mut self,
        at_seconds: f64,
        rendered_frames: u64,
        missed_render_deadlines: u64,
        queued_commands: usize,
    ) -> Result<wire::Metrics> {
        let started = Instant::now();
        let frame = self.engine.render_at(at_seconds)?;
        let last_render_duration_seconds = started.elapsed().as_secs_f64();
        self.last_states
            .clone_from(&frame.fixture_properties_before_strobe);
        self.output
            .as_mut()
            .context("rendering session is closed")?
            .send_frame(&frame.dmx_frames_by_universe)?;
        Ok(wire::Metrics {
            elapsed_seconds: frame.time_seconds,
            rendered_frames,
            missed_render_deadlines,
            queued_commands: queued_commands as u32,
            last_render_duration_seconds,
            active_sequence: frame.active_bank.zip(frame.active_sequence).map(
                |(bank_id, sequence_id)| wire::SequenceReference {
                    bank_id,
                    sequence_id,
                },
            ),
            script_error: frame.script_error.unwrap_or_default(),
            runtime_error: String::new(),
        })
    }

    fn close(&mut self) -> Result<()> {
        let Some(mut output) = self.output.take() else {
            return Ok(());
        };
        for state in self.last_states.values_mut() {
            state.intensity = 0.0;
            state.strobe_hz = 0.0;
        }
        output
            .send_frame(&self.rig.encode(&self.last_states)?)
            .context("send final blackout")
    }
}

impl Drop for LiveSession {
    fn drop(&mut self) {
        // Ordinary shutdown reports errors; unwinding still releases output safely.
        let _ = self.close();
    }
}

fn record_failure(failure: &SharedFailure, message: String) {
    let mut current = failure.lock().expect("IPC failure lock poisoned");
    if current.is_none() {
        *current = Some(message);
    }
}

fn check_failure(failure: &SharedFailure) -> Result<()> {
    if let Some(message) = failure.lock().expect("IPC failure lock poisoned").take() {
        bail!(message);
    }
    Ok(())
}

fn receive_controls(
    mut input: impl Read,
    sender: mpsc::SyncSender<wire::RendererCommand>,
    queued: Arc<AtomicUsize>,
    failure: SharedFailure,
) {
    let mut last_command_id = 0;
    loop {
        match read_control(&mut input) {
            Ok(Some(control)) => {
                if control.command_id <= last_command_id {
                    record_failure(
                        &failure,
                        "command IDs must increase within the renderer session".into(),
                    );
                    return;
                }
                last_command_id = control.command_id;
                queued.fetch_add(1, Ordering::Relaxed);
                if let Err(error) = sender.try_send(control) {
                    queued.fetch_sub(1, Ordering::Relaxed);
                    if matches!(error, mpsc::TrySendError::Full(_)) {
                        record_failure(&failure, "live control queue overflow".into());
                    }
                    return;
                }
            }
            Ok(None) => return,
            Err(error) => {
                record_failure(&failure, format!("live control failed: {error:#}"));
                return;
            }
        }
    }
}

fn send_event(
    events: &mpsc::SyncSender<wire::RendererEvent>,
    command_id: u64,
    event: wire::renderer_event::Event,
) -> Result<()> {
    events
        .try_send(wire::RendererEvent {
            command_id,
            event: Some(event),
        })
        .context("renderer event queue overflow or reader closed")
}

fn operation_reply(
    command: &wire::renderer_command::Command,
    engine: Option<&Engine>,
    error: String,
) -> wire::renderer_event::Event {
    use wire::renderer_command::Command;
    use wire::renderer_event::Event;
    match command {
        Command::PlaySequence(_) => Event::PlaySequenceReply(
            engine
                .map(|engine| engine.sequence_reply(error.clone()))
                .unwrap_or(wire::PlaySequenceReply {
                    error,
                    ..Default::default()
                }),
        ),
        Command::SetActiveEffect(_) => {
            Event::SetActiveEffectReply(wire::SetActiveEffectReply { error })
        }
        Command::UnsetActiveEffect(_) => {
            Event::UnsetActiveEffectReply(wire::UnsetActiveEffectReply { error })
        }
        Command::UpdateBeat(_) => Event::UpdateBeatReply(wire::UpdateBeatReply { error }),
        Command::SetParameter(_) => Event::SetParameterReply(wire::SetParameterReply { error }),
        Command::Login(_) | Command::Heartbeat(_) => {
            unreachable!("session operations have their own replies")
        }
    }
}

/// Readers and writers cannot block the renderer or its final blackout. The owned
/// process exits without joining a pipe reader still waiting on its parent.
pub fn run_live(
    input: impl Read + Send + 'static,
    mut output: impl Write + Send + 'static,
    stop: &AtomicBool,
) -> Result<()> {
    let failure = Arc::new(Mutex::new(None));
    let queued = Arc::new(AtomicUsize::new(0));
    let (control_sender, controls) = mpsc::sync_channel(MAX_QUEUED_CONTROLS);
    let input_queued = Arc::clone(&queued);
    let input_failure = Arc::clone(&failure);
    thread::Builder::new()
        .name("starfire-control".into())
        .spawn(move || receive_controls(input, control_sender, input_queued, input_failure))
        .context("start control reader")?;
    let (events, event_receiver) = mpsc::sync_channel(MAX_QUEUED_EVENTS);
    let output_failure = Arc::clone(&failure);
    thread::Builder::new()
        .name("starfire-status".into())
        .spawn(move || {
            for event in event_receiver {
                if let Err(error) = write_event(&mut output, &event) {
                    record_failure(&output_failure, format!("status output failed: {error:#}"));
                    break;
                }
            }
        })
        .context("start status writer")?;

    let mut session: Option<LiveSession> = None;
    let mut last_client_heartbeat = Instant::now();
    let mut last_server_heartbeat = Instant::now();
    let mut next_frame = 0_u64;
    let mut rendered_frames = 0_u64;
    let mut missed_render_deadlines = 0_u64;
    let mut metrics = wire::Metrics::default();
    let playback = (|| -> Result<()> {
        while !stop.load(Ordering::Relaxed) {
            check_failure(&failure)?;
            ensure!(
                last_client_heartbeat.elapsed() < HEARTBEAT_TIMEOUT,
                "controller heartbeat timed out after 30 seconds"
            );
            let elapsed = session
                .as_ref()
                .map(|session| session.started.elapsed())
                .unwrap_or_default();
            if session.is_some()
                && let Some(wait) =
                    Duration::from_secs_f64(frame_time_seconds(next_frame)).checked_sub(elapsed)
            {
                thread::sleep(wait.min(STOP_POLL_INTERVAL));
                continue;
            }
            let due_frame = latest_due_frame_index(elapsed).max(next_frame);
            missed_render_deadlines += due_frame - next_frame;
            let at_seconds = frame_time_seconds(due_frame);
            let mut report_metrics = false;
            let mut input_closed = false;
            for _ in 0..MAX_CONTROLS_PER_FRAME {
                let control = match controls.try_recv() {
                    Ok(control) => control,
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        input_closed = true;
                        break;
                    }
                };
                queued.fetch_sub(1, Ordering::Relaxed);
                let command = control.command.context("control requires an operation")?;
                match &command {
                    wire::renderer_command::Command::Login(login) => {
                        let result = if session.is_some() {
                            Err(anyhow::anyhow!("renderer session is already logged in"))
                        } else {
                            LiveSession::login(login)
                        };
                        let error = match result {
                            Ok(created) => {
                                session = Some(created);
                                last_client_heartbeat = Instant::now();
                                last_server_heartbeat = Instant::now();
                                String::new()
                            }
                            Err(error) => error_message(&error),
                        };
                        let success = error.is_empty();
                        send_event(
                            &events,
                            control.command_id,
                            wire::renderer_event::Event::LoginReply(wire::LoginReply {
                                error,
                                frame_rate_hz: if success { DEFAULT_FRAME_RATE_HZ } else { 0 },
                                max_message_bytes: if success {
                                    MAX_MESSAGE_BYTES as u32
                                } else {
                                    0
                                },
                            }),
                        )?;
                    }
                    wire::renderer_command::Command::Heartbeat(_) => {
                        ensure!(session.is_some(), "login is required before heartbeat");
                        last_client_heartbeat = Instant::now();
                        report_metrics = true;
                        send_event(
                            &events,
                            control.command_id,
                            wire::renderer_event::Event::Heartbeat(wire::Heartbeat {}),
                        )?;
                    }
                    _ => {
                        let result = match &mut session {
                            Some(session) => {
                                apply_command(&mut session.engine, at_seconds, &command)
                            }
                            None => Err(anyhow::anyhow!(
                                "login is required before playback commands"
                            )),
                        };
                        let error = result
                            .err()
                            .map(|error| error_message(&error))
                            .unwrap_or_default();
                        send_event(
                            &events,
                            control.command_id,
                            operation_reply(
                                &command,
                                session.as_ref().map(|session| &session.engine),
                                error,
                            ),
                        )?;
                    }
                }
            }
            check_failure(&failure)?;
            if input_closed {
                break;
            }
            let Some(session) = &mut session else {
                thread::sleep(STOP_POLL_INTERVAL);
                continue;
            };
            let previous_script_error = metrics.script_error.clone();
            rendered_frames += 1;
            metrics = session.render(
                at_seconds,
                rendered_frames,
                missed_render_deadlines,
                queued.load(Ordering::Relaxed),
            )?;
            if last_server_heartbeat.elapsed() >= HEARTBEAT_INTERVAL {
                send_event(
                    &events,
                    0,
                    wire::renderer_event::Event::Heartbeat(wire::Heartbeat {}),
                )?;
                last_server_heartbeat = Instant::now();
                report_metrics = true;
            }
            if report_metrics || metrics.script_error != previous_script_error {
                send_event(
                    &events,
                    0,
                    wire::renderer_event::Event::Metrics(metrics.clone()),
                )?;
            }
            next_frame = due_frame + 1;
        }
        Ok(())
    })();

    let blackout = session.as_mut().map_or(Ok(()), LiveSession::close);
    let result = match (playback, blackout) {
        (Ok(()), blackout) => blackout,
        (Err(error), Ok(())) => Err(error),
        (Err(error), Err(blackout)) => {
            Err(error.context(format!("blackout also failed: {blackout:#}")))
        }
    };
    if let Err(error) = &result {
        metrics.runtime_error = error_message(error);
        let _ = send_event(&events, 0, wire::renderer_event::Event::Metrics(metrics));
    }
    result
}

fn frame_time_seconds(index: u64) -> f64 {
    index as f64 / f64::from(DEFAULT_FRAME_RATE_HZ)
}

fn latest_due_frame_index(elapsed: Duration) -> u64 {
    (elapsed.as_nanos() * u128::from(DEFAULT_FRAME_RATE_HZ) / NANOSECONDS_PER_SECOND) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_boundaries_are_exact_and_stalls_skip_obsolete_frames() {
        assert_eq!(latest_due_frame_index(Duration::ZERO), 0);
        assert_eq!(latest_due_frame_index(Duration::from_millis(24)), 0);
        assert_eq!(latest_due_frame_index(Duration::from_millis(25)), 1);
        assert_eq!(latest_due_frame_index(Duration::from_millis(249)), 9);
        assert_eq!(frame_time_seconds(9), 0.225);
    }

    #[test]
    fn dropping_a_session_blacks_out_and_releases_its_socket() {
        let directory = tempfile::tempdir().unwrap();
        let rig = directory.path().join("rig.json");
        std::fs::write(&rig, r#"{"schema_version":1,"fixtures":[{"id":"lamp","profile":"generic_rgb","universe":0,"address":1}]}"#).unwrap();
        let sequences = directory.path().join("sequences");
        std::fs::create_dir_all(sequences.join("test")).unwrap();
        std::fs::write(
            sequences.join("test/steady.lua"),
            "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(),1)} end}",
        )
        .unwrap();
        let listener = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        listener
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let login = wire::Login {
            rig_path: rig.to_str().unwrap().into(),
            sequences_path: sequences.to_str().unwrap().into(),
            artnet_destination: listener.local_addr().unwrap().to_string(),
            ..Default::default()
        };
        let mut session = LiveSession::login(&login).unwrap();
        session
            .engine
            .apply(
                0.0,
                &crate::model::Command::Select {
                    bank: "test".into(),
                    sequence: "steady".into(),
                    transition: crate::model::Transition::Cut,
                },
            )
            .unwrap();
        let source = session.output.as_ref().unwrap().local_addr().unwrap();
        session.render(0.0, 1, 0, 0).unwrap();
        let mut packet = [0; 1024];
        listener.recv(&mut packet).unwrap();
        assert_eq!(
            &packet[crate::artnet::ARTDMX_HEADER_SIZE..crate::artnet::ARTDMX_HEADER_SIZE + 3],
            &[255; 3]
        );
        drop(session);
        listener.recv(&mut packet).unwrap();
        assert_eq!(
            &packet[crate::artnet::ARTDMX_HEADER_SIZE..crate::artnet::ARTDMX_HEADER_SIZE + 3],
            &[0; 3]
        );
        assert!(std::net::UdpSocket::bind(source).is_ok());
    }
}
