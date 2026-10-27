use crate::protocol::{HEADER_BYTES, MAX_MESSAGE_BYTES, Message};
use anyhow::{Context, Result, bail, ensure};
use std::{
    collections::VecDeque,
    io::{ErrorKind, Read, Write},
    net::TcpStream,
    time::{Duration, Instant},
};

// About 350 ms of stereo PCM. A stalled peer cannot grow memory without bound.
pub const MAX_PENDING_BYTES: usize = 128 * 1024;
const READ_CHUNK_BYTES: usize = 8192;
const MAX_READ_BYTES_PER_POLL: usize = 64 * 1024;
const STALLED_WRITE_TIMEOUT: Duration = Duration::from_secs(2);

struct Outgoing {
    bytes: Vec<u8>,
    droppable: bool,
}

pub struct Connection<S = TcpStream> {
    stream: S,
    clock: fn() -> Instant,
    incoming: Vec<u8>,
    outgoing: VecDeque<Outgoing>,
    write_offset: usize,
    pending_bytes: usize,
    last_write_progress: Instant,
    read_eof: bool,
    refused_sends: u64,
}

impl Connection {
    pub fn new(stream: TcpStream) -> Result<Self> {
        stream.set_nodelay(true)?;
        stream.set_nonblocking(true)?;
        Ok(Self::with_stream(stream, Instant::now))
    }
}

impl<S> Connection<S> {
    pub fn pending_bytes(&self) -> usize {
        self.pending_bytes
    }

    /// Messages `send` refused under backpressure; evicted audio is counted by the caller.
    pub fn refused_sends(&self) -> u64 {
        self.refused_sends
    }
}

impl<S: Read + Write> Connection<S> {
    /// `stream` must be nonblocking; `clock` times the stalled-write detector.
    pub fn with_stream(stream: S, clock: fn() -> Instant) -> Self {
        Self {
            stream,
            clock,
            incoming: Vec::with_capacity(READ_CHUNK_BYTES + MAX_MESSAGE_BYTES),
            outgoing: VecDeque::new(),
            write_offset: 0,
            pending_bytes: 0,
            last_write_progress: clock(),
            read_eof: false,
            refused_sends: 0,
        }
    }

    /// False means bounded backpressure: the caller retains this message. Unsent
    /// audio queued with `send_audio` is evicted, oldest first, to make room.
    pub fn send(&mut self, message: &Message) -> Result<bool> {
        let bytes = message.encode()?;
        let queued = self.enqueue(bytes, false)?.0;
        self.refused_sends += u64::from(!queued);
        Ok(queued)
    }

    /// Never retains the caller: under backpressure the oldest unsent audio is
    /// dropped instead, this message last. Returns the AudioFrames dropped.
    pub fn send_audio(&mut self, message: &Message) -> Result<u64> {
        let bytes = message.encode()?;
        let (queued, evicted) = self.enqueue(bytes, true)?;
        Ok(evicted + u64::from(!queued))
    }

    fn enqueue(&mut self, bytes: Vec<u8>, droppable: bool) -> Result<(bool, u64)> {
        self.flush()?;
        let mut evicted = 0;
        while self.pending_bytes + bytes.len() > MAX_PENDING_BYTES {
            // A partially written message must finish or the peer loses framing.
            let unsent = usize::from(self.write_offset > 0);
            let Some(index) = self
                .outgoing
                .iter()
                .skip(unsent)
                .position(|entry| entry.droppable)
            else {
                return Ok((false, evicted));
            };
            let entry = self.outgoing.remove(index + unsent).expect("found above");
            self.pending_bytes -= entry.bytes.len();
            evicted += 1;
        }
        if self.pending_bytes == 0 {
            self.last_write_progress = (self.clock)();
        }
        self.pending_bytes += bytes.len();
        self.outgoing.push_back(Outgoing { bytes, droppable });
        self.flush()?;
        Ok((true, evicted))
    }

    pub fn flush(&mut self) -> Result<()> {
        while let Some(Outgoing { bytes, .. }) = self.outgoing.front() {
            match self.stream.write(&bytes[self.write_offset..]) {
                Ok(0) => bail!("Radius peer closed while writing"),
                Ok(count) => {
                    self.write_offset += count;
                    self.pending_bytes -= count;
                    self.last_write_progress = (self.clock)();
                    if self.write_offset == bytes.len() {
                        self.outgoing.pop_front();
                        self.write_offset = 0;
                    }
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(error) => return Err(error).context("Radius TCP write failed"),
            }
        }
        ensure!(
            self.pending_bytes == 0
                || (self.clock)().duration_since(self.last_write_progress) < STALLED_WRITE_TIMEOUT,
            "Radius receiver is not accepting data; TCP backpressure exceeded 2 seconds"
        );
        Ok(())
    }

    pub fn poll(&mut self) -> Result<Vec<Message>> {
        ensure!(!self.read_eof, "Radius TCP connection closed");
        self.flush()?;
        let mut messages = Vec::new();
        let mut chunk = [0; READ_CHUNK_BYTES];
        let mut read_bytes = 0;
        loop {
            self.decode_pending(&mut messages)?;
            if read_bytes >= MAX_READ_BYTES_PER_POLL {
                break;
            }
            match self.stream.read(&mut chunk) {
                Ok(0) => {
                    self.read_eof = true;
                    ensure!(
                        self.incoming.is_empty(),
                        "Radius peer closed with an incomplete message"
                    );
                    ensure!(!messages.is_empty(), "Radius TCP connection closed");
                    break;
                }
                Ok(count) => {
                    self.incoming.extend_from_slice(&chunk[..count]);
                    read_bytes += count;
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(error) => return Err(error).context("Radius TCP read failed"),
            }
        }
        self.decode_pending(&mut messages)?;
        Ok(messages)
    }

    fn decode_pending(&mut self, messages: &mut Vec<Message>) -> Result<()> {
        let mut consumed = 0;
        while self.incoming.len() - consumed >= HEADER_BYTES {
            let header = &self.incoming[consumed..consumed + HEADER_BYTES];
            let length = u32::from_be_bytes(header[1..].try_into()?) as usize;
            ensure!(
                length <= MAX_MESSAGE_BYTES - HEADER_BYTES,
                "Radius frame length exceeds resource bound"
            );
            let end = consumed + HEADER_BYTES + length;
            if self.incoming.len() < end {
                break;
            }
            messages.push(Message::decode(
                header[0],
                &self.incoming[consumed + HEADER_BYTES..end],
            )?);
            consumed = end;
        }
        self.incoming.drain(..consumed);
        Ok(())
    }
}
