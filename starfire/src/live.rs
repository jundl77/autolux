//! Paced local-process host. The renderer itself only sees supplied musical time.

use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};

use crate::artnet::ArtNetOutput;
use crate::engine::{Engine, RenderedFrame};
use crate::fixtures::FixtureRig;
use crate::model::DEFAULT_FRAME_RATE_HZ;
use crate::protocol::{musical_command, read_control, wire, write_event};
use crate::script::SequenceCatalog;

const MAX_QUEUED_CONTROLS: usize = 1024;
const MAX_QUEUED_EVENTS: usize = 32;
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_POLL_INTERVAL: Duration = Duration::from_millis(10);
const MILLISECONDS_PER_SECOND: f64 = 1000.0;
const NANOSECONDS_PER_SECOND: u128 = 1_000_000_000;

type SharedFailure = Arc<Mutex<Option<String>>>;

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
    sender: mpsc::SyncSender<wire::Control>,
    queued: Arc<AtomicUsize>,
    failure: SharedFailure,
) {
    loop {
        match read_control(&mut input) {
            Ok(Some(control)) => {
                queued.fetch_add(1, Ordering::Relaxed);
                if let Err(error) = sender.try_send(control) {
                    queued.fetch_sub(1, Ordering::Relaxed);
                    if matches!(error, mpsc::TrySendError::Full(_)) {
                        record_failure(&failure, "live control queue overflow".to_owned());
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

/// Pipe readers/writers run separately so a stalled parent cannot block blackout.
/// The owned process exits without joining a reader still blocked on stdin.
pub fn run_live(
    rig: FixtureRig,
    catalog: SequenceCatalog,
    sender: &mut ArtNetOutput,
    input: impl Read + Send + 'static,
    mut output: impl Write + Send + 'static,
    stop: &AtomicBool,
) -> Result<()> {
    let mut engine = Engine::new(rig.clone(), catalog)?;
    let failure = Arc::new(Mutex::new(None));
    let queued = Arc::new(AtomicUsize::new(0));
    let (control_sender, controls) = mpsc::sync_channel(MAX_QUEUED_CONTROLS);
    let input_queued = Arc::clone(&queued);
    let input_failure = Arc::clone(&failure);
    thread::Builder::new()
        .name("starfire-control".to_owned())
        .spawn(move || receive_controls(input, control_sender, input_queued, input_failure))
        .context("start control reader")?;

    let (events, event_receiver) = mpsc::sync_channel(MAX_QUEUED_EVENTS);
    let output_failure = Arc::clone(&failure);
    thread::Builder::new()
        .name("starfire-status".to_owned())
        .spawn(move || {
            for event in event_receiver {
                if let Err(error) = write_event(&mut output, event) {
                    record_failure(&output_failure, format!("status output failed: {error:#}"));
                    break;
                }
            }
        })
        .context("start status writer")?;
    events
        .try_send(wire::renderer_event::Event::Ready(wire::Ready {
            frame_rate_hz: DEFAULT_FRAME_RATE_HZ,
        }))
        .context("announce renderer readiness")?;

    let started = Instant::now();
    let mut last_heartbeat = Duration::ZERO;
    let mut next_frame = 0_u64;
    let mut rendered_frames = 0_u64;
    let mut skipped_frames = 0_u64;
    let mut reported_script_error = None;
    let mut last_states = rig.empty_states();
    let playback = (|| -> Result<()> {
        while !stop.load(Ordering::Relaxed) {
            check_failure(&failure)?;
            let elapsed = started.elapsed();
            ensure!(
                elapsed.saturating_sub(last_heartbeat) < HEARTBEAT_TIMEOUT,
                "controller heartbeat timed out after 30 seconds"
            );
            let target = frame_time_seconds(next_frame);
            if let Some(wait) = Duration::from_secs_f64(target).checked_sub(elapsed) {
                thread::sleep(wait.min(STOP_POLL_INTERVAL));
                continue;
            }
            let due_frame = latest_due_frame_index(elapsed).max(next_frame);
            skipped_frames += due_frame - next_frame;
            let at_seconds = frame_time_seconds(due_frame);
            let mut report_health = false;
            let mut input_closed = false;
            for _ in 0..MAX_QUEUED_CONTROLS {
                match controls.try_recv() {
                    Ok(control) => {
                        queued.fetch_sub(1, Ordering::Relaxed);
                        let command = control.command.context("control requires a command")?;
                        if let Some(command) = musical_command(command)? {
                            engine.apply(at_seconds, &command)?;
                        } else {
                            last_heartbeat = elapsed;
                            report_health = true;
                        }
                    }
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        input_closed = true;
                        break;
                    }
                }
            }
            check_failure(&failure)?;
            if input_closed {
                break;
            }
            let render_started = Instant::now();
            let frame = engine.render_at(at_seconds)?;
            let render_ms = render_started.elapsed().as_secs_f64() * MILLISECONDS_PER_SECOND;
            last_states.clone_from(&frame.fixture_properties_before_strobe);
            sender.send_frame(&frame.dmx_frames_by_universe)?;
            rendered_frames += 1;
            if report_health || frame.script_error != reported_script_error {
                events
                    .try_send(wire::renderer_event::Event::Health(health(
                        &frame,
                        rendered_frames,
                        skipped_frames,
                        queued.load(Ordering::Relaxed),
                        render_ms,
                    )))
                    .context("renderer status queue overflow or reader closed")?;
                reported_script_error.clone_from(&frame.script_error);
            }
            next_frame = due_frame + 1;
        }
        Ok(())
    })();

    for state in last_states.values_mut() {
        state.intensity = 0.0;
        state.strobe_hz = 0.0;
    }
    let blackout = rig
        .encode(&last_states)
        .and_then(|frame| sender.send_frame(&frame));
    if let Err(error) = playback {
        let message = format!("{error:#}");
        let _ = events.try_send(wire::renderer_event::Event::Error(wire::Error { message }));
        return match blackout {
            Ok(()) => Err(error),
            Err(blackout_error) => {
                Err(error.context(format!("blackout also failed: {blackout_error:#}")))
            }
        };
    }
    blackout.context("send live final blackout")
}

fn frame_time_seconds(index: u64) -> f64 {
    index as f64 / f64::from(DEFAULT_FRAME_RATE_HZ)
}

fn latest_due_frame_index(elapsed: Duration) -> u64 {
    // Integer arithmetic avoids skipping a boundary because of float rounding.
    (elapsed.as_nanos() * u128::from(DEFAULT_FRAME_RATE_HZ) / NANOSECONDS_PER_SECOND) as u64
}

fn health(
    frame: &RenderedFrame,
    rendered_frames: u64,
    skipped_frames: u64,
    queued_commands: usize,
    last_render_ms: f64,
) -> wire::Health {
    wire::Health {
        elapsed_seconds: frame.time_seconds,
        rendered_frames,
        skipped_frames,
        queued_commands: queued_commands as u32,
        last_render_ms,
        active_bank: frame.active_bank.clone().unwrap_or_default(),
        active_sequence: frame.active_sequence.clone().unwrap_or_default(),
        script_error: frame.script_error.clone().unwrap_or_default(),
    }
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
}
