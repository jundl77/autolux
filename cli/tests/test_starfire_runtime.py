import io
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from autolux.lighting import mvr
from cli import starfire_protocol as protocol
from cli import starfire_runtime
from cli.starfire import ROOT
from proto.starfire import rig_pb2 as rig_wire
from proto.starfire import starfire_pb2 as wire

ROOM = ROOT / 'autolux/tests/data/room.mvr'


def test_the_session_reports_the_revision_of_the_rig_it_loads():
    import argparse
    import hashlib

    args = argparse.Namespace(
        starfire_binary='starfire.exe', rig=None, library='rig/library', autoloops='show/autoloops'
    )
    revision = starfire_runtime.fixture_setup(args, ROOM)['rig_revision']
    assert revision == hashlib.sha256(ROOM.read_bytes()).hexdigest()


def edited_room(path: Path, old: str, new: str) -> Path:
    scene = mvr.read(ROOM.read_bytes())
    xml = mvr._xml_bytes(scene.root).decode('utf-8')
    assert old in xml
    scene.root = mvr._guard_xml(xml.replace(old, new, 1).encode('utf-8'), path.name)
    path.write_bytes(mvr.write(scene))
    return path


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
    message_type, length = protocol.FRAME_HEADER.unpack(encoded[: protocol.FRAME_HEADER.size])
    value = wire.SetActiveEffect.FromString(encoded[protocol.FRAME_HEADER.size :])
    assert message_type == wire.SET_ACTIVE_EFFECT
    assert length == len(encoded) - protocol.FRAME_HEADER.size
    assert value.command_id == 1
    effect = value
    assert effect.group == 'wash'
    assert effect.HasField('intensity')
    assert effect.intensity == 0.0
    assert not effect.HasField('pan')
    assert effect.duration_seconds == 1.0


def test_discarding_delayed_audio_frames_as_its_own_standalone_command():
    command = protocol.build_command({'kind': 'discard_delayed_audio'})
    command.command_id = 3
    encoded = protocol.encode_command(command)
    message_type, length = protocol.FRAME_HEADER.unpack(encoded[: protocol.FRAME_HEADER.size])
    assert message_type == wire.DISCARD_DELAYED_AUDIO
    assert length == len(encoded) - protocol.FRAME_HEADER.size
    value = wire.DiscardDelayedAudio.FromString(encoded[protocol.FRAME_HEADER.size :])
    assert value.command_id == 3
    with pytest.raises(ValueError, match='no fields'):
        protocol.build_command({'kind': 'discard_delayed_audio', 'seconds': 1.0})


def test_control_adapter_translates_a_special_one_to_one():
    special = {
        'colour_slot': 'secondary',
        'energy': 0.25,
        'speed': 0.5,
        'pulse_beats': 2,
        'attack_beats': 8.0,
        'release_beats': 4.0,
        'pattern': 0.75,
    }
    command = {
        'kind': 'set_override',
        'value': {'id': 'accent', 'target': {'group': 'fx'}, 'special_fx': special, 'priority': 50},
    }
    effect = protocol.build_command(command)
    assert (effect.group, effect.priority, effect.HasField('intensity')) == ('fx', 50, False)
    assert {field.name: value for field, value in effect.special_fx.ListFields()} == special
    effect.command_id = 1
    encoded = protocol.encode_command(effect)
    assert wire.SetActiveEffect.FromString(encoded[protocol.FRAME_HEADER.size :]) == effect

    continuous = protocol.build_command(
        {
            'kind': 'set_override',
            'value': {
                'id': 'accent',
                'target': {'group': 'fx'},
                'special_fx': {**special, 'pulse_beats': None, 'pattern': None},
            },
        }
    )
    assert not continuous.special_fx.HasField('pulse_beats')
    assert not continuous.special_fx.HasField('pattern')
    with pytest.raises(ValueError, match='special'):
        protocol.build_command(
            {
                'kind': 'set_override',
                'value': {'id': 'accent', 'target': 'all', 'special_fx': {'strobe_hz': 4.0}},
            }
        )


def test_login_groups_become_plain_capability_records():
    reply = wire.LoginReply(
        groups=[
            wire.GroupCapability(
                name='fx',
                cell_count=1,
                colour=True,
                speed=True,
                pulse=True,
                role=rig_wire.GROUP_ROLE_ACCENT,
                area=rig_wire.STAGE_AREA_UP_LEFT,
                centre_x_metres=-2.5,
            ),
            wire.GroupCapability(name='wash', cell_count=4, pulse=True),
        ]
    )
    assert protocol.login_groups(reply) == [
        {
            'name': 'fx',
            'cell_count': 1,
            'colour': True,
            'speed': True,
            'pulse': True,
            'pattern': False,
            'role': 'accent',
            'zone': 'house_left',
            'depth': 'upstage',
            'x': -2.5,
        },
        {
            'name': 'wash',
            'cell_count': 4,
            'colour': False,
            'speed': False,
            'pulse': True,
            'pattern': False,
            'role': 'loop',
            'zone': None,
            'depth': None,
            'x': 0.0,
        },
    ]


def test_control_adapter_preserves_gdtf_physical_values_and_named_presets():
    effect = protocol.build_command(
        {
            'kind': 'set_override',
            'value': {
                'id': 'detail',
                'target': {'fixture': 'mover'},
                'values': {'attributes': {'Zoom': 0.0, 'Gobo1': 'Open'}},
            },
        }
    )
    assert effect.attributes['Zoom'].WhichOneof('value') == 'number'
    assert effect.attributes['Zoom'].number == 0.0
    assert effect.attributes['Gobo1'].preset == 'Open'


@pytest.mark.parametrize('attributes', [None, [], 'Zoom'])
def test_control_adapter_rejects_non_mapping_gdtf_attributes(attributes):
    with pytest.raises(ValueError, match='attributes must map'):
        protocol.build_command(
            {
                'kind': 'set_override',
                'value': {'id': 'detail', 'target': 'all', 'values': {'attributes': attributes}},
            }
        )


@pytest.mark.parametrize('value', [True, [], {}, float('nan')])
def test_control_adapter_rejects_invalid_gdtf_values(value):
    with pytest.raises(ValueError):
        protocol.build_command(
            {
                'kind': 'set_override',
                'value': {
                    'id': 'detail',
                    'target': 'all',
                    'values': {'attributes': {'Zoom': value}},
                },
            }
        )


@pytest.mark.parametrize(
    'command',
    [
        {'kind': 'set_normalized_rms_level', 'level': float('nan')},
        {'kind': 'set_master', 'intensity': float('inf')},
        {'kind': 'unknown'},
        {'kind': 'stop', 'autoloop': {'bank_id': 'drop', 'autoloop_id': 'kick'}},
        {'kind': 'select', 'bank': 'drop', 'autoloop': 'x', 'extra': 1},
        {
            'kind': 'select',
            'bank': 'drop',
            'autoloop': 'x',
            'transition': {'kind': 'cut', 'extra': 1},
        },
    ],
)
def test_invalid_control_cannot_enter_the_pipe(command):
    with pytest.raises(ValueError):
        protocol.build_command(command)


@pytest.mark.parametrize(
    'payload',
    [
        b'\x00',
        protocol.FRAME_HEADER.pack(wire.METRICS, protocol.MAX_MESSAGE_BYTES + 1),
        protocol.FRAME_HEADER.pack(200, 0xFFFFFFFF),
        protocol.FRAME_HEADER.pack(wire.METRICS, 2) + b'\x08',
    ],
)
def test_incomplete_and_oversized_events_are_rejected(payload):
    with pytest.raises(ValueError):
        protocol.read_event(io.BytesIO(payload))
    assert protocol.read_event(io.BytesIO()) is None


def _padded(message_class, size):
    unknown_field = wire.AutoloopDefinition(lua_source='-' * size).SerializeToString()
    message = message_class.FromString(b'z' + unknown_field[1:])
    message.command_id = 1
    return message


def _round_trip(message):
    message_type, length = protocol.FRAME_HEADER.unpack_from(
        frame := protocol.encode_command(message)
    )
    assert length == len(frame) - protocol.FRAME_HEADER.size
    return protocol.COMMAND_MESSAGES[message_type].FromString(frame[protocol.FRAME_HEADER.size :])


@pytest.mark.parametrize(
    'message_class,size,accepted',
    [
        (wire.SetParameter, 5 * 1024, False),
        (wire.UpdateBeat, protocol.MAX_MESSAGE_BYTES - 5, True),
        (wire.UpdateBeat, protocol.MAX_MESSAGE_BYTES - 4, False),
        (wire.PlayAutoloop, 3 * 1024, True),
        (wire.Login, 1024 * 1024, True),
        (wire.Login, protocol.MAX_LOGIN_BYTES + 100 * 1024, False),
    ],
)
def test_only_login_may_use_the_large_message_limit(message_class, size, accepted):
    message = _padded(message_class, size)
    if accepted:
        assert _round_trip(message) == message
    else:
        name = wire.MessageType.Name(protocol.MESSAGE_TYPE_BY_CLASS[message_class])
        with pytest.raises(ValueError, match=rf'{name} payload of \d+ bytes exceeds'):
            protocol.encode_command(message)


@pytest.mark.parametrize(
    'kind,field,parameter,value_type,value',
    [
        ('set_master', 'intensity', wire.OUTPUT_BRIGHTNESS, 'number', 0.0),
        ('set_speed', 'multiplier', wire.ANIMATION_SPEED, 'number', 0.0),
        ('set_blackout', 'enabled', wire.OUTPUT_BLACKOUT, 'boolean', False),
        ('set_normalized_rms_level', 'level', wire.AUDIO_RMS_NORMALIZED, 'number', 0.0),
        ('set_paused', 'paused', wire.PAUSED, 'boolean', True),
        ('set_default_crossfade_beats', 'beats', wire.DEFAULT_CROSSFADE_BEATS, 'number', 2.0),
        ('set_strobe_max_rate_hz', 'hz', wire.STROBE_MAX_RATE_HZ, 'number', 6.0),
        ('set_strobe_max_burst_seconds', 'seconds', wire.STROBE_MAX_BURST_SECONDS, 'number', 3.0),
        ('set_intensity', 'level', wire.INTENSITY, 'number', 0.3),
    ],
)
def test_controls_use_typed_parameters(kind, field, parameter, value_type, value):
    command = protocol.build_command({'kind': kind, field: value})
    assert isinstance(command, wire.SetParameter)
    setting = command.parameter
    assert setting.type == parameter
    assert setting.WhichOneof('value') == value_type
    assert getattr(setting, value_type) == value


def test_master_fade_maps_to_brightness_parameter():
    command = protocol.build_command({'kind': 'set_master', 'intensity': 0.15, 'fade_seconds': 1.0})
    assert command.parameter.type == wire.OUTPUT_BRIGHTNESS
    assert (command.parameter.number, command.parameter.fade_seconds) == (0.15, 1.0)
    decoded = wire.SetParameter.FromString(command.SerializeToString())
    assert decoded.parameter.fade_seconds == 1.0
    assert (
        protocol.build_command({'kind': 'set_master', 'intensity': 0.5}).parameter.fade_seconds == 0
    )
    with pytest.raises(ValueError, match='unexpected fields'):
        protocol.build_command({'kind': 'set_speed', 'multiplier': 1.0, 'fade_seconds': 1.0})


def test_a_group_master_names_its_group_on_the_wire():
    command = protocol.build_command({'kind': 'set_group_master', 'group': 'wash', 'level': 0.5})
    decoded = wire.SetParameter.FromString(command.SerializeToString())
    assert decoded.parameter.type == wire.GROUP_MASTER
    assert (decoded.parameter.group, decoded.parameter.number) == ('wash', 0.5)
    with pytest.raises(ValueError):
        protocol.build_command({'kind': 'set_group_master', 'level': 0.5})


@pytest.mark.parametrize('bar_phase', [0, 3, None])
def test_beat_facts_survive_the_wire_and_keep_downbeat_distinct_from_unknown(bar_phase):
    command = protocol.build_command(
        {'kind': 'beat', 'beat_index': 41, 'bar_phase': bar_phase, 'beats_per_bar': 4, 'bpm': 128.0}
    )
    decoded = wire.UpdateBeat.FromString(command.SerializeToString())
    assert (decoded.beat_index, decoded.beats_per_bar, decoded.tempo_bpm) == (41, 4, 128.0)
    assert (decoded.bar_phase if decoded.HasField('bar_phase') else None) == bar_phase


def test_typed_commands_are_copied_before_queueing():
    command = wire.SetParameter(
        command_id=93,
        parameter=wire.Parameter(type=wire.OUTPUT_BRIGHTNESS, number=0.4),
    )
    copied = protocol.build_command(command)
    command.parameter.number = 0.9
    assert copied.command_id == 0
    assert copied.parameter.number == 0.4


@pytest.mark.parametrize(
    'unsupported',
    [
        {'intensity_multiplier': 2.0},
        {'lifetime': {'kind': 'beats', 'duration_beats': 4.0}},
    ],
)
def test_unsupported_effect_controls_are_not_silently_changed(unsupported):
    with pytest.raises(ValueError):
        protocol.build_command(
            {
                'kind': 'set_override',
                'value': {
                    'id': 'held',
                    'target': 'all',
                    'values': {'intensity': 0.8},
                    **unsupported,
                },
            }
        )


def test_events_read_partial_pipe_chunks_without_losing_uint64_precision():
    message = wire.Metrics(rendered_frames=(1 << 60) + 7)
    payload = message.SerializeToString()

    class ShortReader(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 1))

    decoded = protocol.read_event(
        ShortReader(protocol.FRAME_HEADER.pack(wire.METRICS, len(payload)) + payload)
    )
    assert decoded.rendered_frames == (1 << 60) + 7


@pytest.mark.parametrize('message_type', [wire.LOGIN, wire.MESSAGE_TYPE_UNSPECIFIED, 255])
def test_unknown_and_wrong_direction_message_types_are_rejected(message_type):
    with pytest.raises(ValueError, match='server message type'):
        protocol.read_event(io.BytesIO(protocol.FRAME_HEADER.pack(message_type, 0)))


@pytest.mark.parametrize(
    'message_type,message_class', [(wire.HEARTBEAT, wire.Heartbeat), (wire.METRICS, wire.Metrics)]
)
def test_empty_standalone_server_messages_are_valid(message_type, message_class):
    assert (
        protocol.read_event(io.BytesIO(protocol.FRAME_HEADER.pack(message_type, 0)))
        == message_class()
    )


_CHILD = r'''
import json, sys, time
from proto.starfire import starfire_pb2 as wire
from cli.starfire_protocol import (
    FRAME_HEADER, MAX_MESSAGE_BYTES, COMMAND_MESSAGES, MESSAGE_TYPE_BY_CLASS,
)
mode, recording = sys.argv[1:]
def event(value):
    payload = value.SerializeToString()
    sys.stdout.buffer.write(FRAME_HEADER.pack(MESSAGE_TYPE_BY_CLASS[type(value)], len(payload)) + payload)
    sys.stdout.buffer.flush()
if mode == 'stall':
    time.sleep(30)
if mode == 'malformed':
    sys.stdout.buffer.write(FRAME_HEADER.pack(wire.METRICS, MAX_MESSAGE_BYTES + 1))
    sys.stdout.buffer.flush()
commands = []
last_processed = 0
run_id = 0
while prefix := sys.stdin.buffer.read(FRAME_HEADER.size):
    message_type, length = FRAME_HEADER.unpack(prefix)
    value = COMMAND_MESSAGES[message_type].FromString(sys.stdin.buffer.read(length))
    record = {'kind': type(value).__name__}
    if not isinstance(value, wire.Heartbeat):
        record['id'] = value.command_id
        if mode != 'no_progress' or isinstance(value, wire.Login):
            last_processed = value.command_id
    if isinstance(value, wire.PlayAutoloop) and value.autoloop_id:
        record['autoloop'] = value.autoloop_id
    commands.append(record)
    if isinstance(value, wire.Login) and mode != 'no_login_reply':
        run_id = value.run_id
        assert run_id > 0
        assert value.command_id == 1
        assert value.HasField('rig') and value.rig.fixtures and value.autoloops
        assert value.artnet_destination
        assert value.protocol_major == wire.PROTOCOL_MAJOR and value.protocol_minor == wire.PROTOCOL_MINOR
        if mode == 'login_error':
            event(wire.ErrorReply(command_id=value.command_id, message='fixture patch is missing'))
        else:
            event(wire.LoginReply(command_id=value.command_id,
                run_id=run_id if mode != 'wrong_login_run' else 1,
                frame_rate_hz=40, max_message_bytes=MAX_MESSAGE_BYTES,
                protocol_major=wire.PROTOCOL_MAJOR + (mode == 'wrong_major_reply'),
                protocol_minor=wire.PROTOCOL_MINOR + (mode == 'newer_minor_reply'),
                groups=[wire.GroupCapability(name='fx', cell_count=1, colour=True, pulse=True)],
            ))
    elif isinstance(value, wire.Heartbeat):
        if mode != 'no_heartbeat':
            event(wire.Heartbeat())
        event(wire.Metrics(rendered_frames=len(commands), last_processed_command_id=last_processed,
            run_id=run_id if mode != 'wrong_metrics_run' else 1))
    elif isinstance(value, wire.SetParameter) and mode == 'reject':
        event(wire.ErrorReply(command_id=value.command_id, message='brightness is out of range'))
with open(recording, 'w') as output:
    json.dump(commands, output)
'''


def _fake_runtime(tmp_path, mode='normal', **kwargs):
    scripts = tmp_path / 'scripts'
    (scripts / 'drop').mkdir(parents=True)
    (scripts / 'drop/kick.lua').write_text('return {render=function(ctx) return {} end}')
    runtime = starfire_runtime.StarfireRuntime(
        sys.executable,
        ROOM,
        scripts,
        '127.0.0.1:6454',
        tmp_path / 'run',
        run_id=kwargs.pop('run_id', (1 << 64) - 1),
        **kwargs,
    )
    runtime.command = [sys.executable, '-c', _CHILD, mode, str(tmp_path / 'commands.json')]
    return runtime


def test_owned_child_readiness_heartbeats_and_eof_cleanup(tmp_path):
    runtime = _fake_runtime(tmp_path)
    assert runtime.groups == []
    with runtime:
        child = runtime.process
        assert runtime.groups == [
            {
                'name': 'fx',
                'cell_count': 1,
                'colour': True,
                'speed': False,
                'pulse': True,
                'pattern': False,
                'role': 'loop',
                'zone': None,
                'depth': None,
                'x': 0.0,
            }
        ]
        runtime.send_command(
            {'kind': 'select', 'bank': 'drop', 'autoloop': 'kick', 'palette': 'techno'}
        )
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
        {'id': 1, 'kind': 'Login'},
        {'kind': 'Heartbeat'},
        {'id': 2, 'kind': 'PlayAutoloop', 'autoloop': 'drop/kick'},
        {'id': 3, 'kind': 'PlayAutoloop'},
        {'kind': 'Heartbeat'},
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
        runtime.send_command(
            {'kind': 'select', 'bank': 'drop', 'autoloop': 'kick', 'palette': 'techno'}
        )
        threading.Timer(0.05, release.set).start()
        started = time.monotonic()
    assert time.monotonic() - started < 1
    assert child.poll() == 0
    assert all(
        'autoloop' not in command
        for command in json.loads((tmp_path / 'commands.json').read_text())
    )
    assert all(not thread.is_alive() for thread in runtime._threads)


@pytest.mark.parametrize(
    'mode,reason', [('stall', 'startup deadline'), ('malformed', 'METRICS payload of')]
)
def test_partial_start_failure_reaps_only_owned_renderer(tmp_path, mode, reason):
    runtime = _fake_runtime(tmp_path, mode, startup_timeout=0.5, stop_timeout=0.1)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=reason), runtime:
        pytest.fail('invalid startup became ready')
    assert time.monotonic() - started < 3
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


@pytest.mark.parametrize('mode', ['wrong_major_reply', 'newer_minor_reply', 'wrong_login_run'])
def test_login_rejects_an_incompatible_server_version_and_reaps_it(tmp_path, mode):
    runtime = _fake_runtime(tmp_path, mode)
    with pytest.raises(RuntimeError, match='Unexpected Starfire login reply'), runtime:
        pytest.fail('incompatible server became ready')
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


def test_startup_waits_for_login_reply_before_sending_any_other_command(tmp_path):
    runtime = _fake_runtime(tmp_path, 'no_login_reply', startup_timeout=0.1)
    with pytest.raises(RuntimeError, match='startup deadline'), runtime:
        pytest.fail('renderer became ready without a login reply')
    assert json.loads((tmp_path / 'commands.json').read_text()) == [{'id': 1, 'kind': 'Login'}]
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


@pytest.mark.parametrize('run_id', [0, -1, 1 << 64, True, 1.5])
def test_invalid_run_id_is_rejected_before_starting_a_renderer(tmp_path, run_id):
    runtime = _fake_runtime(tmp_path, run_id=run_id)
    with pytest.raises(ValueError, match='nonzero uint64'), runtime:
        pytest.fail('invalid run_id started a renderer')
    assert runtime.process is None
    assert not runtime.run_dir.exists()


def test_metrics_from_a_different_run_fail_the_owned_connection(tmp_path):
    runtime = _fake_runtime(tmp_path, 'wrong_metrics_run')
    with pytest.raises(RuntimeError, match='metrics belong to a different run_id'), runtime:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            runtime.check_health()
            time.sleep(0.01)
        pytest.fail('mismatched metrics were accepted')
    assert runtime.process is None


def test_rejected_login_reports_initialization_error_and_reaps_owned_process(tmp_path):
    runtime = _fake_runtime(tmp_path, 'login_error')
    with (
        pytest.raises(RuntimeError, match='rejected command 1: fixture patch is missing'),
        runtime,
    ):
        pytest.fail('failed initialization became ready')
    assert runtime.process is None
    assert json.loads((tmp_path / 'commands.json').read_text()) == [{'id': 1, 'kind': 'Login'}]
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
    with pytest.raises(RuntimeError, match='injected thread start failure'), runtime:
        pytest.fail('renderer became ready with a missing worker')
    assert json.loads((tmp_path / 'commands.json').read_text()) == []
    assert runtime.process is None
    assert all(not thread.is_alive() for thread in runtime._threads)


def test_missing_server_heartbeat_fails_health_and_shutdown_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_TIMEOUT_SECONDS', 0.25)
    with _fake_runtime(tmp_path, 'no_heartbeat') as runtime:
        child = runtime.process
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match='heartbeat response timed out'):
            runtime.check_health()
    assert child.poll() == 0


def test_stalled_command_progress_fails_even_if_heartbeats_and_metrics_continue(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_TIMEOUT_SECONDS', 0.25)
    monkeypatch.setattr(starfire_runtime, 'HEARTBEAT_INTERVAL_SECONDS', 0.05)
    with _fake_runtime(tmp_path, 'no_progress') as runtime:
        runtime.send_command({'kind': 'stop'})
        time.sleep(0.3)
        assert runtime.snapshot()['age_seconds'] < 0.2
        with pytest.raises(RuntimeError, match='command processing stalled'):
            runtime.check_health()


def test_rejected_command_reports_reason_and_reaps_renderer(tmp_path):
    with _fake_runtime(tmp_path, 'reject') as runtime:
        child = runtime.process
        runtime.send_command({'kind': 'set_master', 'intensity': 2.0})
        deadline = time.monotonic() + 2
        while runtime._fault is None and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(RuntimeError, match='rejected command 2: brightness is out of range'):
            runtime.check_health()
    assert child.poll() == 0


def test_heartbeats_do_not_consume_command_ids_or_require_replies(tmp_path, monkeypatch):
    from unittest.mock import Mock

    runtime = _fake_runtime(tmp_path)
    runtime.process = Mock()
    runtime._login = wire.Login()
    runtime._outgoing.put(protocol.build_command({'kind': 'stop'}))
    runtime._outgoing.put(protocol.build_command({'kind': 'stop'}))
    runtime._outgoing.put(None)
    now = 0.0
    monkeypatch.setattr(starfire_runtime.time, 'monotonic', lambda: now)
    recorded = []

    def write_and_observe(encoded):
        nonlocal now
        kind, _ = protocol.FRAME_HEADER.unpack(encoded[: protocol.FRAME_HEADER.size])
        command = protocol.COMMAND_MESSAGES[kind].FromString(encoded[protocol.FRAME_HEADER.size :])
        recorded.append((getattr(command, 'command_id', None), type(command).__name__))
        if isinstance(command, wire.Login):
            runtime._record_progress(command.command_id)
            runtime._ready.set()
        elif isinstance(command, wire.PlayAutoloop) and command.command_id == 2:
            now += starfire_runtime.HEARTBEAT_INTERVAL_SECONDS

    runtime._write = write_and_observe
    runtime._write_commands()
    assert recorded == [
        (1, 'Login'),
        (None, 'Heartbeat'),
        (2, 'PlayAutoloop'),
        (None, 'Heartbeat'),
        (3, 'PlayAutoloop'),
    ]
    assert runtime._last_sent_id == 3
    assert runtime._last_processed_id == 1
    runtime._record_progress(3)
    assert runtime._progress_deadline_at is None
    assert runtime._fault is None
    runtime.process.stdin.close.assert_called_once()


@pytest.mark.parametrize('command_id', [0, 3])
def test_backward_and_unsent_command_progress_is_rejected(tmp_path, command_id):
    runtime = _fake_runtime(tmp_path)
    runtime._last_sent_id = 2
    runtime._last_processed_id = 1
    with pytest.raises(ValueError, match='command progress'):
        runtime._record_progress(command_id)
    assert runtime._last_processed_id == 1


def test_repeated_metrics_do_not_hide_stalled_command_progress(tmp_path, monkeypatch):
    runtime = _fake_runtime(tmp_path)
    runtime._last_sent_id = 3
    runtime._last_processed_id = 1
    runtime._progress_deadline_at = 10.0
    monkeypatch.setattr(starfire_runtime.time, 'monotonic', lambda: 9.0)
    runtime._record_progress(1)
    assert runtime._progress_deadline_at == 10.0
    runtime._record_progress(2)
    assert runtime._progress_deadline_at == 9.0 + starfire_runtime.HEARTBEAT_TIMEOUT_SECONDS


def test_unknown_autoloop_is_rejected_before_queueing(tmp_path):
    with _fake_runtime(tmp_path) as runtime:
        with pytest.raises(ValueError, match='Unknown Starfire autoloop'):
            runtime.send_command(wire.PlayAutoloop(autoloop_id='missing'))
        assert runtime._outgoing.empty()
        runtime.check_health()


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


def red_at_squared_master(brightness, held_intensity=0.75):
    return round(255 * held_intensity * brightness**2)


def test_real_renderer_accepts_python_protobuf_and_blackouts_on_eof(tmp_path):
    root = Path(__file__).resolve().parents[2]
    binary = root / 'target/debug/starfire.exe'
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise the native protocol boundary')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(3)
        runtime = starfire_runtime.StarfireRuntime(
            binary,
            ROOM,
            root / 'show/autoloops',
            f'127.0.0.1:{receiver.getsockname()[1]}',
            tmp_path / 'native',
            run_id=(1 << 64) - 1,
            initial_parameters=[wire.Parameter(type=wire.OUTPUT_BRIGHTNESS, number=0.5)],
        )
        with runtime:
            runtime.send_command(
                {'kind': 'beat', 'beat_index': 0, 'bar_phase': 0, 'beats_per_bar': 4, 'bpm': 120.0}
            )
            runtime.send_command(
                {'kind': 'select', 'bank': 'drop', 'autoloop': 'kick', 'palette': 'techno'}
            )
            runtime.send_command({'kind': 'set_normalized_rms_level', 'level': 0.8})
            runtime.send_command(
                wire.SetActiveEffect(
                    effect_id='held_red',
                    fixture='zq06017/Beam Segment 1',
                    intensity=0.75,
                    colour=wire.RgbColor(red=1.0),
                )
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                assert packet.startswith(b'Art-Net\x00')
                runtime.check_health()
                # The strip is RGB-only at address 97; pan/colour-wheel parking
                # values on other fixtures must not count as emitted light.
                if packet[18 + 96 : 18 + 99] == bytes([red_at_squared_master(0.5), 0, 0]):
                    break
            else:
                pytest.fail('no rendered lighting arrived')
            # Successful commands have no reply; their actual DMX proves application.
            runtime.send_command(
                wire.SetParameter(parameter=wire.Parameter(type=wire.OUTPUT_BRIGHTNESS, number=1.0))
            )
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                runtime.check_health()
                if packet[18 + 96 : 18 + 99] == bytes([red_at_squared_master(1.0), 0, 0]):
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
    binary = root / 'target/debug/starfire.exe'
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise native fixture validation')
    rig = edited_room(
        tmp_path / 'multi-universe.mvr', '<Address break="0">1<', '<Address break="0">513<'
    )
    autoloops = root / 'show/autoloops'
    with pytest.raises(RuntimeError, match=r'fixture universes \[0, 1\].*Radius TX universe 0'):
        starfire_runtime.validate_starfire(binary, rig, autoloops, expected_universe=0)
    starfire_runtime.validate_starfire(binary, rig, autoloops)
    starfire_runtime.validate_starfire(binary, ROOM, autoloops, expected_universe=0)


def test_preflight_renders_autoloops_against_the_selected_patch(tmp_path, caplog):
    root = Path(__file__).resolve().parents[2]
    binary = root / 'target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise native autoloop validation')
    rig = edited_room(tmp_path / 'missing-strip.mvr', 'name="strip"', 'name="pixels"')
    with caplog.at_level('WARNING'):
        starfire_runtime.validate_starfire(binary, rig, root / 'show/autoloops')
    assert 'groups not in this rig: strip' in caplog.text


def test_preflight_rejects_valid_catalog_missing_an_autolux_autoloop(tmp_path):
    from autolux.lib.engine.effect_definitions import PREFERRED_AUTOLOOPS, LightIntent

    root = Path(__file__).resolve().parents[2]
    binary = root / 'target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
    if not binary.is_file():
        pytest.skip('Build Starfire to exercise native autoloop validation')
    for intent, autoloop in PREFERRED_AUTOLOOPS.items():
        if intent == LightIntent.DROP:
            continue
        directory = tmp_path / intent.value
        directory.mkdir()
        (directory / f'{autoloop}.lua').write_text(
            'return { render = function(ctx) return {} end }'
        )
    with pytest.raises(RuntimeError, match='missing Autolux autoloops: drop/kick'):
        starfire_runtime.validate_starfire(binary, ROOM, tmp_path)


def test_preflight_requires_every_discovered_autoloop(tmp_path, monkeypatch):
    from types import SimpleNamespace

    for bank, names in {
        'atmospheric': ('cold_breathe', 'amber_drift', 'pixel_rain'),
        'breakdown': ('wide_space',),
        'buildup': ('ramp',),
        'drop': ('kick', 'slam'),
    }.items():
        (tmp_path / bank).mkdir()
        for name in names:
            (tmp_path / bank / f'{name}.lua').write_text('return {}')
    checked = [
        {'bank': bank, 'autoloop': name}
        for bank, name in [
            ('atmospheric', 'cold_breathe'),
            ('breakdown', 'wide_space'),
            ('buildup', 'ramp'),
            ('drop', 'kick'),
        ]
    ]
    report = json.dumps({'universes': [0], 'checked_autoloops': checked})
    binary = tmp_path / 'starfire.exe'
    binary.touch()
    monkeypatch.setattr(
        starfire_runtime.subprocess,
        'run',
        lambda *_, **__: SimpleNamespace(returncode=0, stdout=report, stderr=''),
    )
    with pytest.raises(
        RuntimeError,
        match='missing Autolux autoloops: atmospheric/amber_drift, atmospheric/pixel_rain, '
        'drop/slam, standby/warm_white$',
    ):
        starfire_runtime.validate_starfire(binary, ROOM, tmp_path)


def test_preflight_names_an_autoloop_whose_selection_header_is_broken(tmp_path, monkeypatch):
    from types import SimpleNamespace

    header = '-- 8-beat loop.\n-- energy: 2\n-- movement: snap\n-- texture: wash\nreturn {}\n'
    loops = [
        ('atmospheric', 'cold_breathe'),
        ('breakdown', 'wide_space'),
        ('buildup', 'ramp'),
        ('drop', 'kick'),
        ('standby', 'warm_white'),
    ]
    for bank, name in loops:
        (tmp_path / bank).mkdir()
        source = header.replace('texture: wash', 'texture: neon') if bank == 'drop' else header
        (tmp_path / bank / f'{name}.lua').write_text(source)
    checked = [{'bank': bank, 'autoloop': name} for bank, name in loops]
    report = json.dumps({'universes': [0], 'checked_autoloops': checked})
    binary = tmp_path / 'starfire.exe'
    binary.touch()
    monkeypatch.setattr(
        starfire_runtime.subprocess,
        'run',
        lambda *_, **__: SimpleNamespace(returncode=0, stdout=report, stderr=''),
    )
    with pytest.raises(RuntimeError, match="cannot select.*drop/kick: texture 'neon'"):
        starfire_runtime.validate_starfire(binary, ROOM, tmp_path)


def test_preflight_warns_about_groups_the_rig_lacks(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace

    header = '-- 8-beat loop.\n-- energy: 2\n-- movement: snap\n-- texture: wash\nreturn {}\n'
    loops = [
        ('atmospheric', 'cold_breathe'),
        ('breakdown', 'wide_space'),
        ('buildup', 'ramp'),
        ('drop', 'kick'),
        ('standby', 'warm_white'),
    ]
    for bank, name in loops:
        (tmp_path / bank).mkdir()
        (tmp_path / bank / f'{name}.lua').write_text(header)
    checked = [{'bank': bank, 'autoloop': name} for bank, name in loops]
    report = json.dumps(
        {'universes': [0], 'checked_autoloops': checked, 'missing_groups': ['fx', 'lasers']}
    )
    binary = tmp_path / 'starfire.exe'
    binary.touch()
    monkeypatch.setattr(
        starfire_runtime.subprocess,
        'run',
        lambda *_, **__: SimpleNamespace(returncode=0, stdout=report, stderr=''),
    )
    with caplog.at_level('WARNING'):
        starfire_runtime.validate_starfire(binary, ROOM, tmp_path)
    assert 'groups not in this rig: fx, lasers' in caplog.text


def test_an_omitted_transition_leaves_the_default_to_starfire_and_a_cut_stays_explicit():
    default = protocol.build_command({'kind': 'select', 'bank': 'buildup', 'autoloop': 'ramp'})
    assert not default.HasField('transition')
    cut = protocol.build_command(
        {'kind': 'select', 'bank': 'drop', 'autoloop': 'kick', 'transition': {'kind': 'cut'}}
    )
    assert cut.HasField('transition') and not cut.transition.HasField('crossfade_beats')
