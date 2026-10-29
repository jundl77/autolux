use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use prost::Message;

use crate::artnet::ArtNetOutput;
use crate::engine::{Engine, extinguish};
use crate::fixtures::FixtureRig;
use crate::model::{Command, DEFAULT_FRAME_RATE_HZ, FixtureStates, Transition};
use crate::palette::PaletteTable;
use crate::protocol::{
    ClientMessage, MAX_MESSAGE_BYTES, ServerMessage, apply_command, error_message,
    read_session_control, wire,
};
use crate::script::{AutoloopCatalog, missing_groups_warning};
use crate::special;

const MAX_QUEUED_CONTROLS: usize = 1024;
const MAX_CONTROLS_PER_FRAME: usize = 32;
// Rejections and state changes can each produce one event per command.
const MAX_QUEUED_EVENTS: usize = 2 * MAX_QUEUED_CONTROLS + 4;
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(30);
const METRICS_INTERVAL: Duration = Duration::from_secs(1);
const STOP_POLL_INTERVAL: Duration = Duration::from_millis(10);
const NANOSECONDS_PER_SECOND: u128 = 1_000_000_000;
pub const PROTOCOL_MAJOR: u32 = wire::ProtocolVersion::ProtocolMajor as u32;

type SharedFailure = Arc<Mutex<Option<String>>>;

enum LiveOutput {
    ArtNet(ArtNetOutput),
    Radius {
        transmitter: radius::RadiusTx,
        universe: u16,
    },
}

impl LiveOutput {
    fn open(login: &wire::Login, rig: &FixtureRig) -> Result<Self> {
        match login.output.as_ref().context("login requires an output")? {
            wire::login::Output::ArtnetDestination(address) => {
                let destination: SocketAddrV4 = address
                    .parse()
                    .context("invalid Art-Net IPv4 destination")?;
                let local = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0);
                Ok(Self::ArtNet(ArtNetOutput::bind(local, destination)?))
            }
            wire::login::Output::Radius(settings) => {
                let address: SocketAddrV4 = settings
                    .receiver_address
                    .parse()
                    .context("invalid Radius receiver IPv4 address")?;
                let universe =
                    u16::try_from(settings.universe).context("invalid Radius universe")?;
                let initial = rig.encode(&rig.empty_states())?;
                ensure!(
                    initial.len() == 1 && initial.contains_key(&universe),
                    "Radius output requires exactly its configured universe"
                );
                let core = |value: Option<u32>| -> Result<i32> {
                    value
                        .map(i32::try_from)
                        .transpose()
                        .context("invalid CPU index")
                        .map(|value| value.unwrap_or(-1))
                };
                let transmitter = radius::RadiusTx::start(radius::TxConfig {
                    peer_address: address.ip().to_string(),
                    bus_port: address.port(),
                    run_id: login.run_id,
                    audio_device_id: settings.capture_device_id.clone(),
                    health_file: std::env::var_os("AUTOLUX_RUNTIME_DIR").map(|directory| {
                        std::path::PathBuf::from(directory).join("transport-health.json")
                    }),
                    playback_delay_seconds: settings.playback_delay_seconds,
                    audio_enabled: settings.receiver_audio_enabled,
                    network_core: core(settings.network_core)?,
                    receiver: radius::config::ReceiverSettings {
                        audio_enabled: settings.receiver_audio_enabled,
                        lighting_enabled: settings.receiver_lighting_enabled,
                        jitter_buffer_ms: settings.receiver_buffer_ms,
                    },
                })?;
                transmitter.update_light([0; radius::protocol::DMX_CHANNELS]);
                let started = Instant::now();
                while !transmitter.is_ready() {
                    if let Some(error) = transmitter.error() {
                        bail!(error);
                    }
                    ensure!(
                        started.elapsed() < radius::protocol::LOGIN_TIMEOUT,
                        "Radius receiver did not become ready"
                    );
                    thread::sleep(STOP_POLL_INTERVAL);
                }
                Ok(Self::Radius {
                    transmitter,
                    universe,
                })
            }
        }
    }

    fn discard_delayed_audio(&self) {
        if let Self::Radius { transmitter, .. } = self {
            transmitter.discard_delayed_audio();
        }
    }

    fn send_frame(&mut self, frames: &BTreeMap<u16, Vec<u8>>) -> Result<()> {
        match self {
            Self::ArtNet(output) => output.send_frame(frames),
            Self::Radius {
                transmitter,
                universe,
            } => {
                if let Some(error) = transmitter.error() {
                    bail!(error);
                }
                let channels = frames
                    .get(universe)
                    .context("rendered frame is missing the Radius universe")?;
                transmitter.update_light(
                    channels
                        .as_slice()
                        .try_into()
                        .context("invalid DMX frame length")?,
                );
                Ok(())
            }
        }
    }
}

struct LiveSession {
    run_id: u64,
    reply: wire::LoginReply,
    rig: FixtureRig,
    engine: Engine,
    output: Option<LiveOutput>,
    last_states: FixtureStates,
    started: Instant,
}

impl LiveSession {
    fn login(login: &wire::Login) -> Result<Self> {
        ensure!(login.run_id != 0, "login run_id must be nonzero");
        ensure!(
            login.protocol_major == PROTOCOL_MAJOR,
            "incompatible protocol major {}; expected {}",
            login.protocol_major,
            PROTOCOL_MAJOR
        );
        if let Some(core) = login.render_core {
            radius::config::pin_worker(i32::try_from(core).context("invalid render CPU index")?)?;
        }
        let rig = FixtureRig::from_wire(login.rig.as_ref().context("login requires a rig")?)?;
        for warning in rig.spatial().warnings() {
            eprintln!("starfire: warning: {warning}");
        }
        let reply = wire::LoginReply {
            run_id: login.run_id,
            command_id: login.command_id,
            frame_rate_hz: DEFAULT_FRAME_RATE_HZ,
            max_message_bytes: MAX_MESSAGE_BYTES as u32,
            protocol_major: PROTOCOL_MAJOR,
            groups: special::capabilities(&rig),
        };
        ensure!(
            reply.encoded_len() <= MAX_MESSAGE_BYTES,
            "the rig's {} group capabilities need a {}-byte login reply, over the {MAX_MESSAGE_BYTES}-byte real-time cap; use fewer or shorter group names",
            reply.groups.len(),
            reply.encoded_len()
        );
        let catalog = AutoloopCatalog::from_definitions(&login.autoloops)?;
        if let Some(warning) = missing_groups_warning(&catalog.groups_missing_from(&rig)) {
            eprintln!("starfire: warning: {warning}");
        }
        let palettes = PaletteTable::from_wire(&login.palettes)?;
        let autoloops = catalog.list();
        let mut probe = Engine::new(rig.clone(), catalog.clone(), palettes.clone())?;
        for parameter in &login.parameters {
            probe.set_parameter(0.0, parameter)?;
        }
        for autoloop in autoloops {
            probe.apply(
                0.0,
                &Command::Select {
                    bank: autoloop.bank,
                    autoloop: autoloop.autoloop,
                    transition: Transition::Cut,
                    palette: palettes.initial().to_owned(),
                },
            )?;
            let frame = probe.render_at(0.0)?;
            if let Some(error) = frame.script_error {
                bail!(error);
            }
        }
        probe.check_scripts_pure()?;
        drop(probe);
        // Probing must not carry fixture positions or script state into playback.
        let mut engine = Engine::new(rig.clone(), catalog, palettes)?;
        for parameter in &login.parameters {
            engine.set_parameter(0.0, parameter)?;
        }
        let output = LiveOutput::open(login, &rig)?;
        let last_states = rig.empty_states();
        Ok(Self {
            run_id: login.run_id,
            reply,
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
        last_processed_command_id: u64,
    ) -> Result<wire::Metrics> {
        let started = Instant::now();
        let frame = self.engine.render_at(at_seconds)?;
        let last_render_duration_seconds = started.elapsed().as_secs_f64();
        self.last_states
            .clone_from(&frame.fixture_properties_before_strobe);
        let output = self
            .output
            .as_mut()
            .context("rendering session is closed")?;
        output.send_frame(&frame.dmx_frames_by_universe)?;
        Ok(wire::Metrics {
            run_id: self.run_id,
            elapsed_seconds: frame.time_seconds,
            rendered_frames,
            missed_render_deadlines,
            queued_commands: queued_commands as u32,
            last_render_duration_seconds,
            active_bank: frame.active_bank.unwrap_or_default(),
            active_autoloop: frame.active_autoloop.unwrap_or_default(),
            script_error: frame.script_error.unwrap_or_default(),
            runtime_error: String::new(),
            last_processed_command_id,
            pending_cues: self.engine.pending_cue_count() as u32,
            overlay_error: self.engine.overlay_error().unwrap_or_default().to_owned(),
            active_overlay_count: self.engine.active_overlay_count() as u32,
        })
    }

    fn close(&mut self) -> Result<()> {
        let Some(mut output) = self.output.take() else {
            return Ok(());
        };
        extinguish(&mut self.last_states);
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
    sender: mpsc::SyncSender<ClientMessage>,
    queued: Arc<AtomicUsize>,
    failure: SharedFailure,
    logged_in: Arc<AtomicBool>,
) {
    let mut last_command_id = 0;
    loop {
        match read_session_control(&mut input, logged_in.load(Ordering::Acquire)) {
            Ok(Some(control)) => {
                if let Some(command_id) = control.command_id() {
                    if command_id <= last_command_id {
                        record_failure(
                            &failure,
                            "command IDs must increase within the renderer session".into(),
                        );
                        return;
                    }
                    last_command_id = command_id;
                }
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

fn send_event(events: &mpsc::SyncSender<ServerMessage>, event: ServerMessage) -> Result<()> {
    events
        .try_send(event)
        .context("renderer event queue overflow or reader closed")
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
    let logged_in = Arc::new(AtomicBool::new(false));
    let input_logged_in = Arc::clone(&logged_in);
    thread::Builder::new()
        .name("starfire-control".into())
        .spawn(move || {
            receive_controls(
                input,
                control_sender,
                input_queued,
                input_failure,
                input_logged_in,
            )
        })
        .context("start control reader")?;
    let (events, event_receiver) = mpsc::sync_channel(MAX_QUEUED_EVENTS);
    let output_failure = Arc::clone(&failure);
    thread::Builder::new()
        .name("starfire-status".into())
        .spawn(move || {
            for event in event_receiver {
                if let Err(error) = ServerMessage::write(&event, &mut output) {
                    record_failure(&output_failure, format!("status output failed: {error:#}"));
                    break;
                }
            }
        })
        .context("start status writer")?;

    let mut session: Option<LiveSession> = None;
    let mut last_client_heartbeat = Instant::now();
    let mut last_server_heartbeat = Instant::now();
    let mut last_metrics = Instant::now();
    let mut next_frame = 0_u64;
    let mut rendered_frames = 0_u64;
    let mut missed_render_deadlines = 0_u64;
    let mut last_processed_command_id = 0;
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
                let command = match controls.try_recv() {
                    Ok(control) => control,
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        input_closed = true;
                        break;
                    }
                };
                queued.fetch_sub(1, Ordering::Relaxed);
                if let Some(command_id) = command.command_id() {
                    last_processed_command_id = command_id;
                }
                match &command {
                    ClientMessage::Login(login) => {
                        let result = if session.is_some() {
                            Err(anyhow::anyhow!("renderer session is already logged in"))
                        } else {
                            LiveSession::login(login)
                        };
                        match result {
                            Ok(created) => {
                                let reply = created.reply.clone();
                                metrics.run_id = created.run_id;
                                session = Some(created);
                                logged_in.store(true, Ordering::Release);
                                last_client_heartbeat = Instant::now();
                                last_server_heartbeat = Instant::now();
                                report_metrics = true;
                                send_event(&events, ServerMessage::LoginReply(reply))?;
                            }
                            Err(error) => send_event(
                                &events,
                                ServerMessage::ErrorReply(wire::ErrorReply {
                                    command_id: login.command_id,
                                    message: error_message(&error),
                                }),
                            )?,
                        }
                    }
                    ClientMessage::Heartbeat(_) => {
                        ensure!(session.is_some(), "login is required before heartbeat");
                        last_client_heartbeat = Instant::now();
                        report_metrics = true;
                    }
                    _ => {
                        let result = match &mut session {
                            Some(session) => {
                                if let (ClientMessage::DiscardDelayedAudio(_), Some(output)) =
                                    (&command, &session.output)
                                {
                                    output.discard_delayed_audio();
                                }
                                apply_command(&mut session.engine, at_seconds, &command)
                            }
                            None => Err(anyhow::anyhow!(
                                "login is required before playback commands"
                            )),
                        };
                        if let Err(error) = result {
                            send_event(
                                &events,
                                ServerMessage::ErrorReply(wire::ErrorReply {
                                    command_id: last_processed_command_id,
                                    message: error_message(&error),
                                }),
                            )?;
                        }
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
                last_processed_command_id,
            )?;
            if last_server_heartbeat.elapsed() >= HEARTBEAT_INTERVAL {
                send_event(&events, ServerMessage::Heartbeat(wire::Heartbeat {}))?;
                last_server_heartbeat = Instant::now();
                report_metrics = true;
            }
            if report_metrics
                || last_metrics.elapsed() >= METRICS_INTERVAL
                || metrics.script_error != previous_script_error
            {
                send_event(&events, ServerMessage::Metrics(metrics.clone()))?;
                last_metrics = Instant::now();
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
        metrics.last_processed_command_id = last_processed_command_id;
        let _ = send_event(&events, ServerMessage::Metrics(metrics));
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
        let listener = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        listener
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let login = wire::Login {
            run_id: 1,
            protocol_major: PROTOCOL_MAJOR,
            rig: Some(wire::RigDefinition {
                fixtures: vec![wire::Fixture {
                    area: wire::StageArea::Centre as i32,
                    fixture_uuid: "lamp".into(),
                    name: "lamp".into(),
                    profile_id: "rgb".into(),
                    address: 1,
                    ..Default::default()
                }],
                profiles: vec![{
                    let generic = wire::RigDefinition::decode(
                        include_bytes!("../../autolux/tests/data/generic_rig/generic.rig.pb").as_slice(),
                    )
                    .unwrap();
                    let profile = generic
                        .profiles
                        .into_iter()
                        .find(|profile| profile.profile_id.starts_with("Generic@RGB@"))
                        .unwrap();
                    wire::FixtureProfile {
                        profile_id: "rgb".into(),
                        ..profile
                    }
                }],
                ..Default::default()
            }),
            autoloops: vec![wire::AutoloopDefinition {
                autoloop_id: "steady".into(),
                bank: "test".into(),
                lua_source:
                    "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(),1), sf.pan_tilt(sf.all(),0.2,0.8)} end}"
                        .into(),
                intensity_htp: false,
            }],
            output: Some(wire::login::Output::ArtnetDestination(listener.local_addr().unwrap().to_string())),
            palettes: vec![wire::Palette {
                name: "techno".into(),
                primary: Some(wire::RgbColor::default()),
                secondary: Some(wire::RgbColor::default()),
                accent: Some(wire::RgbColor::default()),
                cold: Some(wire::RgbColor::default()),
                black: Some(wire::RgbColor::default()),
            }],
            ..Default::default()
        };
        let mut session = LiveSession::login(&login).unwrap();
        let idle = session.engine.render_at(0.0).unwrap();
        let mut expected_idle = session.rig.empty_states();
        for state in expected_idle.values_mut() {
            state.output_blackout = true;
        }
        assert_eq!(
            idle.fixture_properties_before_strobe, expected_idle,
            "preflight must not leak script positions into idle"
        );
        session
            .engine
            .apply(
                0.0,
                &crate::model::Command::Select {
                    bank: "test".into(),
                    autoloop: "steady".into(),
                    transition: crate::model::Transition::Cut,
                    palette: "techno".into(),
                },
            )
            .unwrap();
        let Some(LiveOutput::ArtNet(output)) = session.output.as_ref() else {
            panic!("expected Art-Net output");
        };
        let source = output.local_addr().unwrap();
        session.render(0.0, 1, 0, 0, 1).unwrap();
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
