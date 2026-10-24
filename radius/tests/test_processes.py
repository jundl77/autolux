"""Exercise the actual native executables without requiring audio/USB hardware."""

import json
from contextlib import ExitStack
import socket
import time
from pathlib import Path

import pytest

from cli.native_runtime import NativeRuntime, NativeSpec

ROOT = Path(__file__).resolve().parents[2]


def spec(role, **config):
    name = f'radius_{role}'
    exe = ROOT / 'radius/cmake-build-release' / name / f'{name}.exe'
    if not exe.exists():
        pytest.skip('Run build-radius.ps1 to exercise native processes')
    return NativeSpec(
        name,
        exe,
        {
            'peer_address': '127.0.0.1',
            'session': 87654,
            'audio_enabled': False,
            'dmx_enabled': False,
            'artnet_enabled': False,
            **config,
        },
    )


def test_actual_endpoints_heartbeat_both_ways_report_health_and_stop(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    with NativeRuntime(
        [
            spec('rx', bus_port=port, session=2**64 - 1),
            spec('tx', bus_port=port, session=2**64 - 1),
        ],
        tmp_path / 'run',
    ) as runtime:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            runtime.check_health()
            try:
                report = json.loads((tmp_path / 'run/radius_tx/health.json').read_text())
                if (
                    report['tx']['heartbeats_received'] >= 2
                    and report['rx']['heartbeats_received'] >= 2
                ):
                    break
            except (OSError, KeyError, TypeError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail('Bidirectional 10-second heartbeats or receiver health missing')
        assert report['tx']['link_up'] and report['rx']['link_up']
        assert report['session'] == 2**64 - 1
        assert report['rx']['audio_input_age_seconds'] is None
        assert report['rx']['audio_max_gap_seconds'] is None
        assert report['rx']['buffer_target_seconds'] == 0.15
        assert report['rx']['invalid_packets_total'] == 0
        assert report['rx_age_seconds'] < 2
        children = [process for _, process, _ in runtime.children]
    assert all(process.returncode == 0 for process in children)


def test_health_file_readers_do_not_stop_endpoints_and_publishing_recovers(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    with NativeRuntime(
        [spec('rx', bus_port=port), spec('tx', bus_port=port)], tmp_path / 'run'
    ) as runtime:
        files = [directory / 'health.json' for _, _, directory in runtime.children]
        deadline = time.monotonic() + 5
        while not all(path.is_file() for path in files):
            runtime.check_health()
            assert time.monotonic() < deadline, 'Initial health reports missing'
            time.sleep(0.05)
        with ExitStack() as readers:
            # Python's Windows file handles deny replacement until they close.
            held = [readers.enter_context(path.open('rb')) for path in files]
            previous = [json.load(stream)['timestamp'] for stream in held]
            time.sleep(2.5)
            runtime.check_health()
            assert [json.loads(path.read_text())['timestamp'] for path in files] == previous
        deadline = time.monotonic() + 5
        while True:
            runtime.check_health()
            current = [json.loads(path.read_text())['timestamp'] for path in files]
            if all(after > before for before, after in zip(previous, current)):
                break
            assert time.monotonic() < deadline, 'Health publishing did not recover'
            time.sleep(0.05)
