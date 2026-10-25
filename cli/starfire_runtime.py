"""Own the live renderer and supervise its bounded protobuf control channel."""

import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from google.protobuf.message import DecodeError

from proto.starfire.codec import encode_control, read_event
from cli.starfire import DEFAULT_BINARY, DEFAULT_FIXTURE_STORE, DEFAULT_RIG, DEFAULT_SEQUENCES, ROOT
from autolux.lighting.starfire_replay import DEFAULT_FRAME_RATE_HZ
from autolux.lib.engine.effect_definitions import SEQUENCE_BY_LIGHT_INTENT

HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_TIMEOUT_SECONDS = 30.0
STARTUP_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0
MAX_PENDING_COMMANDS = 1024
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
    encode_control({'kind': 'heartbeat'})
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
    ):
        self.command = [
            str(Path(binary).resolve()),
            'live',
            '--rig',
            str(Path(rig).resolve()),
            '--sequences',
            str(Path(sequences).resolve()),
            '--artnet',
            artnet,
        ]
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
        self._started_at = None
        self._threads = []
        self._job = None

    def __enter__(self):
        try:
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
            if not self._ready.wait(self.startup_timeout):
                raise RuntimeError('Starfire did not become ready before its startup deadline')
            self.check_health()
            writer = threading.Thread(
                target=self._write_commands, name='starfire-control', daemon=True
            )
            self._threads.append(writer)
            writer.start()
            logging.info('[prod] Starfire ready (PID %d)', self.process.pid)
            return self
        except BaseException:
            self.close()
            raise

    def send_command(self, command):
        self.check_health()
        if self._closing:
            raise RuntimeError('Starfire control channel is closing')
        message = encode_control(command)
        try:
            self._outgoing.put_nowait(message)
        except queue.Full as error:
            self._fault = 'Starfire command queue overflow; stopping the show'
            raise RuntimeError(self._fault) from error

    def _read_events(self):
        try:
            while (event := read_event(self.process.stdout)) is not None:
                kind = event.pop('event')
                if kind == 'error':
                    raise RuntimeError(f'Starfire renderer failed: {event["message"]}')
                if kind == 'ready':
                    if self._ready.is_set() or event['frame_rate_hz'] != DEFAULT_FRAME_RATE_HZ:
                        raise ValueError('Unexpected Starfire readiness event')
                    self._ready.set()
                elif kind == 'health':
                    if not self._ready.is_set():
                        raise ValueError('Starfire sent health before readiness')
                    for field in ('rendered_frames', 'skipped_frames', 'queued_commands'):
                        event[field] = int(event[field])
                    self._health = event
                    self._health_at = time.monotonic()
                    if event.get('script_error'):
                        raise RuntimeError(f'Starfire script failed: {event["script_error"]}')
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
            while not self._closing:
                now = time.monotonic()
                if not self._closing and now >= heartbeat_at:
                    self._write(encode_control({'kind': 'heartbeat'}))
                    heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS
                try:
                    message = self._outgoing.get(timeout=max(0.01, heartbeat_at - time.monotonic()))
                except queue.Empty:
                    continue
                if self._closing or message is None:
                    return
                self._write(message)
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
        last_response = self._health_at if self._health_at is not None else self._started_at
        if time.monotonic() - last_response >= HEARTBEAT_TIMEOUT_SECONDS:
            raise RuntimeError('Starfire heartbeat response timed out')

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
            if len(self._threads) < 2 and child.stdin:
                child.stdin.close()
            try:
                code = child.wait(timeout=self.stop_timeout)
                logging.info('[prod] Starfire stopped (exit %d)', code)
            except subprocess.TimeoutExpired:
                logging.warning('[prod] Starfire shutdown timed out; terminating owned renderer')
                child.kill()
                child.wait(timeout=self.stop_timeout)
            for thread in self._threads:
                thread.join(timeout=self.stop_timeout)
            if child.stdout:
                child.stdout.close()
            self.process = None
        if self._job:
            self._job.close()
            self._job = None

    def __exit__(self, *_):
        self.close()
