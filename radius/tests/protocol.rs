use radius::{
    protocol::{self, Message, wire},
    rx::MediaReceiver,
    transport::Connection,
};
use std::{
    io::Write,
    net::{TcpListener, TcpStream},
    time::{Duration, Instant},
};

const RUN_ID: u64 = 123;

fn audio(sequence_number: u32, light_sequence_number: u32) -> wire::AudioFrame {
    wire::AudioFrame {
        run_id: RUN_ID,
        audio_frame_position: u64::from(sequence_number) * 128,
        sequence_number,
        interleaved_samples: vec![0.25; 256],
        light_sequence_number,
    }
}

fn light(sequence_number: u32, value: u8) -> wire::LightFrame {
    wire::LightFrame {
        run_id: RUN_ID,
        sequence_number,
        channels: vec![value; 512],
    }
}

#[test]
fn audio_requires_exact_preceding_light_state_and_keeps_a_copy() {
    let mut receiver = MediaReceiver::default();
    assert!(receiver.resolve_audio(audio(0, 1)).is_err());
    receiver.cache_light(light(1, 17)).unwrap();
    assert!(receiver.resolve_audio(audio(0, 2)).is_err());
    let first = receiver.resolve_audio(audio(0, 1)).unwrap().block;
    receiver.cache_light(light(2, 99)).unwrap();
    let second = receiver.resolve_audio(audio(1, 2)).unwrap().block;
    assert_eq!(first.channels, [17; 512]);
    assert_eq!(second.channels, [99; 512]);
    assert_eq!(first.audio.audio_frame_position, 0);
    assert_eq!(second.audio.audio_frame_position, 128);
    assert!(
        receiver.resolve_audio(audio(1, 2)).is_err(),
        "replayed sequence"
    );
}

#[test]
fn audio_sequence_gap_is_filled_with_silence_and_counted() {
    let mut receiver = MediaReceiver::default();
    receiver.cache_light(light(1, 17)).unwrap();
    receiver.resolve_audio(audio(0, 1)).unwrap();
    let admitted = receiver.resolve_audio(audio(4, 1)).unwrap();
    assert_eq!(admitted.silence_frames, 3 * 128);
    assert_eq!(receiver.audio_gaps_total, 1);
    assert_eq!(receiver.lost_frames_total, 3 * 128);
    let blocks: Vec<_> = admitted.blocks().collect();
    let positions: Vec<_> = blocks
        .iter()
        .map(|b| b.audio.audio_frame_position)
        .collect();
    assert_eq!(positions, [128, 256, 384, 512]);
    for silence in &blocks[..3] {
        assert_eq!(silence.audio.frames, 128);
        assert!(silence.audio.samples.iter().all(|&sample| sample == 0.0));
        assert_eq!(silence.channels, [17; 512]);
    }
    assert_eq!(blocks[3].audio.samples[0], 0.25);
    receiver.resolve_audio(audio(5, 1)).unwrap();
    assert_eq!(
        receiver.audio_gaps_total, 1,
        "consecutive audio is not a gap"
    );
}

#[test]
fn audio_gap_must_match_the_dropped_packet_count() {
    let mut receiver = MediaReceiver::default();
    receiver.cache_light(light(1, 17)).unwrap();
    receiver.resolve_audio(audio(0, 1)).unwrap();
    let mut skipped_timeline = audio(1, 1);
    skipped_timeline.audio_frame_position = 128 * 3;
    assert!(receiver.resolve_audio(skipped_timeline).is_err());
    let mut oversized_gap = audio(2, 1);
    oversized_gap.audio_frame_position = 128 * 3;
    assert!(receiver.resolve_audio(oversized_gap).is_err());
    assert_eq!(receiver.audio_gaps_total, 0);
}

#[test]
fn run_id_sample_bounds_and_versions_are_enforced() {
    let mut message = audio(0, 1);
    assert!(
        Message::AudioFrame(message.clone())
            .validate(RUN_ID + 1)
            .is_err()
    );
    message.interleaved_samples[0] = f32::NAN;
    assert!(
        Message::AudioFrame(message.clone())
            .validate(RUN_ID)
            .is_err()
    );
    message.interleaved_samples = vec![0.0; 258];
    assert!(
        Message::AudioFrame(message.clone())
            .validate(RUN_ID)
            .is_err()
    );
    message.interleaved_samples = vec![0.0; 256];
    message.audio_frame_position = u64::MAX;
    assert!(Message::AudioFrame(message).validate(RUN_ID).is_err());
    let mut login = wire::Login {
        command_id: 1,
        run_id: RUN_ID,
        protocol_major: wire::ProtocolVersion::ProtocolMajor as u32,
        protocol_minor: 99,
        sample_rate_hz: 44_100,
        channel_count: 2,
        audio_enabled: true,
        lighting_enabled: true,
        jitter_buffer_ms: 500,
    };
    protocol::validate_login(&login, RUN_ID).unwrap();
    assert_eq!(
        protocol::login_reply(&login).protocol_minor,
        wire::ProtocolVersion::ProtocolMinor as u32
    );
    login.protocol_major += 1;
    assert!(protocol::validate_login(&login, RUN_ID).is_err());
}

#[test]
fn maximum_audio_frame_stays_within_protocol_resource_bound() {
    let message = Message::AudioFrame(audio(u32::MAX, u32::MAX));
    let bytes = message.encode().unwrap();
    assert!(bytes.len() <= protocol::MAX_MESSAGE_BYTES);
    assert_eq!(
        Message::decode(bytes[0], &bytes[protocol::HEADER_BYTES..]).unwrap(),
        message
    );
}

fn connection() -> (TcpStream, Connection) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let (server, _) = listener.accept().unwrap();
    (client, Connection::new(server).unwrap())
}

#[test]
fn tcp_framing_handles_fragmented_and_coalesced_messages() {
    let (mut client, mut server) = connection();
    let first = Message::LightFrame(light(1, 17));
    let second = Message::AudioFrame(audio(0, 1));
    let bytes = first.encode().unwrap();
    client.write_all(&bytes[..3]).unwrap();
    assert!(server.poll().unwrap().is_empty());
    let mut tail = bytes[3..].to_vec();
    tail.extend(second.encode().unwrap());
    client.write_all(&tail).unwrap();
    let deadline = Instant::now() + Duration::from_secs(1);
    let mut received = Vec::new();
    while received.len() < 2 && Instant::now() < deadline {
        received.extend(server.poll().unwrap());
    }
    assert_eq!(received, vec![first, second]);
}

#[test]
fn oversized_tcp_length_is_rejected_before_receiving_payload() {
    let (mut client, mut server) = connection();
    client
        .write_all(&[wire::MessageType::AudioFrame as u8, 0xff, 0xff, 0xff, 0xff])
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(1);
    while Instant::now() < deadline {
        if server.poll().is_err() {
            return;
        }
    }
    panic!("oversized frame was not rejected");
}

#[test]
fn tcp_disconnect_is_a_failure_not_a_silent_healthy_link() {
    let (client, mut server) = connection();
    drop(client);
    let deadline = Instant::now() + Duration::from_secs(1);
    while Instant::now() < deadline {
        if server.poll().is_err() {
            return;
        }
    }
    panic!("disconnect was not observed");
}

#[test]
fn tcp_backpressure_retains_every_accepted_message_in_order() {
    let (client, mut server) = connection();
    let mut accepted = 0;
    let mut blocked = false;
    for sequence in 0..100_000 {
        if !server
            .send(&Message::AudioFrame(audio(sequence, 1)))
            .unwrap()
        {
            blocked = true;
            break;
        }
        accepted += 1;
    }
    assert!(
        blocked,
        "a non-reading peer must eventually exert bounded backpressure"
    );
    assert!(server.pending_bytes() <= radius::transport::MAX_PENDING_BYTES);
    let mut client = Connection::new(client).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut received = 0;
    while received < accepted && Instant::now() < deadline {
        for message in client.poll().unwrap() {
            let Message::AudioFrame(frame) = message else {
                panic!("unexpected message");
            };
            assert_eq!(frame.sequence_number, received);
            received += 1;
        }
        server.flush().unwrap();
    }
    assert_eq!(received, accepted);
}

#[test]
fn final_error_is_delivered_before_connection_close() {
    let (mut client, mut server) = connection();
    let error = Message::ErrorReply(wire::ErrorReply {
        command_id: 0,
        run_id: RUN_ID,
        message: "audio endpoint disconnected".into(),
    });
    client.write_all(&error.encode().unwrap()).unwrap();
    client.shutdown(std::net::Shutdown::Write).unwrap();
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        let messages = server.poll().unwrap();
        if !messages.is_empty() {
            assert_eq!(messages, vec![error]);
            break;
        }
        assert!(Instant::now() < deadline);
    }
    assert!(server.poll().is_err());
}

#[test]
fn truncated_final_message_is_not_treated_as_clean_eof() {
    let (mut client, mut server) = connection();
    let mut bytes = Message::Heartbeat(wire::Heartbeat { run_id: RUN_ID })
        .encode()
        .unwrap();
    bytes.extend_from_slice(&[wire::MessageType::AudioFrame as u8, 0]);
    client.write_all(&bytes).unwrap();
    client.shutdown(std::net::Shutdown::Write).unwrap();
    let deadline = Instant::now() + Duration::from_secs(1);
    while Instant::now() < deadline {
        if server.poll().is_err() {
            return;
        }
    }
    panic!("incomplete final frame was accepted");
}
