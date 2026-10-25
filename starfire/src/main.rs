use std::collections::BTreeSet;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use serde_json::json;

use starfire::artnet::ArtNetOutput;
use starfire::engine::RenderedFrame;
use starfire::fixtures::FixtureRig;
use starfire::replay::{Replay, dmx_digest, render_replay, write_capture};
use starfire::script::SequenceCatalog;

const DEFAULT_RIG: &str = "starfire/rigs/room.json";
const DEFAULT_SEQUENCES: &str = "starfire/sequences";
const STOP_POLL_INTERVAL: Duration = Duration::from_millis(20);
const MIN_STROBE_SAMPLES_PER_CYCLE: f64 = 4.0;

#[derive(Parser)]
#[command(about = "Beat-driven Lua lighting renderer", version)]
struct Arguments {
    #[command(subcommand)]
    command: Action,
}

#[derive(Subcommand)]
enum Action {
    /// Render live controls from framed protobuf on stdin; status uses stdout.
    Live {
        #[arg(long, default_value = DEFAULT_RIG)]
        rig: PathBuf,
        #[arg(long, default_value = DEFAULT_SEQUENCES)]
        sequences: PathBuf,
        #[arg(long)]
        artnet: SocketAddrV4,
        #[arg(long)]
        broadcast: bool,
    },
    /// Validate and render recorded input. Network output requires --artnet.
    Replay {
        input: PathBuf,
        #[arg(long, default_value = DEFAULT_RIG)]
        rig: PathBuf,
        #[arg(long, default_value = DEFAULT_SEQUENCES)]
        sequences: PathBuf,
        #[arg(long)]
        output: Option<PathBuf>,
        #[arg(long)]
        summary: Option<PathBuf>,
        /// Explicit IPv4 address and UDP port; default is no network output.
        #[arg(long)]
        artnet: Option<SocketAddrV4>,
        /// Pace output to the recorded timeline. Required for network playback.
        #[arg(long, requires = "artnet")]
        realtime: bool,
        /// Explicitly permit legacy ArtDmx broadcast output.
        #[arg(long, requires = "artnet")]
        broadcast: bool,
        /// Stop paced output when this path appears; owned by the launching CLI.
        #[arg(long, requires = "artnet")]
        stop_file: Option<PathBuf>,
    },
    /// List registered bank/sequence identities.
    List {
        #[arg(long, default_value = DEFAULT_SEQUENCES)]
        sequences: PathBuf,
    },
    /// Validate a fixture patch without emitting DMX.
    Rig { input: PathBuf },
}

fn main() -> std::process::ExitCode {
    match run(Arguments::parse()) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("starfire: {error:#}");
            std::process::ExitCode::FAILURE
        }
    }
}

fn run(arguments: Arguments) -> Result<()> {
    match arguments.command {
        Action::Live {
            rig,
            sequences,
            artnet,
            broadcast,
        } => {
            let rig = FixtureRig::load(&rig)?;
            let catalog = SequenceCatalog::load(&sequences)?;
            let local = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0);
            let mut sender = if broadcast {
                ArtNetOutput::bind_broadcast(local, artnet)?
            } else {
                ArtNetOutput::bind(local, artnet)?
            };
            let stop = Arc::new(AtomicBool::new(false));
            let handler_stop = Arc::clone(&stop);
            ctrlc::set_handler(move || handler_stop.store(true, Ordering::Relaxed))
                .context("install live stop handler")?;
            starfire::live::run_live(
                rig,
                catalog,
                &mut sender,
                std::io::stdin(),
                std::io::stdout(),
                &stop,
            )?;
        }
        Action::List { sequences } => {
            println!(
                "{}",
                serde_json::to_string_pretty(&SequenceCatalog::load(&sequences)?.list())?
            );
        }
        Action::Rig { input } => {
            let rig = FixtureRig::load(&input)?;
            let universes = rig
                .encode(&rig.empty_states())?
                .into_keys()
                .collect::<Vec<_>>();
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "fixtures": rig.fixture_ids(), "groups": rig.groups(), "universes": universes,
                }))?
            );
        }
        Action::Replay {
            input,
            rig,
            sequences,
            output,
            summary,
            artnet,
            realtime,
            broadcast,
            stop_file,
        } => {
            ensure!(
                artnet.is_none() || realtime,
                "network playback requires --realtime"
            );
            validate_output_paths(&input, &rig, output.as_deref(), summary.as_deref())?;
            let replay = Replay::load(&input)?;
            let rig = FixtureRig::load(&rig)?;
            let catalogue = SequenceCatalog::load(&sequences)?;
            let started = Instant::now();
            let result = render_replay(&replay, rig.clone(), catalogue)?;
            let render_seconds = started.elapsed().as_secs_f64();
            let failures: BTreeSet<_> = result
                .frames
                .iter()
                .filter_map(|frame| frame.script_error.as_deref())
                .collect();
            if let Some(path) = output {
                write_capture(&path, &result.frames)?;
            }
            let report = json!({
                "frames": result.frames.len(),
                "duration_seconds": replay.duration_seconds,
                "frame_rate_hz": replay.frame_rate_hz,
                "dmx_sha256": dmx_digest(&result.frames),
                "render_seconds": render_seconds,
                "script_errors": failures,
            });
            let report = serde_json::to_string_pretty(&report)?;
            if let Some(path) = summary {
                std::fs::write(&path, &report)
                    .with_context(|| format!("write replay summary {}", path.display()))?;
            }
            println!("{report}");
            ensure!(
                failures.is_empty(),
                "replay contains script faults; network playback cancelled"
            );
            if let Some(destination) = artnet {
                validate_output_cadence(&result.frames, replay.frame_rate_hz)?;
                let local = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0);
                let mut sender = if broadcast {
                    ArtNetOutput::bind_broadcast(local, destination)?
                } else {
                    ArtNetOutput::bind(local, destination)?
                };
                let stop = Arc::new(AtomicBool::new(false));
                let handler_stop = Arc::clone(&stop);
                ctrlc::set_handler(move || handler_stop.store(true, Ordering::Relaxed))
                    .context("install playback stop handler")?;
                play_frames(
                    &result.frames,
                    &rig,
                    &mut sender,
                    &stop,
                    stop_file.as_deref(),
                )?;
            }
        }
    }
    Ok(())
}

fn validate_output_paths(
    input: &Path,
    rig: &Path,
    output: Option<&Path>,
    summary: Option<&Path>,
) -> Result<()> {
    let mut paths = vec![input.canonicalize()?, rig.canonicalize()?];
    for path in [output, summary].into_iter().flatten() {
        let resolved = if path.exists() {
            path.canonicalize()?
        } else {
            let parent = path
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty())
                .unwrap_or(Path::new("."));
            parent
                .canonicalize()?
                .join(path.file_name().context("output needs a file name")?)
        };
        ensure!(
            !paths.contains(&resolved),
            "capture and summary must not overwrite inputs or each other"
        );
        paths.push(resolved);
    }
    Ok(())
}

fn validate_output_cadence(frames: &[RenderedFrame], frame_rate_hz: u32) -> Result<()> {
    let maximum_strobe_hz = frames
        .iter()
        .flat_map(|frame| frame.fixture_properties_before_strobe.values())
        .map(|state| state.strobe_hz)
        .fold(0.0, f64::max);
    ensure!(
        f64::from(frame_rate_hz) >= maximum_strobe_hz * MIN_STROBE_SAMPLES_PER_CYCLE,
        "network playback requires at least four frames per strobe cycle"
    );
    Ok(())
}

/// Drop obsolete frames after a stall; never burst old movement commands.
fn latest_due_frame(frames: &[RenderedFrame], next: usize, elapsed_seconds: f64) -> usize {
    let due = frames[next..].partition_point(|frame| frame.time_seconds <= elapsed_seconds);
    next + due.saturating_sub(1)
}

fn play_frames(
    frames: &[RenderedFrame],
    rig: &FixtureRig,
    sender: &mut ArtNetOutput,
    stop: &AtomicBool,
    stop_file: Option<&std::path::Path>,
) -> Result<()> {
    let started = Instant::now();
    let mut next = 0;
    let mut last_states = rig.empty_states();
    let playback = (|| -> Result<()> {
        while next < frames.len() && !stop.load(Ordering::Relaxed) {
            if let Some(path) = stop_file
                && path.try_exists().context("check playback stop signal")?
            {
                break;
            }
            let target = Duration::from_secs_f64(frames[next].time_seconds);
            if let Some(wait) = target.checked_sub(started.elapsed()) {
                thread::sleep(wait.min(STOP_POLL_INTERVAL));
                continue;
            }
            next = latest_due_frame(frames, next, started.elapsed().as_secs_f64());
            let frame = &frames[next];
            // Preserve this position even if sending only some universes succeeds.
            last_states.clone_from(&frame.fixture_properties_before_strobe);
            sender.send_frame(&frame.dmx_frames_by_universe)?;
            next += 1;
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
        if let Err(blackout_error) = blackout {
            return Err(error.context(format!("blackout also failed: {blackout_error:#}")));
        }
        return Err(error);
    }
    blackout.context("send final blackout")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[test]
    fn late_playback_skips_expired_frames_without_rewinding() {
        let frames = [0.0, 0.025, 0.05, 0.075].map(|time_seconds| RenderedFrame {
            time_seconds,
            beat_position: 0.0,
            fixture_properties_before_strobe: BTreeMap::new(),
            dmx_frames_by_universe: BTreeMap::new(),
            active_bank: None,
            active_sequence: None,
            script_error: None,
        });
        assert_eq!(latest_due_frame(&frames, 0, 0.06), 2);
        assert_eq!(latest_due_frame(&frames, 2, 0.06), 2);
        assert_eq!(latest_due_frame(&frames, 2, 5.0), 3);
    }

    #[test]
    fn cancellation_still_sends_a_dark_frame() {
        let listener = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        listener
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let destination = match listener.local_addr().unwrap() {
            std::net::SocketAddr::V4(address) => address,
            _ => unreachable!(),
        };
        let mut sender = ArtNetOutput::bind("127.0.0.1:0".parse().unwrap(), destination).unwrap();
        let rig = FixtureRig::from_json(include_str!("../rigs/room.json")).unwrap();
        play_frames(&[], &rig, &mut sender, &AtomicBool::new(true), None).unwrap();
        let mut packet = [0; 1024];
        let count = listener.recv(&mut packet).unwrap();
        assert_eq!(count, 530);
        for dimmer in [7, 19, 68, 76, 84, 92] {
            assert_eq!(packet[18 + dimmer], 0);
        }
    }

    #[test]
    fn network_playback_rejects_strobe_aliasing() {
        let frame = RenderedFrame {
            time_seconds: 0.0,
            beat_position: 0.0,
            fixture_properties_before_strobe: BTreeMap::from([(
                "light".into(),
                starfire::model::FixtureState {
                    strobe_hz: 10.0,
                    ..Default::default()
                },
            )]),
            dmx_frames_by_universe: BTreeMap::new(),
            active_bank: None,
            active_sequence: None,
            script_error: None,
        };
        assert!(validate_output_cadence(std::slice::from_ref(&frame), 10).is_err());
        assert!(validate_output_cadence(&[frame], 40).is_ok());
    }
}
