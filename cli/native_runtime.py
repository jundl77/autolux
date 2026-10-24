"""Start, supervise and stop only the native processes owned by this session."""
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import subprocess
import time


@dataclass(frozen=True)
class NativeSpec:
    name: str
    executable: Path
    config: dict


class NativeRuntime:
    def __init__(self, specs, run_dir, *, startup_timeout=20.0, stop_timeout=5.0):
        self.specs = specs
        self.run_dir = Path(run_dir)
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.children = []
        self.job = None

    def __enter__(self):
        try:
            if os.name == 'nt':
                from cli.windows_runtime import Job
                self.job = Job()
            self.run_dir.mkdir(parents=True, exist_ok=False)
            for spec in self.specs:
                directory = self.run_dir / spec.name
                directory.mkdir()
                config = directory / 'config.json'
                config.write_text(json.dumps(spec.config, indent=2), encoding='utf-8')
                log_path = directory / 'process.log'
                with log_path.open('wb') as log:
                    child = subprocess.Popen(
                        [str(spec.executable), str(config)], cwd=spec.executable.parent,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        env={**os.environ, 'AUTOLUX_RUNTIME_DIR': str(directory)},
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.children.append((spec.name, child, directory))
                if self.job:
                    self.job.add(child.pid)
                deadline = time.monotonic() + self.startup_timeout
                while True:
                    self.check_health()
                    ready = directory / 'ready'
                    if ready.exists() and ready.read_text().strip() == 'autolux-native-v1':
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'{spec.name} did not become ready. Rebuild native binaries; see {log_path}')
                    time.sleep(0.05)
                logging.info('[prod] %s ready (PID %d)', spec.name, child.pid)
            return self
        except BaseException:
            self.close()
            raise

    def check_health(self):
        for name, process, directory in self.children:
            code = process.poll()
            if code is not None:
                raise RuntimeError(f'{name} exited with code {code}; stopping the session. See {directory / "process.log"}')

    def close(self):
        # Ask all children first, so one slow shutdown cannot delay the others.
        for _, process, directory in self.children:
            if process.poll() is None:
                try:
                    (directory / 'stop').touch()
                except OSError:
                    logging.exception('[prod] could not request native shutdown')
        deadline = time.monotonic() + self.stop_timeout
        try:
            for name, process, _ in reversed(self.children):
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    logging.warning('[prod] %s did not stop; terminating owned process', name)
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if self.job:
                self.job.close()
                self.job = None
        self.children.clear()

    def __exit__(self, *_):
        self.close()


def validate_native(spec, directory):
    """Validate the generated config, not the unmodified template."""
    path = Path(directory) / f'{spec.name}.json'
    path.write_text(json.dumps(spec.config), encoding='utf-8')
    result = subprocess.run([str(spec.executable), '--check-config', str(path)],
                            cwd=spec.executable.parent, capture_output=True, text=True,
                            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError(f'{spec.name} config check failed: {result.stdout}\n{result.stderr}')
