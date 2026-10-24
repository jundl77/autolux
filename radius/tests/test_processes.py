"""Exercise the actual native executables without requiring audio/USB hardware."""

import json
import socket
import time
from pathlib import Path

import pytest

from cli.native_runtime import NativeRuntime, NativeSpec, validate_native

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


@pytest.mark.parametrize(
    'key,value',
    [
        ('bus_port', -1),
        ('bus_port', 65536),
        ('sample_rate', 48000),
        ('jitter_buffer_ms', 0),
        ('dmx_universe', -1),
        ('session', -1),
        ('session', 0),
        ('audio_core', 32),
        ('audio_core', 2**64 - 1),
        ('playback_delay_seconds', -1),
    ],
)
def test_config_rejects_invalid_values_before_devices_open(tmp_path, key, value):
    with pytest.raises(RuntimeError, match='config check failed'):
        validate_native(spec('tx', **{key: value}), tmp_path)


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
