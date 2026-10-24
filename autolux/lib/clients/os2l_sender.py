import time
import logging
import socket
import json
import datetime
import heapq
import itertools
import codecs
import select
from threading import Thread, Event
from queue import Queue, Empty, Full
import autolux.lib.clients.os2l_messages as os2l_messages
from autolux.lib.analyser.music_analyser import MusicAnalyser
from autolux.lib.clock import Clock, SYSTEM_CLOCK


class Os2lSender:
    def __init__(self, playback_delay_sec: float = 0.0, clock: Clock = SYSTEM_CLOCK):
        self._playback_delay_sec = playback_delay_sec
        self._clock = clock
        self.analyser: MusicAnalyser = None
        self.dest_ipv4_address: str = None
        self.dest_port: int = None
        self.os2l_socket: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sending_thread = Thread(target=self._run_sending_thread, name='os2l', daemon=True)
        self.message_queue: Queue = Queue(maxsize=8192)
        self._pending_messages: list = []
        self._message_sequence = itertools.count()
        self.is_running: bool = False
        self.logon_complete: bool = False
        self._ready = Event()
        self._error = None
        self._receive_buffer = ''
        self._utf8 = codecs.getincrementaldecoder('utf-8')()
        self._receive_started = None

        self.send_update_frequency: datetime.timedelta = datetime.timedelta(milliseconds=25)
        self.last_update_sent: datetime.datetime = self._clock.now()

    def set_analyser(self, analyser: MusicAnalyser):
        self.analyser: MusicAnalyser = analyser

    def start(self, ipv4_address: str, port: int):
        self.dest_ipv4_address = ipv4_address
        self.dest_port = port
        self.is_running = True
        self.sending_thread.start()
        if not self._ready.wait(6):
            self.stop()
            raise RuntimeError('SoundSwitch did not complete its OS2L subscription')
        self.check_health()

    def stop(self):
        self.is_running = False
        if self.sending_thread.ident is not None:
            self.sending_thread.join(timeout=2)
        if self.sending_thread.is_alive():
            self.os2l_socket.close()
            self.sending_thread.join(timeout=2)
        else:
            try:
                if self.logon_complete and self._error is None:
                    self._send_message(os2l_messages.shutdown_message())
            except OSError:
                pass
            finally:
                self.os2l_socket.close()
        self.logon_complete = False

    def check_health(self):
        if self._error is not None:
            raise RuntimeError(f'OS2L connection failed: {self._error}') from self._error
        if self.sending_thread.ident is not None and not self.sending_thread.is_alive():
            raise RuntimeError('OS2L sender stopped unexpectedly')

    def send_message(self, message: str, delay_sec: float = 0.0):
        self.check_health()
        try:
            self.message_queue.put_nowait(
                (self._clock.monotonic() + delay_sec, next(self._message_sequence), message)
            )
        except Full:
            raise RuntimeError('OS2L output queue is full') from None

    def _run_sending_thread(self):
        logging.info(f'[os2l] started sender thread')
        logging.info(f'[os2l] connecting to {self.dest_ipv4_address}:{self.dest_port}')
        server_address = (self.dest_ipv4_address, self.dest_port)
        try:
            self.os2l_socket.settimeout(1.0)
            self.os2l_socket.connect(server_address)
            while self.is_running:
                self._poll_socket()
                self._poll_queue()
                if self.logon_complete:
                    self._send_update_if_due()
                time.sleep(0.001)
        except Exception as error:
            if self.is_running:
                self._error = error
                logging.error('[os2l] sender failed: %s', error)
        finally:
            self.is_running = False
            self._ready.set()

    def _poll_socket(self):
        if self._receive_started is not None and time.monotonic() - self._receive_started > 5:
            raise ValueError('Incomplete or invalid OS2L JSON message')
        if not select.select([self.os2l_socket], [], [], 0)[0]:
            return
        received = self.os2l_socket.recv(4096)
        if not received:
            raise ConnectionError('SoundSwitch closed the OS2L connection')
        if self._receive_started is None:
            self._receive_started = time.monotonic()
        self._receive_buffer += self._utf8.decode(received)
        if len(self._receive_buffer) > 262144:
            raise ValueError('OS2L message exceeds 256 KiB')
        decoder = json.JSONDecoder()
        while self._receive_buffer.strip():
            self._receive_buffer = self._receive_buffer.lstrip()
            try:
                message, end = decoder.raw_decode(self._receive_buffer)
            except json.JSONDecodeError:
                break  # A TCP read may end anywhere inside a JSON object.
            self._receive_buffer = self._receive_buffer[end:]
            self._on_message(message)
        if not self._receive_buffer.strip() and not self._utf8.getstate()[0]:
            self._receive_buffer = ''
            self._receive_started = None

    def _poll_queue(self):
        if not self.logon_complete:
            return
        while True:
            try:
                if len(self._pending_messages) >= 8192:
                    raise RuntimeError('Too many scheduled OS2L messages')
                heapq.heappush(self._pending_messages, self.message_queue.get_nowait())
            except Empty:
                break
        now = self._clock.monotonic()
        while self._pending_messages and self._pending_messages[0][0] <= now:
            _, _, message = heapq.heappop(self._pending_messages)
            self._send_message(message)

    def _send_update_if_due(self):
        now = self._clock.now()
        is_playing = self.analyser.is_song_playing()
        if is_playing and now - self.last_update_sent > self.send_update_frequency:
            beat_position: float = self.analyser.get_beat_position()
            time_elapsed: datetime.timedelta = self.analyser.get_song_current_duration()
            time_elapsed_ms: int = int(time_elapsed.total_seconds() * 1000)
            self.send_message(
                os2l_messages.update_message(beat_position, time_elapsed_ms),
                delay_sec=self._playback_delay_sec,
            )
            self.last_update_sent = now

    def _send_message(self, message: str):
        self.os2l_socket.sendall(message.encode())

    def _on_message(self, json_message):
        if not isinstance(json_message, dict) or 'evt' not in json_message:
            logging.warning('[os2l] message without an event, skipping')
            return

        os2l_event: str = json_message['evt']
        if os2l_event == 'subscribe':
            self._on_subscribe_message(json_message)
        else:
            logging.info(f"[os2l] received message with unknown event '{os2l_event}', skipping")

    def _on_subscribe_message(self, json_message):
        frequency = int(json_message['frequency'])
        if not 1 <= frequency <= 60000:
            raise ValueError('OS2L frequency must be between 1 and 60000 milliseconds')
        self.send_update_frequency = datetime.timedelta(milliseconds=frequency)
        logging.info('[os2l] setting update frequency to %d ms', frequency)
        self._send_message(os2l_messages.logon_message())
        self.logon_complete = True
        self._ready.set()
