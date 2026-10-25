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

from cli import starfire_protocol as protocol, starfire_runtime
from proto.starfire import starfire_pb2 as wire


def test_control_adapter_preserves_zero_values_and_named_targets():
    command = protocol.build_command(
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
    command.command_id = 1
    encoded = protocol.encode_command(command)
    value = wire.RendererCommand.FromString(encoded[protocol.LENGTH_PREFIX.size :])
    assert protocol.LENGTH_PREFIX.unpack(encoded[:4])[0] == len(encoded) - 4
    assert value.command_id == 1
    effect = value.set_active_effect
    assert effect.fixtures.group_id == 'wash'
    attributes = {item.attribute: item for item in effect.attributes}
    assert attributes[wire.INTENSITY].value.WhichOneof('value') == 'number'
    assert attributes[wire.INTENSITY].value.number == 0.0
    assert wire.PAN not in attributes
    assert effect.lifetime.duration_seconds == 1.0


@pytest.mark.parametrize(
    'command',
    [
        {'kind': 'set_normalized_rms_level', 'level': float('nan')},
        {'kind': 'set_master', 'intensity': float('inf')},
        {'kind': 'unknown'},
        {'kind': 'stop', 'sequence': {'bank_id': 'drop', 'sequence_id': 'blue_impact'}},
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
        protocol.build_command(command)


@pytest.mark.parametrize(
    'payload',
    [b'\x00', struct.pack('!I', protocol.MAX_MESSAGE_BYTES + 1), struct.pack('!I', 2) + b'\x08'],
)
def test_incomplete_and_oversized_events_are_rejected(payload):
    with pytest.raises(ValueError):
        protocol.read_event(io.BytesIO(payload))
    assert protocol.read_event(io.BytesIO()) is None


@pytest.mark.parametrize(
    'kind,field,parameter,value',
    [
        ('set_master', 'intensity', 'output_brightness', 0.0),
        ('set_speed', 'multiplier', 'animation_speed', 0.0),
        ('set_blackout', 'enabled', 'output_blackout', False),
        ('set_normalized_rms_level', 'level', 'audio_rms_normalized', 0.0),
    ],
)
def test_controls_use_typed_parameter_variants(kind, field, parameter, value):
    command = protocol.build_command({'kind': kind, field: value})
    assert command.WhichOneof('command') == 'set_parameter'
    setting = command.set_parameter
    assert setting.WhichOneof('parameter') == parameter
    assert getattr(setting, parameter) == value


def test_typed_commands_are_copied_before_queueing():
    command = wire.RendererCommand(
        command_id=93,
        set_parameter=wire.SetParameter(
            sequence_parameter=wire.SequenceParameter(
                sequence=wire.SequenceReference(bank_id='drop', sequence_id='blue_impact'),
                name='pulse_decay_beats',
                value=wire.ParameterValue(number=0.4),
            )
        ),
    )
    copied = protocol.build_command(command)
    command.set_parameter.sequence_parameter.value.number = 0.9
    assert copied.command_id == 0
    assert copied.set_parameter.sequence_parameter.value.number == 0.4


def test_effect_multiplier_keeps_one_assignment_per_attribute():
    effect = protocol.build_command(
        {
            'kind': 'set_override',
            'value': {
                'id': 'held',
                'target': 'all',
                'values': {'intensity': 0.8},
                'intensity_multiplier': 2.0,
            },
        }
    ).set_active_effect
    assert len(effect.attributes) == 1
    assert effect.attributes[0].operation == wire.REPLACE
    assert effect.attributes[0].value.number == 1.0


def test_events_read_partial_pipe_chunks_without_losing_uint64_precision():
    message = wire.RendererEvent(metrics=wire.Metrics(rendered_frames=(1 << 60) + 7))
    payload = message.SerializeToString()

    class ShortReader(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 1))

    decoded = protocol.read_event(ShortReader(protocol.LENGTH_PREFIX.pack(len(payload)) + payload))
    assert decoded.metrics.rendered_frames == (1 << 60) + 7


_CHILD = r'''
import json, struct, sys, time
from proto.starfire import starfire_pb2 as wire
from cli.starfire_protocol import LENGTH_PREFIX, MAX_MESSAGE_BYTES
mode, recording = sys.argv[1:]
def event(**value):
    payload = wire.RendererEvent(**value).SerializeToString()
    sys.stdout.buffer.write(LENGTH_PREFIX.pack(len(payload)) + payload)
    sys.stdout.buffer.flush()
if mode == 'stall':
    time.sleep(30)
if mode == 'malformed':
    sys.stdout.buffer.write(struct.pack('!I', 65537))
    sys.stdout.buffer.flush()
commands = []
while prefix := sys.stdin.buffer.read(4):
    value = wire.RendererCommand.FromString(sys.stdin.buffer.read(LENGTH_PREFIX.unpack(prefix)[0]))
    kind = value.WhichOneof('command')
    record = {'id': value.command_id, 'kind': kind}
    if kind == 'play_sequence' and value.play_sequence.HasField('sequence'):
        record['sequence'] = value.play_sequence.sequence.sequence_id
    commands.append(record)
    if kind == 'login' and mode != 'no_login_reply':
        assert value.command_id == 1
        assert value.login.rig_path and value.login.sequences_path
        assert value.login.artnet_destination
        if mode == 'login_error':
            event(command_id=value.command_id, login_reply={'error': 'fixture patch is missing'})
        else:
            event(command_id=value.command_id, login_reply={
                'frame_rate_hz': 40, 'max_message_bytes': MAX_MESSAGE_BYTES
            })
    elif kind == 'heartbeat':
        if mode != 'no_heartbeat':
            event(heartbeat={})
            if mode != 'no_replies':
                event(command_id=value.command_id, heartbeat={})
        event(metrics={'rendered_frames': len(commands)})
    elif kind != 'login' and mode != 'no_replies':
        error = 'unknown sequence' if mode == 'reject' and 'sequence' in record else ''
        event(command_id=value.command_id, **{kind + '_reply': {'error': error}})
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
            runtime.snapshot() is None or runtime.snapshot()['rendered_frames'] < 5
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        runtime.check_health()
        assert runtime.snapshot()['rendered_frames'] == 5
    assert child.poll() == 0
    assert json.loads((tmp_path / 'commands.json').read_text()) == [
        {'id': 1, 'kind': 'login'},
        {'id': 2, 'kind': 'heartbeat'},
        {'id': 3, 'kind': 'play_sequence', 'sequence': 'blue_impact'},
        {'id': 4, 'kind': 'play_sequence'},
        {'id': 5, 'kind': 'heartbeat'},
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
    assert all(
        'sequence' not in command
        for command in json.loads((tmp_path / 'commands.json').read_text())
    )
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


def test_startup_waits_for_login_reply_before_sending_any_other_command(tmp_path):
    runtime = _fake_runtime(tmp_path, 'no_login_reply', startup_timeout=0.1)
    with pytest.raises(RuntimeError, match='startup deadline'):
        with runtime:
            pytest.fail('renderer became ready without a login reply')
    assert json.loads((tmp_path / 'commands.json').read_text()) == [{'id': 1, 'kind': 'login'}]
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


def test_rejected_login_reports_initialization_error_and_reaps_owned_process(tmp_path):
    runtime = _fake_runtime(tmp_path, 'login_error')
    with pytest.raises(
        RuntimeError, match='login_reply rejected command 1: fixture patch is missing'
    ):
        with runtime:
            pytest.fail('failed initialization became ready')
    assert runtime.process is None
    assert json.loads((tmp_path / 'commands.json').read_text()) == [{'id': 1, 'kind': 'login'}]
    assert all(not thread.is_alive() for thread in runtime._threads)


@pytest.mark.parametrize('failed_thread', ['starfire-events', 'starfire-control'])
def test_thread_start_failure_closes_pipes_without_masking_the_original_error(
    tmp_path, monkeypatch, failed_thread
):
    runtime = _fake_runtime(tmp_path)
    start = threading.Thread.start

    def fail_selected(thread):
        if thread.name == failed_thread:
            raise RuntimeError('injected thread start failure')
        start(thread)

    monkeypatch.setattr(threading.Thread, 'start', fail_selected)
    with pytest.raises(RuntimeError, match='injected thread start failure'):
        with runtime:
            pytest.fail('renderer became ready with a missing worker')
    assert json.loads((tmp_path / 'commands.json').read_text()) == []
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


def test_missing_heartbeat_reply_fails_health_and_shutdown_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_TIMEOUT_SECONDS', 0.25)
    with _fake_runtime(tmp_path, 'no_heartbeat') as runtime:
        child = runtime.process
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match='heartbeat response timed out'):
            runtime.check_health()
    assert child.poll() == 0


def test_missing_command_replies_fail_even_if_heartbeats_and_metrics_continue(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_TIMEOUT_SECONDS', 0.25)
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_INTERVAL_SECONDS', 0.05)
    with _fake_runtime(tmp_path, 'no_replies') as runtime:
        time.sleep(0.3)
        assert runtime.snapshot()['age_seconds'] < 0.2
        with pytest.raises(RuntimeError, match='acknowledgement timed out'):
            runtime.check_health()


def test_rejected_command_reports_reason_and_reaps_renderer(tmp_path):
    with _fake_runtime(tmp_path, 'reject') as runtime:
        child = runtime.process
        runtime.send_command({'kind': 'select', 'bank': 'drop', 'sequence': 'missing'})
        deadline = time.monotonic() + 2
        while runtime._fault is None and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(RuntimeError, match='rejected command 3: unknown sequence'):
            runtime.check_health()
    assert child.poll() == 0


def test_injected_heartbeat_ids_follow_pipe_order_and_fast_responses_are_registered(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    runtime = _fake_runtime(tmp_path)
    runtime.process = Mock()
    runtime._outgoing.put(protocol.build_command({'kind': 'stop'}))
    runtime._outgoing.put(protocol.build_command({'kind': 'stop'}))
    runtime._outgoing.put(None)
    now = 0.0
    monkeypatch.setattr(starfire_runtime.time, 'monotonic', lambda: now)
    recorded = []

    def write_and_acknowledge(encoded):
        nonlocal now
        command = wire.RendererCommand.FromString(encoded[protocol.LENGTH_PREFIX.size :])
        recorded.append((command.command_id, command.WhichOneof('command')))
        # Simulate a child replying while the write is still in progress.
        kind = command.WhichOneof('command')
        reply_kind = kind if kind == 'heartbeat' else f'{kind}_reply'
        runtime._accept_reply(wire.RendererEvent(command_id=command.command_id, **{reply_kind: {}}))
        if kind == 'login':
            runtime._ready.set()
        if command.command_id == 3:
            now += starfire_runtime.HEARTBEAT_INTERVAL_SECONDS

    runtime._write = write_and_acknowledge
    runtime._write_commands()
    assert recorded == [
        (1, 'login'),
        (2, 'heartbeat'),
        (3, 'play_sequence'),
        (4, 'heartbeat'),
        (5, 'play_sequence'),
    ]
    assert runtime._last_reply_id == runtime._last_sent_id == 5
    assert not runtime._pending_replies
    assert runtime._fault is None
    runtime.process.stdin.close.assert_called_once()


@pytest.mark.parametrize(
    'command_id,reply_kind',
    [
        (0, 'set_parameter_reply'),
        (2, 'set_parameter_reply'),
        (1, 'update_beat_reply'),
    ],
)
def test_unsolicited_out_of_order_and_wrong_operation_replies_are_rejected(
    tmp_path, command_id, reply_kind
):
    runtime = _fake_runtime(tmp_path)
    runtime._last_sent_id = 1
    runtime._pending_replies.append((1, 'set_parameter_reply', time.monotonic() + 1))
    with pytest.raises(ValueError):
        runtime._accept_reply(wire.RendererEvent(command_id=command_id, **{reply_kind: {}}))
    assert runtime._last_reply_id == 0
    assert len(runtime._pending_replies) == 1


def test_command_backlog_is_bounded_without_waiting_on_audio_thread(tmp_path, monkeypatch):
    runtime = _fake_runtime(tmp_path)
    runtime._write = lambda _: None
    monkeypatch.setattr(starfire_runtime, 'MAX_PENDING_COMMANDS', 2)
    runtime._write_command(protocol.build_command({'kind': 'heartbeat'}))
    runtime._write_command(protocol.build_command({'kind': 'stop'}))
    with pytest.raises(RuntimeError, match='too many unacknowledged'):
        runtime._write_command(protocol.build_command({'kind': 'stop'}))
    assert runtime._last_sent_id == 2


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
            initial_parameters=[wire.SetParameter(output_brightness=0.5)],
        )
        with runtime:
            runtime.send_command({'kind': 'beat', 'position_beats': 0.0, 'bpm': 120.0})
            runtime.send_command({'kind': 'select', 'bank': 'drop', 'sequence': 'blue_impact'})
            runtime.send_command({'kind': 'set_normalized_rms_level', 'level': 0.8})
            runtime.send_command(
                wire.RendererCommand(
                    set_active_effect=wire.SetActiveEffect(
                        effect_id='held_red',
                        fixtures=wire.FixtureSelection(fixture_id='cf805_pixel_1'),
                        attributes=[
                            wire.AttributeAssignment(
                                attribute=wire.INTENSITY,
                                value=wire.ParameterValue(number=0.75),
                                operation=wire.REPLACE,
                            ),
                            wire.AttributeAssignment(
                                attribute=wire.COLOUR,
                                value=wire.ParameterValue(rgb=wire.RgbColor(red=1.0)),
                                operation=wire.REPLACE,
                            ),
                        ],
                        lifetime=wire.EffectLifetime(until_removed=wire.Empty()),
                    )
                )
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                assert packet.startswith(b'Art-Net\x00')
                runtime.check_health()
                # The strip is RGB-only at address 97; pan/colour-wheel parking
                # values on other fixtures must not count as emitted light.
                if packet[18 + 96 : 18 + 99] == bytes([96, 0, 0]):
                    break
            else:
                pytest.fail('no rendered lighting arrived')
            while runtime._last_reply_id < 6 and time.monotonic() < deadline:
                runtime.check_health()
                time.sleep(0.01)
            assert runtime._last_reply_id == 6
            runtime.send_command(
                wire.RendererCommand(set_parameter=wire.SetParameter(output_brightness=1.0))
            )
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                runtime.check_health()
                if packet[18 + 96 : 18 + 99] == bytes([191, 0, 0]):
                    break
            else:
                pytest.fail('reusing the initialization parameter did not change output')
        assert runtime.process is None
        receiver.settimeout(0.1)
        while True:
            try:
                packet = receiver.recv(1024)
            except TimeoutError:
                break
        dmx = packet[18:]
        assert not any(dmx[96:120])
        assert all(dmx[channel] == 0 for channel in (7, 19, 68, 76, 84, 92))


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
