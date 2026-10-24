import socket
from struct import *


class DmxFrame:
    def __init__(self):
        self.data: list = [0] * 512

    def pack(self):
        msg = pack('B', self.data[0])
        for i in range(1, 512):
            msg += pack('B', self.data[i])
        return msg


class DmxOverlay:
    def __init__(self, start, length):
        self.start = start
        self.length = length

    def pack(self):
        return pack('HH', self.start, self.length)


class DmxOverlays:
    def __init__(self):
        self.overlays: list(DmxOverlays) = [DmxOverlay(0, 0)] * 100
        self.current_index = -1
        self.dmx_frame = DmxFrame()

    def add_overlay(self, start, length, dmx_data):
        assert self.current_index < 100, "full"
        assert start + length <= 512, "too long"
        assert len(dmx_data) == length, "wrong length"
        self.current_index += 1
        self.overlays[self.current_index] = DmxOverlay(start, length)
        for i in range(start, start + length):
            self.dmx_frame.data[i] = dmx_data[i - start]

    def get_num_overlays(self):
        return self.current_index + 1

    def pack(self):
        msg = self.overlays[0].pack()
        for i in range(1, 100):
            msg += self.overlays[i].pack()
        msg += self.dmx_frame.pack()
        return msg


def create_message(universe, overlays: DmxOverlays):
    msg = pack('=IbH', 0x7799, universe, overlays.get_num_overlays())
    msg += overlays.pack()
    return msg


UDP_IP = "127.0.0.1"
UDP_PORT = 19001

overlays = DmxOverlays()
overlays.add_overlay(0, 512, [0] * 512)
#overlays.add_overlay(65, 31, [128] * 31)
#overlays.add_overlay(0, 12, [0] * 12)
MESSAGE = create_message(0, overlays)

print("UDP target IP: %s" % UDP_IP)
print("UDP target port: %s" % UDP_PORT)
print("message: %s" % MESSAGE)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))