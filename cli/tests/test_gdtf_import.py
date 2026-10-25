"""Imported standard fixture modes must work through the actual renderer login."""

import argparse
import json
import os
import socket
import time
import zipfile
from pathlib import Path

import pytest

from cli import starfire
from cli.starfire_assets import load_login
from cli.starfire_runtime import StarfireRuntime

ROOT = Path(__file__).parents[2]
BINARY = ROOT / 'starfire/target/debug' / ('starfire.exe' if os.name == 'nt' else 'starfire')
DESCRIPTION = ROOT / 'autolux/tests/data/gdtf/dimmer.xml'
ARTNET_HEADER_BYTES = 18
TEST_TIMEOUT_SECONDS = 5.0


@pytest.mark.parametrize('mode,expected', [('8 bit', b'\x80'), ('16 bit', b'\x80\x00')])
def test_imported_fixture_renders_from_login_without_rebuilding(tmp_path, mode, expected):
    if not BINARY.is_file():
        pytest.skip('Build Starfire to test native GDTF import')
    archive = tmp_path / 'fixture.gdtf'
    xml = DESCRIPTION.read_bytes()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('description.xml', xml)
    profiles = tmp_path / 'profiles.json'
    starfire.run_import(
        argparse.Namespace(
            binary=BINARY,
            input=archive,
            profiles=profiles,
            profile_id='imported',
            mode=mode,
        )
    )
    rig = tmp_path / 'rig.json'
    rig.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'fixtures': [{'id': 'lamp', 'profile': 'imported', 'universe': 0, 'address': 1}],
                'groups': {},
            }
        ),
        encoding='utf-8',
    )
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
        login = load_login(rig, autoloops, destination, profiles=profiles)
        assert login.rig.profiles[0].gdtf_xml.encode('utf-8') == xml
        with StarfireRuntime(
            BINARY, rig, autoloops, destination, tmp_path / 'run', profiles=profiles
        ) as runtime:
            runtime.send_command({'kind': 'select', 'bank': 'test', 'autoloop': 'dimmer'})
            deadline = time.monotonic() + TEST_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                packet = receiver.recv(1024)
                payload = packet[ARTNET_HEADER_BYTES:]
                if payload[: len(expected)] == expected:
                    assert not any(payload[len(expected) :])
                    break
            else:
                pytest.fail('Imported GDTF mode did not produce the expected DMX')
