use crate::{
    audio::{
        AudioBlock, CAPTURE_DISCONTINUITY_LIMIT, CAPTURE_DISCONTINUITY_WINDOW, CaptureMetrics,
        CaptureStream,
    },
    config::{TxConfig, pin_worker},
    protocol::{self, Message, wire},
    transport::Connection,
};
use anyhow::{Context, Result, bail, ensure};
use crossbeam_queue::ArrayQueue;
use std::{
    collections::VecDeque,
    io::{Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::{SyncSender, sync_channel},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const POLL_INTERVAL: Duration = Duration::from_millis(1);
const CONNECT_ATTEMPT_TIMEOUT: Duration = Duration::from_millis(250);
const MAX_AUDIO_BLOCKS_PER_TICK: usize = 128;
const CALLBACK_STALE_TIMEOUT: Duration = Duration::from_secs(1);
const MAX_LIGHT_UPDATES: usize = 512;
const HEALTH_ERROR_LOG_INTERVAL: Duration = Duration::from_secs(30);
const REPORT_QUEUE: usize = 4;

struct TimedLight {
    at: Instant,
    channels: [u8; protocol::DMX_CHANNELS],
}

struct LightTimeline {
    pending: VecDeque<TimedLight>,
    current: [u8; protocol::DMX_CHANNELS],
}

impl LightTimeline {
    fn new() -> Self {
        Self {
            pending: VecDeque::new(),
            current: [0; protocol::DMX_CHANNELS],
        }
    }

    fn state_at(&mut self, assembly_completed_at: Instant) -> [u8; protocol::DMX_CHANNELS] {
        while self
            .pending
            .front()
            .is_some_and(|light| light.at <= assembly_completed_at)
        {
            self.current = self.pending.pop_front().unwrap().channels;
        }
        self.current
    }
}

#[derive(Clone, Debug, Default)]
pub struct TxSnapshot {
    pub tx: wire::TxMetrics,
    pub rx: Option<wire::Metrics>,
    pub receiver_age_seconds: Option<f64>,
}

fn write_health(path: &Path, run_id: u64, report: &HealthReport) -> Result<()> {
    let timestamp = report.at.duration_since(UNIX_EPOCH)?.as_secs_f64();
    let bytes = serde_json::to_vec(&serde_json::json!({
        "run_id": run_id,
        "timestamp": timestamp,
        "tx": report.snapshot.tx,
        "rx": report.snapshot.rx,
        "rx_age_seconds": report.snapshot.receiver_age_seconds,
        "reports_dropped_total": report.dropped_reports,
        "audio_dropped_total": report.dropped_audio,
    }))?;
    // Readers retain the previous valid report during a partial read. Direct writes
    // remain possible when a Windows reader forbids file replacement.
    std::fs::write(path, bytes).context("write Radius transport health")
}

struct HealthReport {
    // Stamped by the tx thread, so a lagging writer shows up as report age.
    at: SystemTime,
    snapshot: TxSnapshot,
    dropped_reports: u64,
    /// AudioFrame packets (up to 128 stereo frames each) dropped under backpressure.
    dropped_audio: u64,
}

enum Report {
    Health(Box<HealthReport>),
    Log(String),
}

/// The thread that drains the 500 ms capture queue must never touch the disk: under
/// disk pressure the health JSON write was measured blocking ~550 ms, and stderr is a
/// file too. Reports that find the writer busy are dropped and counted, never waited on.
struct Reporter {
    queue: Option<SyncSender<Report>>,
    worker: Option<JoinHandle<()>>,
    dropped: u64,
}

impl Reporter {
    fn spawn(mut write: impl FnMut(Report) + Send + 'static) -> Result<Self> {
        let (queue, reports) = sync_channel(REPORT_QUEUE);
        let worker = thread::Builder::new()
            .name("radius-tx-report".into())
            .spawn(move || reports.into_iter().for_each(&mut write))?;
        Ok(Self {
            queue: Some(queue),
            worker: Some(worker),
            dropped: 0,
        })
    }

    fn submit(&mut self, report: Report) {
        if let Some(queue) = &self.queue
            && queue.try_send(report).is_err()
        {
            self.dropped += 1;
        }
    }

    fn health(&mut self, snapshot: TxSnapshot, dropped_audio: u64) {
        let dropped_reports = self.dropped;
        self.submit(Report::Health(Box::new(HealthReport {
            at: SystemTime::now(),
            snapshot,
            dropped_reports,
            dropped_audio,
        })));
    }

    fn log(&mut self, line: String) {
        self.submit(Report::Log(line));
    }
}

impl Drop for Reporter {
    fn drop(&mut self) {
        drop(self.queue.take());
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn report_writer(run_id: u64, health_file: Option<PathBuf>) -> impl FnMut(Report) + Send {
    let mut health_error_at: Option<Instant> = None;
    move |report| match report {
        Report::Log(line) => eprintln!("{line}"),
        Report::Health(report) => {
            if let Some(path) = &health_file
                && let Err(error) = write_health(path, run_id, &report)
                && health_error_at.is_none_or(|at| at.elapsed() >= HEALTH_ERROR_LOG_INTERVAL)
            {
                eprintln!("Radius telemetry unavailable: {error:#}");
                health_error_at = Some(Instant::now());
            }
        }
    }
}

fn gap_seconds(frames: i64) -> f64 {
    frames as f64 / f64::from(protocol::SAMPLE_RATE)
}

fn capture_failure(reason: Option<&str>, stats: &CaptureMetrics) -> String {
    let mut message = format!(
        "Radius audio capture failed: {}",
        reason.unwrap_or("capture timeline invalid")
    );
    if let Some(gap) = stats.last_gap_frames {
        message += &format!(
            " ({} discontinuities, limit {CAPTURE_DISCONTINUITY_LIMIT} within {} s; last gap {:.0} ms)",
            stats.discontinuities,
            CAPTURE_DISCONTINUITY_WINDOW.as_secs(),
            gap_seconds(gap) * 1000.0
        );
    }
    message + "; check the endpoint, RDP audio redirection and receiver connectivity"
}

/// Sequences light and audio onto the connection. Never retains a block, so a
/// network stall cannot back up into the 500 ms capture queue.
struct MediaSender {
    run_id: u64,
    light_sequence: u32,
    audio_sequence: u32,
    transmitted_light: Option<[u8; protocol::DMX_CHANNELS]>,
    audio_dropped: u64,
    burst_dropped: u64,
}

impl MediaSender {
    fn new(run_id: u64) -> Self {
        Self {
            run_id,
            light_sequence: 0,
            audio_sequence: 0,
            transmitted_light: None,
            audio_dropped: 0,
            burst_dropped: 0,
        }
    }

    fn send<S: Read + Write>(
        &mut self,
        connection: &mut Connection<S>,
        block: &AudioBlock,
        light: &[u8; protocol::DMX_CHANNELS],
    ) -> Result<()> {
        // Dropped audio still consumes a sequence number: the receiver detects the gap.
        self.audio_sequence = self.audio_sequence.wrapping_add(1);
        if self.transmitted_light.as_ref() != Some(light) {
            let next = self.light_sequence.wrapping_add(1);
            if !connection.send(&Message::LightFrame(wire::LightFrame {
                run_id: self.run_id,
                sequence_number: next,
                channels: light.to_vec(),
            }))? {
                self.count_dropped(1);
                return Ok(());
            }
            self.light_sequence = next;
            self.transmitted_light = Some(*light);
        }
        let dropped = connection.send_audio(&Message::AudioFrame(wire::AudioFrame {
            run_id: self.run_id,
            audio_frame_position: block.audio_frame_position,
            sequence_number: self.audio_sequence,
            interleaved_samples: block.samples[..usize::from(block.frames) * protocol::CHANNELS]
                .to_vec(),
            light_sequence_number: self.light_sequence,
        }))?;
        self.count_dropped(dropped);
        Ok(())
    }

    fn count_dropped(&mut self, packets: u64) {
        self.audio_dropped += packets;
        self.burst_dropped += packets;
    }

    /// A burst ends once the receiver has taken everything queued.
    fn end_burst<S>(&mut self, connection: &Connection<S>) -> Option<String> {
        if self.burst_dropped == 0 || connection.pending_bytes() != 0 {
            return None;
        }
        let packets = std::mem::take(&mut self.burst_dropped);
        Some(format!(
            "Radius receiver stalled: dropped the oldest {packets} audio packets ({:.0} ms); {} total",
            gap_seconds((packets * protocol::BLOCK_FRAMES as u64) as i64) * 1000.0,
            self.audio_dropped
        ))
    }
}

struct Shared {
    stop: AtomicBool,
    ready: AtomicBool,
    light: ArrayQueue<TimedLight>,
    light_overflow: AtomicBool,
    discard_delayed_audio: AtomicBool,
    metrics: Mutex<TxSnapshot>,
    error: Mutex<Option<String>>,
}

pub struct RadiusTx {
    shared: Arc<Shared>,
    worker: Option<JoinHandle<()>>,
}

impl RadiusTx {
    pub fn start(config: TxConfig) -> Result<Self> {
        config.validate()?;
        let shared = Arc::new(Shared {
            stop: AtomicBool::new(false),
            ready: AtomicBool::new(false),
            light: ArrayQueue::new(MAX_LIGHT_UPDATES),
            light_overflow: AtomicBool::new(false),
            discard_delayed_audio: AtomicBool::new(false),
            metrics: Mutex::new(TxSnapshot::default()),
            error: Mutex::new(None),
        });
        let worker_shared = shared.clone();
        let worker = thread::Builder::new()
            .name("radius-tx".into())
            .spawn(move || {
                if let Err(error) = run(config, &worker_shared) {
                    *worker_shared.error.lock().unwrap() = Some(format!("{error:#}"));
                }
                worker_shared.ready.store(false, Ordering::Release);
                let mut metrics = worker_shared.metrics.lock().unwrap();
                metrics.tx.up = false;
                metrics.tx.link_up = false;
            })?;
        Ok(Self {
            shared,
            worker: Some(worker),
        })
    }

    pub fn update_light(&self, channels: [u8; protocol::DMX_CHANNELS]) {
        if self
            .shared
            .light
            .push(TimedLight {
                channels,
                at: Instant::now(),
            })
            .is_err()
        {
            self.shared.light_overflow.store(true, Ordering::Release);
        }
    }

    pub fn discard_delayed_audio(&self) {
        self.shared
            .discard_delayed_audio
            .store(true, Ordering::Release);
    }

    pub fn is_ready(&self) -> bool {
        self.shared.ready.load(Ordering::Acquire)
    }
    pub fn error(&self) -> Option<String> {
        self.shared.error.lock().unwrap().clone()
    }
    pub fn metrics(&self) -> TxSnapshot {
        self.shared.metrics.lock().unwrap().clone()
    }
}

impl Drop for RadiusTx {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn run(config: TxConfig, shared: &Shared) -> Result<()> {
    pin_worker(config.network_core)?;
    let started = Instant::now();
    let address = config.address()?;
    let stream = loop {
        if shared.stop.load(Ordering::Acquire) {
            return Ok(());
        }
        match TcpStream::connect_timeout(&address, CONNECT_ATTEMPT_TIMEOUT) {
            Ok(stream) => break stream,
            Err(error) if started.elapsed() >= protocol::LOGIN_TIMEOUT => {
                return Err(error).context("Radius receiver connection timed out");
            }
            Err(_) => thread::sleep(CONNECT_ATTEMPT_TIMEOUT),
        }
    };
    let mut connection = Connection::new(stream)?;
    let login = wire::Login {
        command_id: protocol::LOGIN_COMMAND_ID,
        run_id: config.run_id,
        protocol_major: wire::ProtocolVersion::ProtocolMajor as u32,
        protocol_minor: wire::ProtocolVersion::ProtocolMinor as u32,
        sample_rate_hz: protocol::SAMPLE_RATE,
        channel_count: protocol::CHANNELS as u32,
        audio_enabled: config.receiver.audio_enabled,
        lighting_enabled: config.receiver.lighting_enabled,
        jitter_buffer_ms: config.receiver.jitter_buffer_ms,
    };
    ensure!(
        connection.send(&Message::Login(login))?,
        "cannot enqueue Radius Login"
    );
    let mut capture = None;
    let mut capture_started = Instant::now();
    let mut last_heartbeat = Instant::now();
    let mut last_heartbeat_sent = Instant::now();
    let mut heartbeats_sent = 0;
    let mut heartbeats_received = 0;
    let mut receiver_metrics = None;
    let mut receiver_metrics_at = None;
    let mut last_metrics = Instant::now() - protocol::METRICS_INTERVAL;
    let mut media = MediaSender::new(config.run_id);
    let mut timeline = LightTimeline::new();
    let mut light_at = None;
    let mut reporter = Reporter::spawn(report_writer(config.run_id, config.health_file.clone()))?;
    let mut reported_discontinuities = 0;

    while !shared.stop.load(Ordering::Acquire) {
        ensure!(
            !shared.light_overflow.load(Ordering::Acquire),
            "lighting input queue overflowed; synchronization stopped"
        );
        while let Some(light) = shared.light.pop() {
            light_at = Some(light.at);
            ensure!(
                timeline.pending.len() < MAX_LIGHT_UPDATES,
                "lighting history exceeded bounded audio backlog"
            );
            timeline.pending.push_back(light);
        }
        if capture.is_none() {
            timeline.state_at(Instant::now());
        }
        for message in connection.poll()? {
            message.validate(config.run_id)?;
            match message {
                Message::LoginReply(reply) if !shared.ready.load(Ordering::Acquire) => {
                    ensure!(
                        reply == protocol::login_reply(&login),
                        "incompatible Radius LoginReply"
                    );
                    if config.audio_enabled {
                        capture = Some(CaptureStream::start(
                            &config.audio_device_id,
                            config.playback_delay_seconds,
                        )?);
                        capture_started = Instant::now();
                    }
                    shared.ready.store(true, Ordering::Release);
                    last_heartbeat = Instant::now();
                }
                Message::Heartbeat(_) if shared.ready.load(Ordering::Acquire) => {
                    last_heartbeat = Instant::now();
                    heartbeats_received += 1;
                }
                Message::Metrics(metrics) if shared.ready.load(Ordering::Acquire) => {
                    receiver_metrics = Some(metrics);
                    receiver_metrics_at = Some(Instant::now());
                }
                Message::ErrorReply(error) => bail!(
                    "Radius receiver rejected command {}: {}",
                    error.command_id,
                    error.message
                ),
                _ => bail!("unexpected Radius server message"),
            }
        }
        if !shared.ready.load(Ordering::Acquire) {
            ensure!(
                started.elapsed() < protocol::LOGIN_TIMEOUT,
                "Radius Login timed out"
            );
            thread::sleep(POLL_INTERVAL);
            continue;
        }
        ensure!(
            last_heartbeat.elapsed() < protocol::HEARTBEAT_TIMEOUT,
            "Radius receiver heartbeat timed out after 30 seconds"
        );
        if last_heartbeat_sent.elapsed() >= protocol::HEARTBEAT_INTERVAL
            && connection.send(&Message::Heartbeat(wire::Heartbeat {
                run_id: config.run_id,
            }))?
        {
            last_heartbeat_sent = Instant::now();
            heartbeats_sent += 1;
        }

        if let Some(capture) = &capture {
            if shared.discard_delayed_audio.swap(false, Ordering::AcqRel) {
                capture.discard_delayed_audio();
            }
            let stats = capture.metrics();
            ensure!(
                !stats.faulted,
                capture_failure(capture.last_error(), &stats)
            );
            if stats.discontinuities != reported_discontinuities
                && let Some(gap) = stats.last_gap_frames
            {
                let bridge =
                    ["filled with silence", "of repeated audio dropped"][usize::from(gap < 0)];
                reporter.log(format!(
                    "Radius capture resynchronized after a device discontinuity: {:.1} ms {bridge} ({} total)",
                    gap_seconds(gap).abs() * 1000.0,
                    stats.discontinuities
                ));
                reported_discontinuities = stats.discontinuities;
            }
            ensure!(
                capture_started.elapsed() < CALLBACK_STALE_TIMEOUT
                    || stats
                        .last_callback_age
                        .is_some_and(|age| age < CALLBACK_STALE_TIMEOUT),
                "Radius audio capture stopped producing callbacks"
            );
            for _ in 0..MAX_AUDIO_BLOCKS_PER_TICK {
                let Some(block) = capture.pop() else {
                    break;
                };
                let light = timeline.state_at(block.assembly_completed_at);
                media.send(&mut connection, &block, &light)?;
            }
            if let Some(line) = media.end_burst(&connection) {
                reporter.log(line);
            }
        } else {
            timeline.state_at(Instant::now());
        }
        if last_metrics.elapsed() >= protocol::METRICS_INTERVAL {
            let mut tx = wire::TxMetrics {
                up: true,
                link_up: true,
                heartbeat_age_seconds: last_heartbeat.elapsed().as_secs_f64(),
                heartbeats_sent,
                heartbeats_received,
                playback_delay_seconds: config.playback_delay_seconds,
                dmx_input_age_seconds: light_at.map(|at| at.elapsed().as_secs_f64()),
                network_queued_bytes: connection.pending_bytes() as u64,
                ..Default::default()
            };
            if let Some(capture) = &capture {
                let stats = capture.metrics();
                tx.audio_buffer_seconds =
                    stats.queued_frames as f64 / f64::from(protocol::SAMPLE_RATE);
                tx.capture_overruns_total = stats.device_xruns;
                tx.capture_discontinuities_total = stats.discontinuities;
                tx.capture_last_gap_seconds = stats.last_gap_frames.map(gap_seconds);
                tx.capture_priority_denied = stats.capture_priority_denied;
                tx.queue_dropped_frames_total = stats.queue_overflow_frames;
            }
            let snapshot = TxSnapshot {
                tx,
                rx: receiver_metrics,
                receiver_age_seconds: receiver_metrics_at.map(|at| at.elapsed().as_secs_f64()),
            };
            if config.health_file.is_some() {
                reporter.health(snapshot.clone(), media.audio_dropped);
            }
            *shared.metrics.lock().unwrap() = snapshot;
            last_metrics = Instant::now();
        }
        thread::sleep(POLL_INTERVAL);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn queued_audio_keeps_its_capture_time_light_across_backpressure() {
        let at = Instant::now();
        let mut timeline = LightTimeline::new();
        timeline.pending.push_back(TimedLight {
            at,
            channels: [17; protocol::DMX_CHANNELS],
        });
        timeline.pending.push_back(TimedLight {
            at: at + Duration::from_millis(25),
            channels: [99; protocol::DMX_CHANNELS],
        });
        let retained_audio_light = timeline.state_at(at + Duration::from_millis(5));
        let later_audio_light = timeline.state_at(at + Duration::from_millis(30));
        assert_eq!(retained_audio_light, [17; protocol::DMX_CHANNELS]);
        assert_eq!(later_audio_light, [99; protocol::DMX_CHANNELS]);
        assert!(timeline.pending.is_empty());
    }

    /// Refuses the next `refusals` writes with WouldBlock, then accepts everything.
    #[derive(Clone, Default)]
    struct FakeSocket(std::rc::Rc<std::cell::RefCell<(usize, Vec<u8>)>>);

    impl Read for FakeSocket {
        fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::ErrorKind::WouldBlock.into())
        }
    }

    impl Write for FakeSocket {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            let mut state = self.0.borrow_mut();
            if state.0 > 0 {
                state.0 -= 1;
                return Err(std::io::ErrorKind::WouldBlock.into());
            }
            state.1.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    fn frozen_clock() -> Instant {
        static AT: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
        *AT.get_or_init(Instant::now)
    }

    fn decoded(bytes: &[u8]) -> Vec<Message> {
        let mut messages = Vec::new();
        let mut rest = bytes;
        while !rest.is_empty() {
            let length = u32::from_be_bytes(rest[1..5].try_into().unwrap()) as usize;
            let end = protocol::HEADER_BYTES + length;
            messages.push(Message::decode(rest[0], &rest[protocol::HEADER_BYTES..end]).unwrap());
            rest = &rest[end..];
        }
        messages
    }

    #[test]
    fn refused_socket_drops_the_oldest_audio_instead_of_blocking_the_drain() {
        const BLOCKS: u64 = 400;
        let socket = FakeSocket::default();
        socket.0.borrow_mut().0 = 1_000_000;
        let mut connection = Connection::with_stream(socket.clone(), frozen_clock);
        let mut media = MediaSender::new(7);
        let light = [9; protocol::DMX_CHANNELS];
        for index in 0..BLOCKS {
            let block = AudioBlock {
                audio_frame_position: index * protocol::BLOCK_FRAMES as u64,
                frames: protocol::BLOCK_FRAMES as u16,
                ..Default::default()
            };
            media.send(&mut connection, &block, &light).unwrap();
            assert!(connection.pending_bytes() <= crate::transport::MAX_PENDING_BYTES);
        }
        let dropped = media.audio_dropped;
        assert!(dropped > 0 && dropped < BLOCKS, "{dropped} dropped");
        assert_eq!(
            media.end_burst(&connection),
            None,
            "burst still in progress"
        );

        socket.0.borrow_mut().0 = 0;
        connection.flush().unwrap();
        let line = media
            .end_burst(&connection)
            .expect("burst ends when drained");
        assert!(
            line.contains(&format!("oldest {dropped} audio packets")),
            "{line}"
        );
        assert_eq!(media.end_burst(&connection), None, "logged once per burst");

        let messages = decoded(&socket.0.borrow().1);
        assert!(matches!(&messages[0], Message::LightFrame(l) if l.sequence_number == 1));
        let sequences: Vec<u32> = messages[1..]
            .iter()
            .map(|message| match message {
                Message::AudioFrame(audio) => audio.sequence_number,
                other => panic!("unexpected {other:?}"),
            })
            .collect();
        let survivors: Vec<u32> = (dropped as u32 + 1..=BLOCKS as u32).collect();
        assert_eq!(sequences, survivors, "only the oldest audio is dropped");
    }

    #[test]
    fn capture_fault_names_the_discontinuity_limit_and_last_gap() {
        let stats = CaptureMetrics {
            discontinuities: 3,
            last_gap_frames: Some(441),
            ..Default::default()
        };
        assert_eq!(
            capture_failure(Some("capture discontinuities repeated too often"), &stats),
            concat!(
                "Radius audio capture failed: capture discontinuities repeated too often ",
                "(3 discontinuities, limit 3 within 10 s; last gap 10 ms); check the endpoint, ",
                "RDP audio redirection and receiver connectivity"
            )
        );
        assert!(!capture_failure(None, &CaptureMetrics::default()).contains("last gap"));
    }

    #[test]
    fn health_updates_preserve_uint64_values_while_reader_prevents_replacement() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("transport-health.json");
        let report = |snapshot| HealthReport {
            at: SystemTime::now(),
            snapshot,
            dropped_reports: 3,
            dropped_audio: 5,
        };
        write_health(&path, 1, &report(TxSnapshot::default())).unwrap();
        #[cfg(windows)]
        let _reader = {
            use std::os::windows::fs::OpenOptionsExt;
            const FILE_SHARE_READ: u32 = 1;
            const FILE_SHARE_WRITE: u32 = 2;
            std::fs::OpenOptions::new()
                .read(true)
                .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
                .open(&path)
                .unwrap()
        };
        #[cfg(not(windows))]
        let _reader = std::fs::File::open(&path).unwrap();
        let snapshot = TxSnapshot {
            tx: wire::TxMetrics {
                heartbeats_sent: u64::MAX,
                capture_priority_denied: 2,
                ..Default::default()
            },
            rx: Some(wire::Metrics {
                run_id: u64::MAX,
                dmx_frames_total: u64::MAX,
                ..Default::default()
            }),
            receiver_age_seconds: Some(0.25),
        };
        write_health(&path, u64::MAX, &report(snapshot)).unwrap();
        let report: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(report["run_id"].as_u64(), Some(u64::MAX));
        assert_eq!(report["tx"]["heartbeats_sent"].as_u64(), Some(u64::MAX));
        assert_eq!(report["tx"]["capture_priority_denied"].as_u64(), Some(2));
        assert_eq!(report["rx"]["dmx_frames_total"].as_u64(), Some(u64::MAX));
        assert_eq!(report["rx_age_seconds"].as_f64(), Some(0.25));
        assert!(report["timestamp"].as_f64().unwrap() > 0.0);
        assert_eq!(report["reports_dropped_total"].as_u64(), Some(3));
        assert_eq!(report["audio_dropped_total"].as_u64(), Some(5));
    }

    #[test]
    fn stalled_report_writer_never_stalls_the_capture_drain() {
        const BLOCK_PERIOD: Duration = Duration::from_micros(
            1_000_000 * protocol::BLOCK_FRAMES as u64 / protocol::SAMPLE_RATE as u64,
        );
        let capacity = (protocol::SAMPLE_RATE as usize / 2).div_ceil(protocol::BLOCK_FRAMES);
        let capture = Arc::new(ArrayQueue::<AudioBlock>::new(capacity));
        let stop = Arc::new(AtomicBool::new(false));
        let producer = {
            let (capture, stop) = (capture.clone(), stop.clone());
            thread::spawn(move || {
                let mut overflowed = 0;
                while !stop.load(Ordering::Acquire) {
                    overflowed += usize::from(capture.push(AudioBlock::default()).is_err());
                    thread::sleep(BLOCK_PERIOD);
                }
                overflowed
            })
        };
        let written = Arc::new(Mutex::new(Vec::new()));
        let mut reporter = {
            let written = written.clone();
            let mut stalled = false;
            Reporter::spawn(move |report| {
                if !std::mem::replace(&mut stalled, true) {
                    thread::sleep(Duration::from_secs(2));
                }
                if let Report::Health(report) = report {
                    written.lock().unwrap().push(report.dropped_reports);
                }
            })
            .unwrap()
        };
        let started = Instant::now();
        let mut drained = 0;
        let mut slowest_submit = Duration::ZERO;
        while started.elapsed() < Duration::from_millis(2500) {
            while capture.pop().is_some() {
                drained += 1;
            }
            let submitted = Instant::now();
            reporter.health(TxSnapshot::default(), 0);
            reporter.log("discontinuity".into());
            slowest_submit = slowest_submit.max(submitted.elapsed());
            thread::sleep(Duration::from_millis(10));
        }
        stop.store(true, Ordering::Release);
        assert_eq!(producer.join().unwrap(), 0, "capture queue overflowed");
        assert!(drained > 0);
        assert!(
            slowest_submit < Duration::from_millis(50),
            "{slowest_submit:?}"
        );
        assert!(
            reporter.dropped > 100,
            "{} reports dropped",
            reporter.dropped
        );
        thread::sleep(Duration::from_millis(50));
        let dropped = reporter.dropped;
        reporter.health(TxSnapshot::default(), 0);
        drop(reporter);
        // Drops are visible in the health file once the writer recovers.
        assert_eq!(written.lock().unwrap().last(), Some(&dropped));
    }
}
