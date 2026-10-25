import io
import json
import os
from pathlib import Path
import queue
import socket
import struct
import sys
import threading
import time

import pytest

from cli import starfire_runtime
from proto.starfire import codec


def test_control_codec_preserves_optional_values_and_named_targets():
    encoded = codec.encode_control(
        {
            'kind': 'set_override',
            'value': {
                'id': 'arrival',
                'target': {'group': 'wash'},
                'values': {'intensity': 0.0, 'strobe_hz': 4.0},
                'lifetime': {'kind': 'seconds', 'duration_seconds': 1.0},
                'priority': 10,
            },
        }
    )
    control_type, _ = codec._messages()
    value = control_type.FromString(encoded[codec.LENGTH_PREFIX.size :])
    assert codec.LENGTH_PREFIX.unpack(encoded[:4])[0] == len(encoded) - 4
    assert value.schema_version == codec.SCHEMA_VERSION
    assert value.set_override.target.group == 'wash'
    assert value.set_override.values.HasField('intensity')
    assert value.set_override.values.intensity == 0.0
    assert not value.set_override.values.HasField('pan')
    assert value.set_override.lifetime.seconds == 1.0


@pytest.mark.parametrize(
    'command',
    [
        {'kind': 'set_normalized_rms_level', 'level': float('nan')},
        {'kind': 'set_master', 'intensity': float('inf')},
        {'kind': 'unknown'},
        {'kind': 'select', 'bank': 'drop', 'sequence': 'x', 'extra': 1},
        {
            'kind': 'select',
            'bank': 'drop',
            'sequence': 'x',
            'transition': {'kind': 'cut', 'extra': 1},
        },
    ],
)
def test_invalid_control_cannot_enter_the_pipe(command):
    with pytest.raises(ValueError):
        codec.encode_control(command)


@pytest.mark.parametrize(
    'payload',
    [b'\x00', struct.pack('!I', codec.MAX_MESSAGE_BYTES + 1), struct.pack('!I', 2) + b'\x08'],
)
def test_incomplete_and_oversized_events_are_rejected(payload):
    with pytest.raises(ValueError):
        codec.read_event(io.BytesIO(payload))
    assert codec.read_event(io.BytesIO()) is None


_CHILD = r'''
import json, struct, sys, time
from proto.starfire.codec import _messages, LENGTH_PREFIX
Control, RendererEvent = _messages()
mode, recording = sys.argv[1:]
def event(**value):
    payload = RendererEvent(schema_version=1, **value).SerializeToString()
    sys.stdout.buffer.write(LENGTH_PREFIX.pack(len(payload)) + payload)
    sys.stdout.buffer.flush()
if mode == 'stall':
    time.sleep(30)
if mode == 'malformed':
    sys.stdout.buffer.write(struct.pack('!I', 65537))
    sys.stdout.buffer.flush()
else:
    event(ready={'frame_rate_hz': 40})
commands = []
while prefix := sys.stdin.buffer.read(4):
    value = Control.FromString(sys.stdin.buffer.read(LENGTH_PREFIX.unpack(prefix)[0]))
    commands.append(value.WhichOneof('command'))
    if value.WhichOneof('command') == 'heartbeat' and mode != 'no_health':
        event(health={'rendered_frames': len(commands)})
with open(recording, 'w') as output:
    json.dump(commands, output)
'''


def _fake_runtime(tmp_path, mode='normal', **kwargs):
    runtime = starfire_runtime.StarfireRuntime(
        sys.executable, tmp_path, tmp_path, '127.0.0.1:6454', tmp_path / 'run', **kwargs
    )
    runtime.command = [sys.executable, '-c', _CHILD, mode, str(tmp_path / 'commands.json')]
    return runtime


def test_owned_child_readiness_heartbeats_and_eof_cleanup(tmp_path):
    runtime = _fake_runtime(tmp_path)
    with runtime:
        child = runtime.process
        runtime.send_command({'kind': 'select', 'bank': 'drop', 'sequence': 'blue_impact'})
        runtime.send_command({'kind': 'stop'})
        runtime.send_command({'kind': 'heartbeat'})
        deadline = time.monotonic() + 2
        while (
            runtime.snapshot() is None or runtime.snapshot()['rendered_frames'] < 4
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        runtime.check_health()
        assert runtime.snapshot()['rendered_frames'] == 4
    assert child.poll() == 0
    assert json.loads((tmp_path / 'commands.json').read_text()) == [
        'heartbeat',
        'select',
        'stop',
        'heartbeat',
    ]
    assert all(not thread.is_alive() for thread in runtime._threads)


def test_shutdown_discards_a_full_queue_and_closes_the_pipe_normally(tmp_path):
    runtime = _fake_runtime(tmp_path)
    blocked = threading.Event()
    release = threading.Event()
    with runtime:
        child = runtime.process
        original_write = runtime._write

        def stalled_write(message):
            blocked.set()
            release.wait(2)
            original_write(message)

        runtime._write = stalled_write
        runtime.send_command({'kind': 'stop'})
        assert blocked.wait(2)
        runtime._outgoing = queue.Queue(maxsize=1)
        runtime.send_command({'kind': 'select', 'bank': 'drop', 'sequence': 'blue_impact'})
        threading.Timer(0.05, release.set).start()
        started = time.monotonic()
    assert time.monotonic() - started < 1
    assert child.poll() == 0
    assert 'select' not in json.loads((tmp_path / 'commands.json').read_text())
    assert all(not thread.is_alive() for thread in runtime._threads)


@pytest.mark.parametrize(
    'mode,reason', [('stall', 'startup deadline'), ('malformed', 'event size')]
)
def test_partial_start_failure_reaps_only_owned_renderer(tmp_path, mode, reason):
    runtime = _fake_runtime(tmp_path, mode, startup_timeout=0.5, stop_timeout=0.1)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=reason):
        with runtime:
            pytest.fail('invalid startup became ready')
    assert time.monotonic() - started < 3
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


def test_missing_heartbeat_reply_fails_health_and_shutdown_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_TIMEOUT_SECONDS', 0.25)
    with _fake_runtime(tmp_path, 'no_health') as runtime:
        child = runtime.process
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match='heartbeat response timed out'):
            runtime.check_health()
    assert child.poll() == 0


def test_full_command_queue_fails_instead_of_blocking_audio_thread(tmp_path):
    from unittest.mock import Mock

    runtime = _fake_runtime(tmp_path)
    runtime.process = Mock(poll=lambda: None)
    runtime._started_at = time.monotonic()
    runtime._outgoing = queue.Queue(maxsize=1)
    runtime.send_command({'kind': 'stop'})
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='queue overflow'):
        runtime.send_command({'kind': 'stop'})
    assert time.monotonic() - started < 0.1
    with pytest.raises(RuntimeError, match='queue overflow'):
        runtime.check_health()


def test_real_renderer_accepts_python_protobuf_and_blackouts_on_eof(tmp_path):
    root = Path(__file__).resolve().parents[2]
    binary = root / 'starfire/target/debug/starfire.exe'
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise the native protocol boundary')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(3)
        runtime = starfire_runtime.StarfireRuntime(
            binary,
            root / 'starfire/rigs/room.json',
            root / 'starfire/sequences',
            f'127.0.0.1:{receiver.getsockname()[1]}',
            tmp_path / 'native',
        )
        with runtime:
            runtime.send_command({'kind': 'beat', 'position_beats': 0.0, 'bpm': 120.0})
            runtime.send_command({'kind': 'select', 'bank': 'drop', 'sequence': 'blue_impact'})
            runtime.send_command({'kind': 'set_normalized_rms_level', 'level': 0.8})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                assert packet.startswith(b'Art-Net\x00')
                if any(packet[18:]):
                    break
            else:
                pytest.fail('no rendered lighting arrived')
            runtime.send_command({'kind': 'stop'})
            time.sleep(0.05)
        assert runtime.process is None


def test_production_rejects_multi_universe_patch_but_standalone_keeps_it(tmp_path):
    root = Path(__file__).resolve().parents[2]
    binary = root / 'starfire/target/debug/starfire.exe'
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise native fixture validation')
    patch = json.loads((root / 'starfire/rigs/room.json').read_text())
    patch['fixtures'][0]['universe'] = 1
    rig = tmp_path / 'multi-universe.json'
    rig.write_text(json.dumps(patch))
    sequences = root / 'starfire/sequences'
    with pytest.raises(RuntimeError, match=r'fixture universes \[0, 1\].*Radius TX universe 0'):
        starfire_runtime.validate_starfire(binary, rig, sequences, expected_universe=0)
    starfire_runtime.validate_starfire(binary, rig, sequences)
    starfire_runtime.validate_starfire(
        binary, root / 'starfire/rigs/room.json', sequences, expected_universe=0
    )


def test_preflight_renders_sequences_against_the_selected_patch(tmp_path):
    root = Path(__file__).resolve().parents[2]
    binary = root / 'starfire/target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise native sequence validation')
    patch = json.loads((root / 'starfire/rigs/room.json').read_text())
    del patch['groups']['strip']
    rig = tmp_path / 'missing-strip.json'
    rig.write_text(json.dumps(patch))
    with pytest.raises(RuntimeError, match=r'Starfire validation failed:.*sequence ') as error:
        starfire_runtime.validate_starfire(binary, rig, root / 'starfire/sequences')
    assert 'strip' in str(error.value)


def test_preflight_rejects_valid_catalog_missing_an_autolux_sequence(tmp_path):
    from autolux.lib.engine.effect_definitions import LightIntent, SEQUENCE_BY_LIGHT_INTENT

    root = Path(__file__).resolve().parents[2]
    binary = root / 'starfire/target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise native sequence validation')
    for intent, (bank, sequence) in SEQUENCE_BY_LIGHT_INTENT.items():
        if intent == LightIntent.DROP:
            continue
        directory = tmp_path / bank
        directory.mkdir()
        (directory / f'{sequence}.lua').write_text(
            'return { render = function(ctx) return {} end }'
        )
    with pytest.raises(RuntimeError, match='missing Autolux sequences: drop/blue_impact'):
        starfire_runtime.validate_starfire(binary, root / 'starfire/rigs/room.json', tmp_path)
