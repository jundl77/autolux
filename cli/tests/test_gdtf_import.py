"""Imported standard fixture modes must work through the actual renderer login."""

import argparse
import os
import socket
import time
import zipfile
from pathlib import Path

import pytest

from autolux.lighting.gdtf_compile import compile_mode
from cli import starfire
from cli.starfire_assets import load_login
from cli.starfire_runtime import StarfireRuntime

ROOT = Path(__file__).parents[2]
BINARY = ROOT / 'target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
DESCRIPTION = ROOT / 'autolux/tests/data/gdtf/dimmer.xml'
ARTNET_HEADER_BYTES = 18
TEST_TIMEOUT_SECONDS = 5.0


@pytest.mark.parametrize('mode,expected', [('8 bit', b'\x80'), ('16 bit', b'\x80\x00')])
def test_imported_fixture_renders_from_login_without_rebuilding(
    tmp_path, write_rig, mode, expected
):
    if not BINARY.is_file():
        pytest.skip('Build Starfire to test native GDTF import')
    archive = tmp_path / 'fixture.gdtf'
    xml = DESCRIPTION.read_bytes()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('description.xml', xml)
    library = tmp_path / 'library'
    starfire.run_import(
        argparse.Namespace(binary=BINARY, input=archive, library=library, mode=mode)
    )
    rig = write_rig(library, 'lamp', mode=mode)
    autoloops = tmp_path / 'autoloops'
    bank = autoloops / 'test'
    bank.mkdir(parents=True)
    (bank / 'dimmer.lua').write_text(
        'return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.5)} end}',
        encoding='utf-8',
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(TEST_TIMEOUT_SECONDS)
        destination = f'127.0.0.1:{receiver.getsockname()[1]}'
        login = load_login(rig, autoloops, destination, run_id=1)
        assert login.rig.profiles[0].mode == compile_mode(xml.decode('utf-8'), mode)
        with StarfireRuntime(
            BINARY, rig, autoloops, destination, tmp_path / 'run', run_id=1
        ) as runtime:
            runtime.send_command(
                {'kind': 'select', 'bank': 'test', 'autoloop': 'dimmer', 'palette': 'techno'}
            )
            deadline = time.monotonic() + TEST_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                payload = packet[ARTNET_HEADER_BYTES:]
                if payload[: len(expected)] == expected:
                    assert not any(payload[len(expected) :])
                    break
            else:
                pytest.fail('Imported GDTF mode did not produce the expected DMX')
