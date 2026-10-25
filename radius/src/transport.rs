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

pub struct Connection {
    stream: TcpStream,
    incoming: Vec<u8>,
    outgoing: VecDeque<Vec<u8>>,
    write_offset: usize,
    pending_bytes: usize,
    last_write_progress: Instant,
    read_eof: bool,
}

impl Connection {
    pub fn new(stream: TcpStream) -> Result<Self> {
        stream.set_nodelay(true)?;
        stream.set_nonblocking(true)?;
        Ok(Self {
            stream,
            incoming: Vec::with_capacity(READ_CHUNK_BYTES + MAX_MESSAGE_BYTES),
            outgoing: VecDeque::new(),
            write_offset: 0,
            pending_bytes: 0,
            last_write_progress: Instant::now(),
            read_eof: false,
        })
    }

    /// False means bounded backpressure: the caller retains this message.
    pub fn send(&mut self, message: &Message) -> Result<bool> {
        let bytes = message.encode()?;
        if self.pending_bytes + bytes.len() > MAX_PENDING_BYTES {
            return Ok(false);
        }
        if self.pending_bytes == 0 {
            self.last_write_progress = Instant::now();
        }
        self.pending_bytes += bytes.len();
        self.outgoing.push_back(bytes);
        self.flush()?;
        Ok(true)
    }

    pub fn pending_bytes(&self) -> usize {
        self.pending_bytes
    }

    pub fn flush(&mut self) -> Result<()> {
        while let Some(bytes) = self.outgoing.front() {
            match self.stream.write(&bytes[self.write_offset..]) {
                Ok(0) => bail!("Radius peer closed while writing"),
                Ok(count) => {
                    self.write_offset += count;
                    self.pending_bytes -= count;
                    self.last_write_progress = Instant::now();
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
            self.pending_bytes == 0 || self.last_write_progress.elapsed() < STALLED_WRITE_TIMEOUT,
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
