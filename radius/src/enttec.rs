//! ENTTEC USB Pro output through the safe D2XX wrapper; owned by the DMX worker.

use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use libftd2xx::{DeviceInfo, Ftdi, FtdiCommon, list_devices};

const FTDI_VENDOR_ID: u16 = 0x0403;
const USB_PRO_PRODUCT_ID: u16 = 0x6001;
const MESSAGE_START: u8 = 0x7e;
const MESSAGE_END: u8 = 0xe7;
const GET_WIDGET_PARAMETERS: u8 = 3;
const SEND_DMX_OUTPUT: u8 = 6;
const HEADER_BYTES: usize = 4;
const TRAILER_BYTES: usize = 1;
const DMX_CHANNELS: usize = 512;
const DMX_START_CODE: u8 = 0;
const DMX_PAYLOAD_BYTES: usize = DMX_CHANNELS + 1;
const DMX_FRAME_BYTES: usize = HEADER_BYTES + DMX_PAYLOAD_BYTES + TRAILER_BYTES;
const MAX_WIDGET_PAYLOAD_BYTES: usize = 600;
const WIDGET_PARAMETERS_BYTES: usize = 5;
const MAX_PREAMBLE_BYTES: usize = 64;
const MAX_UNRELATED_MESSAGES: usize = 8;
const USB_IO_TIMEOUT: Duration = Duration::from_millis(100);
const WIDGET_REPLY_TIMEOUT: Duration = Duration::from_millis(500);
const MIN_BREAK_UNITS: u8 = 9;
const MAX_BREAK_UNITS: u8 = 127;
const MIN_MARK_AFTER_BREAK_UNITS: u8 = 1;
const MAX_MARK_AFTER_BREAK_UNITS: u8 = 127;
const MAX_DMX_RATE_HZ: u8 = 40;
const DMX_FIRMWARE: u8 = 1;
const RDM_FIRMWARE: u8 = 2;
const PARAMETERS_REQUEST: [u8; HEADER_BYTES + 2 + TRAILER_BYTES] = [
    MESSAGE_START,
    GET_WIDGET_PARAMETERS,
    2,
    0,
    0,
    0,
    MESSAGE_END,
];

/// One identified USB Pro. Construction establishes darkness before accepting frames.
pub struct EnttecOutput {
    widget: Widget<Ftdi>,
    serial_number: String,
}

impl EnttecOutput {
    pub fn open(serial: Option<&str>) -> Result<Self> {
        ensure!(serial != Some(""), "ENTTEC serial number must not be empty");
        let devices = list_devices().context("enumerate FTDI devices")?;
        let device = select_device(&devices, serial)?;
        ensure!(
            !device.port_open,
            "ENTTEC {} is already open; close its other controller",
            device.serial_number
        );
        ensure!(
            !device.serial_number.is_empty(),
            "ENTTEC adapter has no FTDI serial number"
        );
        let mut port = Ftdi::with_serial_number(&device.serial_number)
            .with_context(|| format!("open ENTTEC {}", device.serial_number))?;
        port.set_timeouts(USB_IO_TIMEOUT, USB_IO_TIMEOUT)
            .context("set bounded ENTTEC USB timeouts")?;
        port.purge_rx()
            .context("discard stale ENTTEC receive data")?;
        let widget = Widget::initialize(port)
            .with_context(|| format!("identify ENTTEC USB Pro {}", device.serial_number))?;
        Ok(Self {
            widget,
            serial_number: device.serial_number.clone(),
        })
    }

    pub fn serial_number(&self) -> &str {
        &self.serial_number
    }

    /// Called on the DMX worker, never from an audio callback.
    pub fn write_frame(&mut self, channels: &[u8; DMX_CHANNELS]) -> Result<()> {
        self.widget.write_frame(channels)
    }
}

fn select_device<'a>(devices: &'a [DeviceInfo], serial: Option<&str>) -> Result<&'a DeviceInfo> {
    let mut candidates = devices.iter().filter(|device| {
        device.vendor_id == FTDI_VENDOR_ID
            && device.product_id == USB_PRO_PRODUCT_ID
            && serial.is_none_or(|requested| requested == device.serial_number)
    });
    let device = candidates
        .next()
        .context("no matching ENTTEC USB Pro (FTDI 0403:6001); check its connection and serial")?;
    ensure!(
        candidates.next().is_none(),
        "multiple FTDI 0403:6001 adapters found; select the ENTTEC by serial number"
    );
    Ok(device)
}

trait UsbIo {
    fn read(&mut self, bytes: &mut [u8]) -> Result<usize>;
    fn write(&mut self, bytes: &[u8]) -> Result<usize>;
}

impl UsbIo for Ftdi {
    fn read(&mut self, bytes: &mut [u8]) -> Result<usize> {
        FtdiCommon::read(self, bytes).context("read ENTTEC USB")
    }

    fn write(&mut self, bytes: &[u8]) -> Result<usize> {
        FtdiCommon::write(self, bytes).context("write ENTTEC USB")
    }
}

struct Widget<T: UsbIo> {
    port: T,
    identified: bool,
    failed: bool,
}

impl<T: UsbIo> Widget<T> {
    fn initialize(port: T) -> Result<Self> {
        let mut widget = Self {
            port,
            identified: false,
            failed: false,
        };
        write_exact(&mut widget.port, &PARAMETERS_REQUEST)?;
        let parameters = read_parameters(&mut widget.port)?;
        ensure!(
            matches!(parameters[1], DMX_FIRMWARE | RDM_FIRMWARE),
            "USB widget firmware does not support DMX output"
        );
        ensure!(
            (MIN_BREAK_UNITS..=MAX_BREAK_UNITS).contains(&parameters[2]),
            "invalid USB Pro break time"
        );
        ensure!(
            (MIN_MARK_AFTER_BREAK_UNITS..=MAX_MARK_AFTER_BREAK_UNITS).contains(&parameters[3]),
            "invalid USB Pro mark-after-break time"
        );
        // The API permits zero for the fastest refresh rate in Set Widget Parameters.
        ensure!(
            parameters[4] <= MAX_DMX_RATE_HZ,
            "invalid USB Pro refresh rate"
        );
        widget.identified = true;
        widget.write_frame(&[0; DMX_CHANNELS])?;
        Ok(widget)
    }

    fn write_frame(&mut self, channels: &[u8; DMX_CHANNELS]) -> Result<()> {
        ensure!(
            !self.failed,
            "ENTTEC output failed; reopen the device before sending more frames"
        );
        let result = write_exact(&mut self.port, &frame_packet(channels));
        self.failed = result.is_err();
        result
    }
}

impl<T: UsbIo> Drop for Widget<T> {
    fn drop(&mut self) {
        if self.identified {
            // A disconnected device may not accept darkness; never block teardown indefinitely.
            let _ = write_exact(&mut self.port, &frame_packet(&[0; DMX_CHANNELS]));
        }
        // The owned Ftdi closes its handle after this destructor, including initialization errors.
    }
}

fn frame_packet(channels: &[u8; DMX_CHANNELS]) -> [u8; DMX_FRAME_BYTES] {
    let mut packet = [0; DMX_FRAME_BYTES];
    packet[0] = MESSAGE_START;
    packet[1] = SEND_DMX_OUTPUT;
    packet[2..HEADER_BYTES].copy_from_slice(&(DMX_PAYLOAD_BYTES as u16).to_le_bytes());
    packet[HEADER_BYTES] = DMX_START_CODE;
    packet[HEADER_BYTES + 1..DMX_FRAME_BYTES - TRAILER_BYTES].copy_from_slice(channels);
    packet[DMX_FRAME_BYTES - TRAILER_BYTES] = MESSAGE_END;
    packet
}

fn write_exact(port: &mut impl UsbIo, mut bytes: &[u8]) -> Result<()> {
    let deadline = Instant::now() + USB_IO_TIMEOUT;
    while !bytes.is_empty() {
        ensure!(Instant::now() < deadline, "ENTTEC write deadline exceeded");
        let written = port.write(bytes)?;
        ensure!(
            written != 0 && written <= bytes.len(),
            "ENTTEC write made no valid progress"
        );
        bytes = &bytes[written..];
    }
    Ok(())
}

fn read_exact(port: &mut impl UsbIo, mut bytes: &mut [u8], deadline: Instant) -> Result<()> {
    while !bytes.is_empty() {
        ensure!(Instant::now() < deadline, "ENTTEC reply deadline exceeded");
        let read = port.read(bytes)?;
        ensure!(
            read != 0 && read <= bytes.len(),
            "ENTTEC read timed out or returned an invalid length"
        );
        bytes = &mut bytes[read..];
    }
    Ok(())
}

fn read_parameters(port: &mut impl UsbIo) -> Result<[u8; WIDGET_PARAMETERS_BYTES]> {
    let deadline = Instant::now() + WIDGET_REPLY_TIMEOUT;
    for _ in 0..MAX_UNRELATED_MESSAGES {
        let mut marker = [0];
        for _ in 0..MAX_PREAMBLE_BYTES {
            read_exact(port, &mut marker, deadline)?;
            if marker[0] == MESSAGE_START {
                break;
            }
        }
        ensure!(
            marker[0] == MESSAGE_START,
            "ENTTEC reply has no message start"
        );
        let mut header = [0; HEADER_BYTES - 1];
        read_exact(port, &mut header, deadline)?;
        let length = usize::from(u16::from_le_bytes([header[1], header[2]]));
        ensure!(
            length <= MAX_WIDGET_PAYLOAD_BYTES,
            "ENTTEC reply exceeds maximum payload size"
        );
        let mut payload = [0; MAX_WIDGET_PAYLOAD_BYTES];
        read_exact(port, &mut payload[..length], deadline)?;
        read_exact(port, &mut marker, deadline)?;
        ensure!(marker[0] == MESSAGE_END, "ENTTEC reply has no message end");
        if header[0] == GET_WIDGET_PARAMETERS {
            ensure!(
                length == WIDGET_PARAMETERS_BYTES,
                "ENTTEC parameter reply has an invalid length"
            );
            return Ok(payload[..WIDGET_PARAMETERS_BYTES]
                .try_into()
                .expect("checked parameter size"));
        }
    }
    bail!("ENTTEC parameter reply was not received within the message limit")
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::rc::Rc;

    use super::*;

    #[derive(Default)]
    struct Trace {
        output: Vec<u8>,
        writes: usize,
        dropped: bool,
        fail_write: bool,
        fail_on_write: Option<usize>,
    }

    struct MockUsb {
        incoming: VecDeque<u8>,
        chunk: usize,
        trace: Rc<RefCell<Trace>>,
    }

    impl Drop for MockUsb {
        fn drop(&mut self) {
            self.trace.borrow_mut().dropped = true;
        }
    }

    impl UsbIo for MockUsb {
        fn read(&mut self, bytes: &mut [u8]) -> Result<usize> {
            let count = bytes.len().min(self.chunk).min(self.incoming.len());
            for byte in &mut bytes[..count] {
                *byte = self.incoming.pop_front().unwrap();
            }
            Ok(count)
        }

        fn write(&mut self, bytes: &[u8]) -> Result<usize> {
            let mut trace = self.trace.borrow_mut();
            trace.writes += 1;
            ensure!(
                !trace.fail_write && trace.fail_on_write != Some(trace.writes),
                "injected USB failure"
            );
            let count = bytes.len().min(self.chunk);
            trace.output.extend_from_slice(&bytes[..count]);
            Ok(count)
        }
    }

    fn reply(label: u8, payload: &[u8]) -> Vec<u8> {
        let mut bytes = vec![MESSAGE_START, label];
        bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
        bytes.extend_from_slice(payload);
        bytes.push(MESSAGE_END);
        bytes
    }

    fn mock(incoming: Vec<u8>, chunk: usize) -> (MockUsb, Rc<RefCell<Trace>>) {
        let trace = Rc::new(RefCell::new(Trace::default()));
        (
            MockUsb {
                incoming: incoming.into(),
                chunk,
                trace: trace.clone(),
            },
            trace,
        )
    }

    fn valid_reply() -> Vec<u8> {
        reply(GET_WIDGET_PARAMETERS, &[44, DMX_FIRMWARE, 9, 1, 40])
    }

    #[test]
    fn partial_io_preserves_exact_dmx_framing_and_drop_darkens_before_closing() {
        let (port, trace) = mock(valid_reply(), 3);
        let mut widget = Widget::initialize(port).unwrap();
        let channels = std::array::from_fn(|index| index as u8);
        widget.write_frame(&channels).unwrap();
        assert!(!trace.borrow().dropped);
        drop(widget);
        let trace = trace.borrow();
        assert!(trace.dropped);
        let mut expected = PARAMETERS_REQUEST.to_vec();
        expected.extend_from_slice(&frame_packet(&[0; DMX_CHANNELS]));
        expected.extend_from_slice(&frame_packet(&channels));
        expected.extend_from_slice(&frame_packet(&[0; DMX_CHANNELS]));
        assert_eq!(trace.output, expected);
        let packet = frame_packet(&channels);
        assert_eq!(&packet[..5], &[0x7e, 6, 1, 2, 0]);
        assert_eq!(&packet[5..517], &channels);
        assert_eq!(packet[517], 0xe7);
    }

    #[test]
    fn invalid_widgets_close_without_emitting_dmx() {
        for input in [
            vec![],
            vec![0; MAX_PREAMBLE_BYTES],
            vec![MESSAGE_START, GET_WIDGET_PARAMETERS, 255, 255],
            reply(GET_WIDGET_PARAMETERS, &[44, 1, 9, 1]),
            reply(GET_WIDGET_PARAMETERS, &[44, 3, 9, 1, 40]),
            reply(GET_WIDGET_PARAMETERS, &[44, 1, 8, 1, 40]),
            reply(GET_WIDGET_PARAMETERS, &[44, 1, 9, 0, 40]),
            reply(GET_WIDGET_PARAMETERS, &[44, 1, 9, 1, 41]),
            [valid_reply()[..valid_reply().len() - 1].to_vec(), vec![0]].concat(),
            valid_reply()[..valid_reply().len() - 1].to_vec(),
        ] {
            let (port, trace) = mock(input, 2);
            assert!(Widget::initialize(port).is_err());
            assert_eq!(trace.borrow().output, PARAMETERS_REQUEST);
            assert!(trace.borrow().dropped);
        }
    }

    #[test]
    fn initial_blackout_failure_releases_the_identified_device() {
        let (port, trace) = mock(valid_reply(), DMX_FRAME_BYTES);
        trace.borrow_mut().fail_on_write = Some(2);
        assert!(Widget::initialize(port).is_err());
        assert!(trace.borrow().dropped);
        assert_eq!(trace.borrow().writes, 3);
        let mut expected = PARAMETERS_REQUEST.to_vec();
        expected.extend_from_slice(&frame_packet(&[0; DMX_CHANNELS]));
        assert_eq!(trace.borrow().output, expected);
    }

    #[test]
    fn documented_fastest_refresh_and_rdm_firmware_support_output() {
        let (port, trace) = mock(
            reply(GET_WIDGET_PARAMETERS, &[44, RDM_FIRMWARE, 9, 1, 0]),
            DMX_FRAME_BYTES,
        );
        let widget = Widget::initialize(port).unwrap();
        drop(widget);
        assert!(trace.borrow().dropped);
        assert_eq!(
            trace.borrow().output.len(),
            PARAMETERS_REQUEST.len() + 2 * DMX_FRAME_BYTES
        );
    }

    #[test]
    fn unsolicited_packets_are_skipped_but_noise_cannot_extend_read_forever() {
        let input = [vec![0, 1, 2], reply(5, &[0, 0, 1, 2]), valid_reply()].concat();
        let (mut port, _) = mock(input, 1);
        assert_eq!(read_parameters(&mut port).unwrap(), [44, 1, 9, 1, 40]);
        let input = reply(5, &[0]).repeat(MAX_UNRELATED_MESSAGES + 1);
        let (mut port, _) = mock(input, 1);
        assert!(read_parameters(&mut port).is_err());
        assert!(!port.incoming.is_empty());
    }

    #[test]
    fn write_failure_is_latched_and_drop_still_releases_the_port() {
        let (port, trace) = mock(valid_reply(), DMX_FRAME_BYTES);
        let mut widget = Widget::initialize(port).unwrap();
        trace.borrow_mut().fail_write = true;
        assert!(widget.write_frame(&[1; DMX_CHANNELS]).is_err());
        let writes = trace.borrow().writes;
        assert!(widget.write_frame(&[2; DMX_CHANNELS]).is_err());
        assert_eq!(trace.borrow().writes, writes);
        drop(widget);
        assert_eq!(trace.borrow().writes, writes + 1);
        assert!(trace.borrow().dropped);
    }

    #[test]
    fn expired_deadline_and_zero_progress_do_not_spin() {
        let (mut port, trace) = mock(valid_reply(), 0);
        assert!(read_exact(&mut port, &mut [0], Instant::now()).is_err());
        assert!(write_exact(&mut port, &[1]).is_err());
        assert_eq!(trace.borrow().writes, 1);
    }

    #[test]
    fn device_selection_ignores_other_ftdi_ids_and_requires_an_unambiguous_serial() {
        fn info(serial: &str, product_id: u16) -> DeviceInfo {
            DeviceInfo {
                port_open: false,
                speed: None,
                device_type: libftd2xx::DeviceType::Unknown,
                vendor_id: FTDI_VENDOR_ID,
                product_id,
                serial_number: serial.into(),
                description: String::new(),
            }
        }
        let devices = [
            info("other", 0x6014),
            info("first", USB_PRO_PRODUCT_ID),
            info("second", USB_PRO_PRODUCT_ID),
        ];
        assert!(select_device(&devices, None).is_err());
        assert_eq!(
            select_device(&devices, Some("second"))
                .unwrap()
                .serial_number,
            "second"
        );
        assert!(select_device(&devices, Some("other")).is_err());
        assert!(select_device(&devices, Some("missing")).is_err());
        assert_eq!(
            select_device(&devices[..2], None).unwrap().serial_number,
            "first"
        );
    }
}
