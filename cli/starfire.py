from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from autolux.lib.ui_bridge import DEFAULT_DASHBOARD_PORT
from autolux.lighting.starfire_replay import DEFAULT_FRAME_RATE_HZ, report_to_replay
from cli.starfire_assets import native_rig, rig_bytes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / 'target' / 'release' / ('starfire.exe' if os.name == 'nt' else 'starfire')
DEFAULT_RIG = ROOT / 'rig' / 'room.mvr'
DEFAULT_AUTOLOOPS = ROOT / 'show' / 'autoloops'
DEFAULT_FIXTURE_STORE = ROOT / '.live' / 'starfire' / 'rig.mvr'
LEGACY_FIXTURE_STORE = ROOT / '.live' / 'starfire' / 'rig.json'
FIXTURE_LIBRARY = ROOT / 'rig' / 'library'
CHILD_STOP_TIMEOUT_SEC = 5.0
PATCH_VALIDATION_TIMEOUT_SEC = 10.0
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_TCP_PORT = 65535
LOGGER = logging.getLogger(__name__)


def saved_rig(library: Path = FIXTURE_LIBRARY) -> Path:
    if DEFAULT_FIXTURE_STORE.is_file():
        from autolux.lighting.fixture_setup import refresh_profiles

        # The store was seeded once; profiles fixed in the library since must still reach it.
        refresh_profiles(DEFAULT_FIXTURE_STORE, library)
        return DEFAULT_FIXTURE_STORE
    if LEGACY_FIXTURE_STORE.is_file():
        raise RuntimeError(f'rig.json is no longer supported; the rig is {DEFAULT_FIXTURE_STORE}')
    return DEFAULT_RIG


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser('starfire', help='Replay lighting or edit the fixture patch')
    parser.add_argument(
        '--binary', type=Path, default=DEFAULT_BINARY, help='Prebuilt renderer path'
    )
    actions = parser.add_subparsers(dest='starfire_action', required=True)

    replay = actions.add_parser('replay', help='Render native replay input; network output is off')
    replay.add_argument('input', type=Path)
    replay.add_argument('--rig', type=Path, help='MVR rig; defaults to the saved setup or room rig')
    replay.add_argument('--autoloops', type=Path, default=DEFAULT_AUTOLOOPS)
    replay.add_argument('--output', type=Path, help='Write a binary DMX capture')
    replay.add_argument('--summary', type=Path, help='Write render metrics and DMX digest')
    replay.add_argument('--artnet', help='Explicit destination IPv4:port; requires --realtime')
    replay.add_argument('--realtime', action='store_true')
    replay.add_argument('--broadcast', action='store_true')
    replay.set_defaults(func=run_native)

    listing = actions.add_parser('list', help='List autoloop banks and names')
    listing.add_argument('--autoloops', type=Path, default=DEFAULT_AUTOLOOPS)
    listing.set_defaults(func=run_native)
    rig = actions.add_parser('rig', help='Validate an MVR rig without output')
    rig.add_argument('input', type=Path)
    rig.set_defaults(func=run_native)
    export = actions.add_parser('export-rig', help='Write the Starfire RigDefinition of an MVR rig')
    export.add_argument('input', type=Path)
    export.add_argument('output', type=Path)
    export.set_defaults(func=export_rig)

    importer = actions.add_parser(
        'import-fixture', help='Install a GDTF archive once Starfire renders one of its modes'
    )
    importer.add_argument('input', type=Path)
    importer.add_argument('--mode', required=True, help='Exact DMX mode name to validate')
    importer.add_argument('--library', type=Path, default=FIXTURE_LIBRARY)
    importer.set_defaults(func=run_import)

    convert = actions.add_parser(
        'convert-report',
        help='Convert report beats/intents to source-time replay; excludes unrecorded accents',
    )
    convert.add_argument('input', type=Path)
    convert.add_argument('output', type=Path)
    convert.add_argument('--frame-rate-hz', type=int, default=DEFAULT_FRAME_RATE_HZ)
    convert.set_defaults(func=convert_report)

    ui = actions.add_parser('ui', help='Open the existing dashboard with fixture setup')
    ui.add_argument('--port', type=int, default=DEFAULT_DASHBOARD_PORT)
    ui.add_argument('--rig-store', type=Path, default=DEFAULT_FIXTURE_STORE)
    ui.add_argument('--library', type=Path, default=FIXTURE_LIBRARY)
    ui.add_argument('--autoloops', type=Path, default=DEFAULT_AUTOLOOPS)
    ui.add_argument('--expected-universe', type=int, help="The show's one DMX universe")
    ui.add_argument('--snapshot-port', type=int, help='Read the running controller snapshot')
    ui.set_defaults(func=run_ui)


def _binary(path: Path) -> Path:
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError(f'Starfire binary is missing: {path}. Build it with build-starfire.ps1.')
    return path


async def _run_child(command: list[str], *, stop_file: Path | None = None) -> None:
    child = await asyncio.create_subprocess_exec(
        *command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        code = await child.wait()
        if code != 0:
            raise RuntimeError(f'Starfire exited with code {code}')
    finally:
        if child.returncode is None:
            try:
                if stop_file is not None:
                    stop_file.touch()
                else:
                    child.terminate()
            except ProcessLookupError:
                pass
            except OSError as error:
                LOGGER.warning('[starfire] graceful shutdown request failed: %s', error)
            try:
                await asyncio.wait_for(child.wait(), CHILD_STOP_TIMEOUT_SEC)
            except TimeoutError:
                LOGGER.warning('[starfire] shutdown timed out; terminating owned renderer')
                child.kill()
                await child.wait()


async def run_native(args: argparse.Namespace) -> None:
    command = [str(_binary(args.binary)), args.starfire_action]
    rig = getattr(args, 'rig', None)
    if args.starfire_action == 'rig':
        rig = args.input
    elif hasattr(args, 'input'):
        command.append(str(args.input.resolve()))
        rig = rig or saved_rig()
    for name in ('autoloops', 'output', 'summary'):
        if (path := getattr(args, name, None)) is not None:
            command.extend([f'--{name}', str(path.resolve())])
    if getattr(args, 'artnet', None):
        if not args.realtime:
            raise ValueError('Art-Net output requires --realtime')
        command.extend(['--artnet', args.artnet])
    if getattr(args, 'broadcast', False) and not getattr(args, 'artnet', None):
        raise ValueError('--broadcast requires --artnet')
    for name in ('realtime', 'broadcast'):
        if getattr(args, name, False):
            command.append(f'--{name}')
    with contextlib.ExitStack() as stack:
        if rig is not None:
            exported = str(stack.enter_context(native_rig(rig)))
            command.extend([exported] if args.starfire_action == 'rig' else ['--rig', exported])
        if getattr(args, 'artnet', None):
            runtime = ROOT / '.live' / 'starfire'
            runtime.mkdir(parents=True, exist_ok=True)
            owned = stack.enter_context(tempfile.TemporaryDirectory(prefix='replay-', dir=runtime))
            stop_file = Path(owned) / 'stop'
            command.extend(['--stop-file', str(stop_file)])
            await _run_child(command, stop_file=stop_file)
        else:
            await _run_child(command)


async def export_rig(args: argparse.Namespace) -> None:
    args.output.write_bytes(rig_bytes(args.input))


async def convert_report(args: argparse.Namespace) -> None:
    if args.input.resolve() == args.output.resolve():
        raise ValueError('Replay output must differ from the input report')
    with args.input.open('rb') as source:
        raw = source.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError('Session report exceeds the input size limit')
    try:
        replay = report_to_replay(json.loads(raw), frame_rate_hz=args.frame_rate_hz)
    except TypeError as error:
        raise ValueError(f'Invalid session report: {error}') from error
    args.output.write_text(json.dumps(replay, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(f'Source-time replay: {args.output} ({len(replay["events"])} commands; accents omitted)')


def _validate_patch(binary: Path, patch: Path) -> bytes:
    try:
        with native_rig(patch) as exported:
            definition = exported.read_bytes()
            completed = subprocess.run(
                [str(binary), 'rig', str(exported)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=PATCH_VALIDATION_TIMEOUT_SEC,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Starfire fixture validation timed out') from error
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or 'Starfire rejected the fixture patch')
    return definition


def patch_validator(binary: Path, autoloops: Path, expected_universe: int | None = None):
    """Save runs the show's start check too, so a patch the show would refuse fails at Save."""
    from cli.starfire_runtime import validate_starfire

    def validate(patch: Path) -> bytes:
        definition = _validate_patch(binary, patch)
        validate_starfire(binary, patch, autoloops, expected_universe=expected_universe)
        return definition

    return validate


def run_import(args: argparse.Namespace) -> None:
    from autolux.lighting.gdtf import import_fixture

    binary = _binary(args.binary)
    metadata = import_fixture(
        args.input.resolve(),
        args.library.resolve(),
        args.mode,
        lambda patch: _validate_patch(binary, patch),
    )
    print(f'Installed {metadata["manufacturer"]} {metadata["name"]}: {metadata["notes"]}')


def run_ui(args: argparse.Namespace) -> None:
    from autolux.lib.engine.event_buffer import EventBuffer
    from autolux.lighting.fixture_setup import FixtureStore, refresh_profiles
    from autolux.simulate.visualizer_app import run_app

    if not 1 <= args.port <= MAX_TCP_PORT:
        raise ValueError(f'Dashboard port must be in 1..{MAX_TCP_PORT}')
    binary = _binary(args.binary)
    patch = args.rig_store.resolve()
    library = args.library.resolve()
    if not patch.exists():
        if patch == DEFAULT_FIXTURE_STORE:
            saved_rig()
        _validate_patch(binary, DEFAULT_RIG)
        patch.parent.mkdir(parents=True, exist_ok=True)
        try:
            with patch.open('xb') as target:
                target.write(DEFAULT_RIG.read_bytes())
        except FileExistsError:
            pass
    refresh_profiles(patch, library)
    _validate_patch(binary, patch)
    # Only the default store mirrors to the tracked rig; another --rig-store is not the room.
    repo_rig = DEFAULT_RIG if patch == DEFAULT_FIXTURE_STORE else None
    validate = patch_validator(binary, args.autoloops.resolve(), args.expected_universe)
    store = FixtureStore(patch, library, validate, repo_rig)
    source = EventBuffer()
    if args.snapshot_port is not None:
        from autolux.simulate.visualizer_app import SnapshotPoller

        if not 1 <= args.snapshot_port <= MAX_TCP_PORT:
            raise ValueError(f'Snapshot port must be in 1..{MAX_TCP_PORT}')
        source = SnapshotPoller(port=args.snapshot_port)
    run_app(source, port=args.port, fixture_store=store)
