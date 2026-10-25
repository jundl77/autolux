"""Own the live renderer and supervise its bounded protobuf control channel."""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from google.protobuf.message import DecodeError

from autolux.lib.engine.effect_definitions import SEQUENCE_BY_LIGHT_INTENT
from autolux.lighting.starfire_replay import DEFAULT_FRAME_RATE_HZ
from cli.starfire import DEFAULT_BINARY, DEFAULT_FIXTURE_STORE, DEFAULT_RIG, DEFAULT_SEQUENCES, ROOT
from cli.starfire_assets import load_login
from cli.starfire_protocol import (
    COMMAND_ID_MAX_ENCODED_BYTES,
    MAX_MESSAGE_BYTES,
    build_command,
    encode_command,
    read_event,
)
from proto.starfire import starfire_pb2 as wire

HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_TIMEOUT_SECONDS = 30.0
STARTUP_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0
MAX_PENDING_COMMANDS = 1024
COMMAND_POLL_INTERVAL_SECONDS = 0.01
MILLISECONDS_PER_SECOND = 1000.0
DEFAULT_ARTNET_DESTINATION = '127.0.0.1:6454'


def add_arguments(parser, *, include_artnet=False):
    parser.add_argument('--starfire-binary', type=Path, default=DEFAULT_BINARY)
    parser.add_argument('--rig', type=Path, help='Fixture patch; defaults to the saved room patch')
    parser.add_argument('--sequences', type=Path, default=DEFAULT_SEQUENCES)
    if include_artnet:
        parser.add_argument('--artnet', default=DEFAULT_ARTNET_DESTINATION)


def selected_rig(path=None):
    return (
        Path(path)
        if path
        else DEFAULT_FIXTURE_STORE
        if DEFAULT_FIXTURE_STORE.is_file()
        else DEFAULT_RIG
    )


def fixture_setup(args):
    return {
        'binary': Path(args.starfire_binary).resolve(),
        'rig_store': Path(args.rig).resolve() if args.rig else DEFAULT_FIXTURE_STORE,
    }


def validate_starfire(binary, rig, sequences, *, expected_universe=None):
    binary = Path(binary).resolve()
    if not binary.is_file():
        raise RuntimeError(f'Starfire binary is missing: {binary}. Run build-starfire.ps1.')
    build_command({'kind': 'heartbeat'})
    result = subprocess.run(
        [
            str(binary),
            'check',
            '--rig',
            str(Path(rig).resolve()),
            '--sequences',
            str(Path(sequences).resolve()),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if result.returncode:
        raise RuntimeError(f'Starfire validation failed: {result.stderr.strip()}')
    report = json.loads(result.stdout)
    checked_sequences = {
        (sequence['bank'], sequence['sequence']) for sequence in report['checked_sequences']
    }
    missing_sequences = set(SEQUENCE_BY_LIGHT_INTENT.values()) - checked_sequences
    if missing_sequences:
        names = ', '.join(f'{bank}/{sequence}' for bank, sequence in sorted(missing_sequences))
        raise RuntimeError(f'Starfire is missing Autolux sequences: {names}')
    if expected_universe is not None:
        universes = report.get('universes')
        if universes != [expected_universe]:
            raise RuntimeError(
                f'Starfire fixture universes {universes} do not match Radius TX universe '
                f'{expected_universe}. Production requires every fixture on that universe.'
            )


class StarfireRuntime:
    def __init__(
        self,
        binary,
        rig,
        sequences,
        artnet,
        run_dir,
        *,
        startup_timeout=STARTUP_TIMEOUT_SECONDS,
        stop_timeout=STOP_TIMEOUT_SECONDS,
        initial_parameters=(),
    ):
        self.command = [str(Path(binary).resolve()), 'live']
        self._rig_path = Path(rig).resolve()
        self._sequence_directory = Path(sequences).resolve()
        self._artnet_destination = artnet
        self._initial_parameters = initial_parameters
        self._login = None
        self._sequence_ids = frozenset()
        self.run_dir = Path(run_dir)
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.process = None
        self._outgoing = queue.Queue(maxsize=MAX_PENDING_COMMANDS)
        self._ready = threading.Event()
        self._closing = False
        self._fault = None
        self._health = None
        self._health_at = None
        self._heartbeat_at = None
        self._started_at = None
        self._threads = []
        self._writer = None
        self._job = None
        self._command_lock = threading.Lock()
        self._last_sent_id = 0
        self._last_processed_id = 0
        self._progress_deadline_at = None

    def __enter__(self):
        try:
            self._login = load_login(
                self._rig_path,
                self._sequence_directory,
                self._artnet_destination,
                parameters=self._initial_parameters,
            )
            self._sequence_ids = frozenset(script.sequence_id for script in self._login.scripts)
            self.run_dir.mkdir(parents=True, exist_ok=False)
            with (self.run_dir / 'process.log').open('wb') as log:
                self.process = subprocess.Popen(
                    self.command,
                    cwd=ROOT,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    bufsize=0,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                )
            if os.name == 'nt':
                from cli.windows_runtime import Job

                self._job = Job()
                self._job.add(self.process.pid)
            self._started_at = time.monotonic()
            reader = threading.Thread(target=self._read_events, name='starfire-events', daemon=True)
            self._threads.append(reader)
            reader.start()
            writer = threading.Thread(
                target=self._write_commands, name='starfire-control', daemon=True
            )
            self._writer = writer
            self._threads.append(writer)
            writer.start()
            if not self._ready.wait(self.startup_timeout):
                raise RuntimeError('Starfire did not become ready before its startup deadline')
            self.check_health()
            logging.info('[prod] Starfire ready (PID %d)', self.process.pid)
            return self
        except BaseException:
            self.close()
            raise

    def send_command(self, command):
        self.check_health()
        if self._closing:
            raise RuntimeError('Starfire control channel is closing')
        message = build_command(command)
        if (
            isinstance(message, wire.PlaySequence)
            and message.sequence_id
            and message.sequence_id not in self._sequence_ids
        ):
            raise ValueError(f'Unknown Starfire sequence: {message.sequence_id}')
        # Leave room for the writer-assigned uint64 ID, including its field tag.
        if message.ByteSize() + COMMAND_ID_MAX_ENCODED_BYTES > MAX_MESSAGE_BYTES:
            raise ValueError('Starfire command exceeds the message limit')
        try:
            self._outgoing.put_nowait(message)
        except queue.Full as error:
            self._fault = 'Starfire command queue overflow; stopping the show'
            raise RuntimeError(self._fault) from error

    def _read_events(self):
        try:
            while (event := read_event(self.process.stdout)) is not None:
                if isinstance(event, wire.ErrorReply):
                    if not 0 < event.command_id <= self._last_sent_id or not event.message:
                        raise ValueError('Invalid Starfire error reply')
                    raise RuntimeError(
                        f'Starfire rejected command {event.command_id}: {event.message}'
                    )
                if isinstance(event, wire.Metrics):
                    metrics = event
                    if metrics.runtime_error:
                        raise RuntimeError(f'Starfire renderer failed: {metrics.runtime_error}')
                    if not self._ready.is_set():
                        raise ValueError('Starfire sent metrics before login')
                    self._record_progress(metrics.last_processed_command_id)
                    self._health = {
                        'elapsed_seconds': metrics.elapsed_seconds,
                        'rendered_frames': metrics.rendered_frames,
                        'skipped_frames': metrics.missed_render_deadlines,
                        'queued_commands': metrics.queued_commands,
                        'last_render_ms': metrics.last_render_duration_seconds
                        * MILLISECONDS_PER_SECOND,
                        'active_bank': metrics.active_bank,
                        'active_sequence': metrics.active_sequence,
                        'script_error': metrics.script_error,
                        'last_processed_command_id': metrics.last_processed_command_id,
                    }
                    self._health_at = time.monotonic()
                    if metrics.script_error:
                        raise RuntimeError(f'Starfire script failed: {metrics.script_error}')
                    continue
                if isinstance(event, wire.Heartbeat):
                    if not self._ready.is_set():
                        raise ValueError('Starfire sent a heartbeat before login')
                    self._heartbeat_at = time.monotonic()
                    continue
                if isinstance(event, wire.LoginReply):
                    if (
                        self._ready.is_set()
                        or self._login is None
                        or event.command_id != self._login.command_id
                        or event.command_id == 0
                        or event.frame_rate_hz != DEFAULT_FRAME_RATE_HZ
                        or event.max_message_bytes != MAX_MESSAGE_BYTES
                    ):
                        raise ValueError('Unexpected Starfire login reply')
                    self._record_progress(event.command_id)
                    self._heartbeat_at = time.monotonic()
                    self._ready.set()
                    continue
            if not self._closing:
                raise RuntimeError('Starfire closed its event channel')
        except (OSError, RuntimeError, ValueError, DecodeError) as error:
            if not self._closing:
                self._fault = str(error)
        finally:
            self._ready.set()

    def _write_commands(self):
        heartbeat_at = 0.0
        try:
            self._write_command(self._login)
            if not self._ready.wait(self.startup_timeout):
                raise RuntimeError('Starfire did not become ready before its startup deadline')
            if self._closing or self._fault:
                return
            while not self._closing:
                now = time.monotonic()
                if not self._closing and now >= heartbeat_at:
                    self._write_command(build_command({'kind': 'heartbeat'}))
                    heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS
                try:
                    message = self._outgoing.get(
                        timeout=max(COMMAND_POLL_INTERVAL_SECONDS, heartbeat_at - time.monotonic())
                    )
                except queue.Empty:
                    continue
                if self._closing or message is None:
                    return
                self._write_command(message)
        except (OSError, RuntimeError, ValueError) as error:
            if not self._closing:
                self._fault = f'Starfire command channel failed: {error}'
        finally:
            self.process.stdin.close()

    def _write(self, message):
        remaining = memoryview(message)
        while remaining:
            written = self.process.stdin.write(remaining)
            if not written:
                raise RuntimeError('Starfire command pipe stopped accepting data')
            remaining = remaining[written:]

    def _write_command(self, message):
        if isinstance(message, wire.Heartbeat):
            self._write(encode_command(message))
            return
        # Only the writer assigns command IDs; heartbeats are independent.
        with self._command_lock:
            message.command_id = self._last_sent_id + 1
            encoded = encode_command(message)
            if self._last_sent_id == self._last_processed_id:
                self._progress_deadline_at = time.monotonic() + HEARTBEAT_TIMEOUT_SECONDS
            self._last_sent_id = message.command_id
        # Register before writing: a fast child may respond during the write itself.
        self._write(encoded)

    def _record_progress(self, command_id):
        with self._command_lock:
            if not self._last_processed_id <= command_id <= self._last_sent_id:
                raise ValueError(f'Invalid Starfire command progress: {command_id}')
            if command_id > self._last_processed_id:
                self._last_processed_id = command_id
                self._progress_deadline_at = (
                    time.monotonic() + HEARTBEAT_TIMEOUT_SECONDS
                    if command_id < self._last_sent_id
                    else None
                )

    def check_health(self):
        if self._fault:
            raise RuntimeError(self._fault)
        if self.process is None:
            raise RuntimeError('Starfire is not started')
        code = self.process.poll()
        if code is not None:
            raise RuntimeError(
                f'Starfire exited with code {code}; see {self.run_dir / "process.log"}'
            )
        last_response = self._heartbeat_at if self._heartbeat_at is not None else self._started_at
        if time.monotonic() - last_response >= HEARTBEAT_TIMEOUT_SECONDS:
            raise RuntimeError('Starfire heartbeat response timed out')
        with self._command_lock:
            if (
                self._progress_deadline_at is not None
                and time.monotonic() >= self._progress_deadline_at
            ):
                raise RuntimeError('Starfire command processing stalled')

    def snapshot(self):
        if self._health is None:
            return None
        return {**self._health, 'age_seconds': time.monotonic() - self._health_at}

    def close(self):
        self._closing = True
        child = self.process
        if child is not None:
            try:
                self._outgoing.put_nowait(None)
            except queue.Full:
                pass
            if (self._writer is None or self._writer.ident is None) and child.stdin:
                child.stdin.close()
            try:
                code = child.wait(timeout=self.stop_timeout)
                logging.info('[prod] Starfire stopped (exit %d)', code)
            except subprocess.TimeoutExpired:
                logging.warning('[prod] Starfire shutdown timed out; terminating owned renderer')
                child.kill()
                child.wait(timeout=self.stop_timeout)
            for thread in self._threads:
                if thread.ident is not None:
                    thread.join(timeout=self.stop_timeout)
            if child.stdout:
                child.stdout.close()
            self.process = None
        if self._job:
            self._job.close()
            self._job = None

    def __exit__(self, *_):
        self.close()
