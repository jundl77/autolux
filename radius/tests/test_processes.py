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
            'run_id': 87654,
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
            spec('rx', bus_port=port, run_id=2**64 - 1),
            spec('tx', bus_port=port, run_id=2**64 - 1),
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
        assert report['run_id'] == 2**64 - 1
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


def test_pending_login_can_be_cancelled_without_starting_capture(tmp_path, monkeypatch):
    # A silent listener keeps the startup pending without opening any real outputs.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        port = receiver.getsockname()[1]
        runtime = NativeRuntime([spec('tx', bus_port=port)], tmp_path / 'run', startup_timeout=0.5)
        stopped = []
        close = runtime.close

        def close_and_record():
            stopped.extend(process for _, process, _ in runtime.children)
            close()

        monkeypatch.setattr(runtime, 'close', close_and_record)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='did not become ready'):
            runtime.__enter__()
        assert time.monotonic() - started < 3
        assert not runtime.children
        assert not (tmp_path / 'run/radius_tx/ready').exists()
        assert len(stopped) == 1 and stopped[0].returncode == 0


def test_restarted_receiver_rejects_a_transmitter_from_the_previous_run(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    previous_run_id, current_run_id = 1, 2**64 - 1
    runtime = NativeRuntime(
        [
            spec('rx', bus_port=port, run_id=current_run_id),
            spec('tx', bus_port=port, run_id=previous_run_id),
        ],
        tmp_path / 'run',
        startup_timeout=0.5,
    )
    with pytest.raises(RuntimeError, match='radius_tx did not become ready'):
        runtime.__enter__()
    assert not runtime.children
    assert not (tmp_path / 'run/radius_tx/ready').exists()
    assert not (tmp_path / 'run/radius_rx/health.json').exists()


def test_private_artnet_input_ignores_other_applications_standard_port(tmp_path):
    standard_artnet_port = 6454
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as bus_reservation,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as artnet_reservation,
    ):
        bus_reservation.bind(('127.0.0.1', 0))
        artnet_reservation.bind(('127.0.0.1', 0))
        bus_port = bus_reservation.getsockname()[1]
        artnet_port = artnet_reservation.getsockname()[1]
    assert artnet_port != standard_artnet_port
    with (
        NativeRuntime(
            [
                spec('rx', bus_port=bus_port),
                spec(
                    'tx',
                    bus_port=bus_port,
                    artnet_enabled=True,
                    artnet_address='127.0.0.1',
                    artnet_port=artnet_port,
                ),
            ],
            tmp_path / 'run',
        ) as runtime,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender,
    ):
        # Universe 0, protocol 14, 512 channels. No physical receiver output is enabled.
        artdmx = b'Art-Net\0\x00\x50\x00\x0e\x00\x00\x00\x00\x02\x00' + bytes([77]) + bytes(511)
        health = tmp_path / 'run/radius_tx/health.json'

        def next_report(previous=-1):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                runtime.check_health()
                try:
                    report = json.loads(health.read_text())
                    if report['timestamp'] > previous:
                        return report
                except (OSError, json.JSONDecodeError):
                    pass
                time.sleep(0.05)
            pytest.fail('Transmitter health did not update')

        report = next_report()
        assert report['tx']['dmx_input_age_seconds'] is None
        sender.sendto(artdmx, ('127.0.0.1', standard_artnet_port))
        report = next_report(report['timestamp'])
        assert report['tx']['dmx_input_age_seconds'] is None
        sender.sendto(artdmx, ('127.0.0.1', artnet_port))
        report = next_report(report['timestamp'])
        assert 0 <= report['tx']['dmx_input_age_seconds'] < 2
        # Even a second socket requesting port sharing cannot claim the private input.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as competing:
            competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competing.bind(('127.0.0.1', artnet_port))


@pytest.mark.parametrize('port', [0, 65536])
def test_native_artnet_port_rejects_out_of_range_values(tmp_path, port):
    import subprocess

    transmitter = spec('tx', artnet_port=port)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(transmitter.config))
    result = subprocess.run(
        [str(transmitter.executable), '--check-config', str(config)],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode != 0
    assert 'artnet_port is out of range' in result.stderr
