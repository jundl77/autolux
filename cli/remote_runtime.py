"""Deploy an immutable Radius bundle and supervise one SSH-owned receiver session."""

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiverSpec:
    executable: Path
    config: dict


def encoded_command(script):
    return [
        'powershell.exe',
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-EncodedCommand',
        base64.b64encode(script.encode('utf-16le')).decode('ascii'),
    ]


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class RemoteRuntime:
    def __init__(self, spec, host, user, key, known_hosts, run_dir, *, timeout=25):
        ipaddress.IPv4Address(host)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', user):
            raise ValueError('SSH user must be a Windows account name')
        self.spec, self.host, self.user = spec, host, user
        self.key, self.known_hosts = Path(key).expanduser(), Path(known_hosts).expanduser()
        self.run_dir, self.timeout = Path(run_dir), timeout
        self.session_id = uuid.uuid4().hex
        self.process = None
        self.remote = None
        self._log = None

    def options(self):
        for path in (self.key, self.known_hosts):
            if not path.is_file():
                raise RuntimeError(
                    f'Missing SSH setup file: {path}. See cli/deployment/enable-ssh.ps1.'
                )
        return [
            '-i',
            str(self.key),
            '-o',
            'BatchMode=yes',
            '-o',
            'StrictHostKeyChecking=yes',
            '-o',
            f'UserKnownHostsFile={self.known_hosts}',
            '-o',
            'ConnectTimeout=5',
            '-o',
            'ServerAliveInterval=10',
            '-o',
            'ServerAliveCountMax=3',
        ]

    def ssh(self, script, *, timeout=15):
        result = subprocess.run(
            [
                'ssh',
                *self.options(),
                f'{self.user}@{self.host}',
                *encoded_command(
                    "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
                    + script
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode:
            raise RuntimeError(
                f'Receiver SSH failed: {result.stderr.strip() or result.stdout.strip()}'
            )
        return result.stdout.strip()

    def check_connection(self):
        self.ssh('Write-Output radius-ssh-ready')

    def prepare(self):
        """Deploy and check receiver devices without opening playback or DMX."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        files = [
            self.spec.executable,
            Path(__file__).parent / 'deployment' / 'run-radius.ps1',
        ]
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.name.encode())
            digest.update(file.read_bytes())
        version = digest.hexdigest()[:20]
        script = (
            "$base=Join-Path $env:LOCALAPPDATA 'Autolux'; "
            f"$bundle=Join-Path $base 'radius/{version}'; "
            f"$run=Join-Path $base 'runs/{self.session_id}'; "
            "New-Item -ItemType Directory -Force -Path $bundle,$run | Out-Null; "
            "@{bundle=$bundle;run=$run}|ConvertTo-Json -Compress"
        )
        self.remote = json.loads(self.ssh(script))
        config = self.run_dir / 'receiver.json'
        config.write_text(json.dumps(self.spec.config), encoding='utf-8')
        for sources, destination in (
            (files, self.remote['bundle']),
            ([config], self.remote['run']),
        ):
            # scp's SFTP transport accepts a single destination argument, including spaces.
            target = f'{self.user}@{self.host}:' + destination.replace('\\', '/') + '/'
            subprocess.run(
                ['scp', *self.options(), *map(str, sources), target],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        bundle, run = map(ps_literal, (self.remote['bundle'], self.remote['run']))
        self.ssh(
            f'& (Join-Path {bundle} radius_rx.exe) --check-config (Join-Path {run} receiver.json); exit $LASTEXITCODE'
        )

    def __enter__(self):
        try:
            if self.remote is None:
                self.prepare()
            bundle, run = map(ps_literal, (self.remote['bundle'], self.remote['run']))
            self._log = (self.run_dir / 'receiver-ssh.log').open('wb')
            script = f'& (Join-Path {bundle} run-radius.ps1) -Bundle {bundle} -RunDirectory {run}; exit $LASTEXITCODE'
            self.process = subprocess.Popen(
                ['ssh', *self.options(), f'{self.user}@{self.host}', *encoded_command(script)],
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                self.check_health()
                ready = self.ssh(
                    f'$p=Join-Path {run} ready; if(Test-Path -LiteralPath $p){{Get-Content -LiteralPath $p}}'
                )
                if ready == 'ready':
                    LOGGER.info('[prod] receiver ready on %s', self.host)
                    return self
                time.sleep(0.2)
            raise RuntimeError(
                f'Receiver startup timed out. See {self.run_dir / "receiver-ssh.log"}'
            )
        except BaseException:
            self.close()
            raise

    def check_health(self):
        if self.process and self.process.poll() is not None:
            raise RuntimeError(
                f'Receiver SSH session exited ({self.process.returncode}); see {self.run_dir / "receiver-ssh.log"}'
            )

    def close(self):
        if self.remote and self.process:
            try:
                run = ps_literal(self.remote['run'])
                self.ssh(
                    f'New-Item -ItemType File -Force -Path (Join-Path {run} stop) | Out-Null',
                    timeout=8,
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                LOGGER.exception(
                    '[prod] remote shutdown request failed; receiver heartbeat timeout remains active'
                )
            try:
                code = self.process.wait(timeout=8)
                if code == 0:
                    LOGGER.info('[prod] receiver stopped on %s', self.host)
                else:
                    LOGGER.warning(
                        '[prod] receiver SSH exited with code %s; see %s',
                        code,
                        self.run_dir / 'receiver-ssh.log',
                    )
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self._log:
            self._log.close()
            self._log = None

    def __exit__(self, *_):
        self.close()
