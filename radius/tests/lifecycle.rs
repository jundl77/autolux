use radius::{
    RadiusTx, RxConfig, TxConfig,
    config::ReceiverSettings,
    protocol::{self, Message, wire},
    rx::run_receiver,
    transport::Connection,
};
use std::{
    net::{TcpListener, TcpStream},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

fn config(port: u16) -> RxConfig {
    RxConfig {
        peer_address: "127.0.0.1".into(),
        bind_address: "127.0.0.1".into(),
        bus_port: port,
        run_id: 932,
        audio_device_id: String::new(),
        dmx_serial: None,
        audio_enabled: false,
        dmx_enabled: false,
        jitter_buffer_ms: 500,
        sample_rate: 44_100,
        network_core: -1,
        dmx_core: -1,
    }
}

fn connect(port: u16) -> Connection {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match TcpStream::connect(("127.0.0.1", port)) {
            Ok(stream) => return Connection::new(stream).unwrap(),
            Err(_) if Instant::now() < deadline => thread::sleep(Duration::from_millis(5)),
            Err(error) => panic!("receiver did not listen: {error}"),
        }
    }
}

fn login(run_id: u64) -> Message {
    Message::Login(wire::Login {
        command_id: 1,
        run_id,
        protocol_major: wire::ProtocolVersion::ProtocolMajor as u32,
        protocol_minor: 0,
        sample_rate_hz: 44_100,
        channel_count: 2,
        audio_enabled: false,
        lighting_enabled: false,
        jitter_buffer_ms: 500,
    })
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn next_message(connection: &mut Connection) -> Message {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(message) = connection.poll().unwrap().into_iter().next() {
            return message;
        }
        assert!(Instant::now() < deadline, "peer did not respond");
        thread::sleep(Duration::from_millis(1));
    }
}

#[test]
fn receiver_process_announces_listener_ready_and_stops_during_login() {
    let directory = tempfile::tempdir().unwrap();
    let config_path = directory.path().join("receiver.json");
    let receiver_config = config(free_port());
    std::fs::write(&config_path, serde_json::to_vec(&receiver_config).unwrap()).unwrap();
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_radius_rx"))
        .arg(&config_path)
        .env("AUTOLUX_RUNTIME_DIR", directory.path())
        .spawn()
        .unwrap();
    let ready = directory.path().join("ready");
    let deadline = Instant::now() + Duration::from_secs(3);
    while !ready.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let marker = std::fs::read_to_string(&ready);
    let _pending_login = connect(receiver_config.bus_port);
    std::fs::write(directory.path().join("stop"), "stop").unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break Some(status);
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let _ = child.wait();
            break None;
        }
        thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(marker.unwrap().trim(), "ready");
    assert!(status.unwrap().success());
    assert!(!ready.exists());
}

#[test]
fn old_run_cannot_claim_receiver_before_the_owned_run_logs_in() {
    let config = config(free_port());
    let stop = Arc::new(AtomicBool::new(false));
    let worker_stop = stop.clone();
    let worker_config = config.clone();
    let worker = thread::spawn(move || run_receiver(worker_config, worker_stop, None, None));
    let mut old = connect(config.bus_port);
    old.send(&login(config.run_id - 1)).unwrap();
    // The receiver rejects this connection but keeps listening for the owned run.
    assert!(matches!(next_message(&mut old), Message::ErrorReply(_)));
    drop(old);
    let mut current = connect(config.bus_port);
    current.send(&login(config.run_id)).unwrap();
    let Message::LoginReply(reply) = next_message(&mut current) else {
        panic!("owned Login was not accepted");
    };
    assert_eq!(reply.run_id, config.run_id);
    stop.store(true, Ordering::Release);
    assert!(worker.join().unwrap().is_ok());
}

#[test]
fn embedded_tx_and_rx_keep_heartbeats_when_health_files_are_unwritable() {
    let config = config(free_port());
    let stop = Arc::new(AtomicBool::new(false));
    let worker_stop = stop.clone();
    let worker_config = config.clone();
    // A directory cannot be replaced by a report: telemetry failures must not stop transport.
    let unwritable_health_file = tempfile::tempdir().unwrap();
    let receiver_health_file = unwritable_health_file.path().to_path_buf();
    let worker = thread::spawn(move || {
        run_receiver(
            worker_config,
            worker_stop,
            None,
            Some(&receiver_health_file),
        )
    });
    let tx = RadiusTx::start(TxConfig {
        peer_address: "127.0.0.1".into(),
        bus_port: config.bus_port,
        run_id: config.run_id,
        audio_device_id: String::new(),
        health_file: Some(unwritable_health_file.path().to_path_buf()),
        playback_delay_seconds: 0.0,
        audio_enabled: false,
        network_core: -1,
        receiver: ReceiverSettings {
            audio_enabled: false,
            lighting_enabled: false,
            jitter_buffer_ms: 500,
        },
    })
    .unwrap();
    let deadline = Instant::now() + protocol::HEARTBEAT_INTERVAL + Duration::from_secs(3);
    let mut heartbeats = false;
    while Instant::now() < deadline {
        assert!(tx.error().is_none(), "{:?}", tx.error());
        let metrics = tx.metrics();
        if metrics.tx.heartbeats_sent > 0
            && metrics.tx.heartbeats_received > 0
            && metrics
                .rx
                .is_some_and(|rx| rx.heartbeats_received > 0 && rx.heartbeats_sent > 0)
        {
            heartbeats = true;
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    stop.store(true, Ordering::Release);
    assert!(worker.join().unwrap().is_ok());
    drop(tx);
    assert!(
        heartbeats,
        "both endpoints must independently send 10-second heartbeats"
    );
}
