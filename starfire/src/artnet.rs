//! Explicit ArtDmx forwarding; sockets stay silent until a frame is sent.

use std::collections::BTreeMap;
use std::net::{SocketAddr, SocketAddrV4, UdpSocket};
use std::time::Duration;

use anyhow::{Context, Result, ensure};

use crate::model::DMX_CHANNELS;

pub const ARTNET_PORT: u16 = 6454;
pub const MAX_ARTNET_UNIVERSE: u16 = 0x7fff;
pub const ARTDMX_HEADER_SIZE: usize = 18;
const ARTNET_ID: &[u8; 8] = b"Art-Net\0";
const ARTDMX_OPCODE: u16 = 0x5000;
const ARTNET_PROTOCOL_VERSION: u16 = 14;
const FIRST_SEQUENCE: u8 = 1;
const MIN_DMX_PAYLOAD: usize = 2;
const UDP_WRITE_TIMEOUT: Duration = Duration::from_millis(100);

/// Serialize ArtDmx per the Artistic Licence specification. Sequence zero disables ordering.
pub fn encode_artdmx(universe: u16, sequence: u8, channels: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        universe <= MAX_ARTNET_UNIVERSE,
        "Art-Net universe exceeds 15-bit port address"
    );
    ensure!(
        (MIN_DMX_PAYLOAD..=DMX_CHANNELS).contains(&channels.len())
            && channels.len().is_multiple_of(2),
        "ArtDmx requires an even payload of 2..512 channels"
    );
    let mut packet = Vec::with_capacity(ARTDMX_HEADER_SIZE + channels.len());
    packet.extend_from_slice(ARTNET_ID);
    packet.extend_from_slice(&ARTDMX_OPCODE.to_le_bytes());
    packet.extend_from_slice(&ARTNET_PROTOCOL_VERSION.to_be_bytes());
    packet.push(sequence);
    packet.push(0); // Physical DMX input port; Starfire is a software controller.
    packet.extend_from_slice(&universe.to_le_bytes());
    packet.extend_from_slice(&(channels.len() as u16).to_be_bytes());
    packet.extend_from_slice(channels);
    Ok(packet)
}

/// One explicitly configured receiver; discovery and scheduling belong to the host.
/// Standard Art-Net source and destination ports are both ARTNET_PORT.
pub struct ArtNetOutput {
    socket: UdpSocket,
    destination: SocketAddrV4,
    sequence: u8,
}

impl ArtNetOutput {
    /// Binding never sends data. Alternate ports support isolated loopback tests.
    pub fn bind(local: SocketAddrV4, destination: SocketAddrV4) -> Result<Self> {
        ensure!(
            !destination.ip().is_broadcast(),
            "use explicit broadcast output for this destination"
        );
        Self::bind_socket(local, destination, false)
    }

    /// Opt into legacy ArtDmx broadcast. Modern Art-Net receivers should use unicast.
    pub fn bind_broadcast(local: SocketAddrV4, destination: SocketAddrV4) -> Result<Self> {
        Self::bind_socket(local, destination, true)
    }

    fn bind_socket(
        local: SocketAddrV4,
        destination: SocketAddrV4,
        broadcast: bool,
    ) -> Result<Self> {
        ensure!(
            destination.port() != 0,
            "Art-Net destination port must be nonzero"
        );
        ensure!(
            !destination.ip().is_unspecified() && !destination.ip().is_multicast(),
            "Art-Net destination must be an explicit IPv4 address"
        );
        let socket =
            UdpSocket::bind(local).with_context(|| format!("bind Art-Net output {local}"))?;
        socket.set_broadcast(broadcast)?;
        socket.set_write_timeout(Some(UDP_WRITE_TIMEOUT))?;
        Ok(Self {
            socket,
            destination,
            sequence: FIRST_SEQUENCE,
        })
    }

    pub fn local_addr(&self) -> Result<SocketAddr> {
        Ok(self.socket.local_addr()?)
    }

    /// Validate all universes before emitting any packet. Universes share a frame sequence.
    pub fn send_frame(&mut self, universes: &BTreeMap<u16, Vec<u8>>) -> Result<()> {
        if universes.is_empty() {
            return Ok(());
        }
        let packets = universes
            .iter()
            .map(|(&universe, channels)| encode_artdmx(universe, self.sequence, channels))
            .collect::<Result<Vec<_>>>()?;
        for packet in packets {
            let sent = self
                .socket
                .send_to(&packet, self.destination)
                .context("send Art-Net frame")?;
            ensure!(sent == packet.len(), "incomplete Art-Net datagram");
        }
        self.sequence = if self.sequence == u8::MAX {
            FIRST_SEQUENCE
        } else {
            self.sequence + 1
        };
        Ok(())
    }
}
