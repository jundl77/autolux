use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};
use std::time::Duration;

use starfire::artnet::{ARTDMX_HEADER_SIZE, ARTNET_PORT, ArtNetOutput, encode_artdmx};
use starfire::model::DMX_CHANNELS;

const TEST_RECEIVE_TIMEOUT: Duration = Duration::from_millis(100);

fn loopback_receiver() -> UdpSocket {
    let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    socket.set_read_timeout(Some(TEST_RECEIVE_TIMEOUT)).unwrap();
    socket
}

fn address(socket: &UdpSocket) -> SocketAddrV4 {
    match socket.local_addr().unwrap() {
        SocketAddr::V4(address) => address,
        SocketAddr::V6(_) => unreachable!(),
    }
}

fn frame() -> BTreeMap<u16, Vec<u8>> {
    BTreeMap::from([(0, vec![7; DMX_CHANNELS])])
}

fn assert_silent(socket: &UdpSocket) {
    let error = socket.recv_from(&mut [0; 1024]).unwrap_err();
    assert!(matches!(
        error.kind(),
        ErrorKind::WouldBlock | ErrorKind::TimedOut
    ));
}

#[test]
fn artdmx_header_uses_protocol_endian_order_and_full_channel_payload() {
    let channels = (0..DMX_CHANNELS)
        .map(|value| (value % 256) as u8)
        .collect::<Vec<_>>();
    let packet = encode_artdmx(0x1234, 255, &channels).unwrap();
    assert_eq!(ARTNET_PORT, 6454);
    assert_eq!(packet.len(), ARTDMX_HEADER_SIZE + DMX_CHANNELS);
    assert_eq!(
        &packet[..ARTDMX_HEADER_SIZE],
        &[
            b'A', b'r', b't', b'-', b'N', b'e', b't', 0, 0x00, 0x50, 0x00, 0x0e, 255, 0, 0x34,
            0x12, 0x02, 0x00
        ]
    );
    assert_eq!(&packet[ARTDMX_HEADER_SIZE..], channels);
    assert_eq!(encode_artdmx(0, 0, &[0, 255]).unwrap()[12], 0);
    assert!(encode_artdmx(32767, 1, &[0, 0]).is_ok());
}

#[test]
fn invalid_universes_and_payload_lengths_never_serialize() {
    for length in [0, 1, 3, 511, 513, 514] {
        assert!(encode_artdmx(0, 1, &vec![0; length]).is_err());
    }
    assert!(encode_artdmx(32768, 1, &[0, 0]).is_err());
}

#[test]
fn explicit_loopback_output_is_silent_until_send_and_uses_selected_ports() {
    let receiver = loopback_receiver();
    let mut output = ArtNetOutput::bind(
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0),
        address(&receiver),
    )
    .unwrap();
    assert_silent(&receiver);
    output.send_frame(&frame()).unwrap();
    let mut packet = [0; 1024];
    let (length, source) = receiver.recv_from(&mut packet).unwrap();
    assert_eq!(source, output.local_addr().unwrap());
    assert_eq!(length, ARTDMX_HEADER_SIZE + DMX_CHANNELS);
    assert_eq!(
        &packet[..length],
        encode_artdmx(0, 1, &vec![7; DMX_CHANNELS]).unwrap()
    );
}

#[test]
fn sequences_wrap_without_zero_and_share_one_number_across_universes() {
    let receiver = loopback_receiver();
    let mut output = ArtNetOutput::bind(
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0),
        address(&receiver),
    )
    .unwrap();
    let frames = BTreeMap::from([(0, vec![1, 2]), (257, vec![3, 4])]);
    for frame_number in 0..257 {
        output.send_frame(&frames).unwrap();
        for universe in [0u16, 257] {
            let mut packet = [0; 1024];
            let (length, _) = receiver.recv_from(&mut packet).unwrap();
            assert_eq!(length, ARTDMX_HEADER_SIZE + 2);
            assert_eq!(packet[12], (frame_number % 255 + 1) as u8);
            assert_eq!(u16::from_le_bytes([packet[14], packet[15]]), universe);
        }
    }
}

#[test]
fn invalid_later_universe_does_not_partially_emit_or_advance_sequence() {
    let receiver = loopback_receiver();
    let mut output = ArtNetOutput::bind(
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0),
        address(&receiver),
    )
    .unwrap();
    let invalid = BTreeMap::from([(0, vec![1, 2]), (1, vec![3])]);
    assert!(output.send_frame(&invalid).is_err());
    output.send_frame(&BTreeMap::new()).unwrap();
    assert_silent(&receiver);
    output.send_frame(&frame()).unwrap();
    let mut packet = [0; 1024];
    receiver.recv_from(&mut packet).unwrap();
    assert_eq!(packet[12], 1);
}

#[test]
fn output_requires_a_concrete_destination_and_broadcast_is_explicit() {
    let local = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0);
    for destination in [
        SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, ARTNET_PORT),
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0),
        SocketAddrV4::new(Ipv4Addr::BROADCAST, ARTNET_PORT),
        SocketAddrV4::new(Ipv4Addr::new(239, 0, 0, 1), ARTNET_PORT),
    ] {
        assert!(ArtNetOutput::bind(local, destination).is_err());
    }
    let receiver = loopback_receiver();
    let mut output = ArtNetOutput::bind_broadcast(local, address(&receiver)).unwrap();
    assert_silent(&receiver);
    output.send_frame(&frame()).unwrap();
    receiver.recv_from(&mut [0; 1024]).unwrap();
}
