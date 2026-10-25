"""Autolux command line and application lifecycle coordination."""

import argparse
import argcomplete
import asyncio
import logging
import signal
import sys
from pathlib import Path

from autolux.lib.main import AutoluxController, PLAYBACK_DELAY_SEC
from autolux.lib.audio_config import SAMPLE_RATE, BUFFER_SIZE
from autolux.lib.ui_bridge import DEFAULT_DASHBOARD_PORT

global_app = None


async def run_cmd(args: argparse.Namespace):
    global global_app
    if args.debug:
        print('starting in debug mode')
        debug_mode = True
    else:
        debug_mode = False

    midi_port_index: int = int(args.midi_port_index)
    input_device_index = int(args.input_device) if args.input_device is not None else None
    output_device_index = int(args.output_device) if args.output_device is not None else None
    global_app = AutoluxController(
        midi_port_index=midi_port_index,
        input_device_index=input_device_index,
        output_device_index=output_device_index,
        debug_mode=debug_mode,
        disable_os2l=args.no_os2l,
        enable_ui=args.ui,
        ui_port=args.ui_port,
        report_path=args.report,
    )

    try:
        await global_app.run()
        write_report(global_app, args.report)
    finally:
        global_app = None


def write_report(app, path):
    if path and app.event_buffer is not None:
        import json
        from autolux.simulate.evaluator import evaluate, print_evaluation

        report = app.event_buffer.to_report()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, default=str)
        logging.info(f'[main] report written: {path}')
        print_evaluation(evaluate(report))


async def list_cmd(args: argparse.Namespace):
    from autolux.lib.clients.pyaudio_client import PyAudioClient
    from autolux.lib.clients.midi_client import MidiClient

    PyAudioClient(SAMPLE_RATE, BUFFER_SIZE, None, None).list_devices()
    MidiClient(0).list_devices()


async def label_cmd(args: argparse.Namespace):
    """Load the annotation tool only when requested, keeping Dash optional."""
    import sys
    from pathlib import Path

    training = str(Path(__file__).resolve().parents[1] / 'autolux' / 'training')
    if training not in sys.path:
        sys.path.insert(0, training)
    import label_tool

    label_tool.launch(args.audio, port=args.port, seed=args.seed)


def death_handler(signum, frame):
    """These handlers are installed at import, so every subcommand gets them --
    including the ones that never build a show. Returning without a show would
    be the end of it: the interrupt is delivered, consumed, and never becomes a
    KeyboardInterrupt, so anything blocked in a serve loop keeps serving and
    Ctrl-C looks broken. Raise what the default handler would have."""
    if global_app is None:
        raise KeyboardInterrupt
    logging.info('[main] stop requested; shutting down controller and receiver')
    global_app.stop()


signal.signal(signal.SIGINT, death_handler)
signal.signal(signal.SIGTERM, death_handler)


def run_sync():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError, ValueError) as error:
        logging.error('%s', error)
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(help='Functionality to start')

    from cli import production

    prod = subparsers.add_parser(
        'prod', help='Supervise the production controller or receiver (also --prod)'
    )
    production.add_arguments(prod)
    prod.set_defaults(func=production.run)
    subparsers.add_parser('status', help='Show current system health').set_defaults(
        func=production.status
    )
    subparsers.add_parser(
        'stop', help='Stop the owned controller and receiver session'
    ).set_defaults(func=production.stop)

    subparser = subparsers.add_parser('list', help='List all available sound and midi devices')
    subparser.set_defaults(func=list_cmd)

    subparser = subparsers.add_parser('run', help='Create the specified instance')
    subparser.add_argument(
        '--verbose', action='store_true', help='Include beat, note and protocol diagnostics'
    )
    subparser.add_argument(
        'midi_port_index',
        help='The midi port index of the midi device to use. Available devices are shown by running \'list\'',
    )
    subparser.add_argument(
        '-i',
        '--input_device',
        help='Specify the index of the sound INPUT device to use, uses system-default by default',
        required=False,
        default=None,
    )
    subparser.add_argument(
        '-o',
        '--output_device',
        help='Specify the index of the sound OUTPUT device to use, uses system-default by default',
        required=False,
        default=None,
    )
    subparser.add_argument(
        '-d',
        '--debug',
        help='Run in debug mode, this will playback audio on the output device with additional auditory information',
        required=False,
        action='store_true',
    )
    subparser.add_argument(
        '--no-os2l',
        help='Disable OS2L (connection to SoundSwitch). This can be useful for debugging.',
        required=False,
        action='store_true',
    )
    subparser.add_argument(
        '--ui',
        help='Launch real-time lighting visualizer (requires dash extra)',
        required=False,
        action='store_true',
    )
    subparser.add_argument(
        '--ui-port',
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help=f'Dashboard port (default: {DEFAULT_DASHBOARD_PORT})',
        required=False,
        dest='ui_port',
    )
    subparser.add_argument(
        '--report',
        default=None,
        help='Write a JSON session report on exit (e.g. report.json); implies event tracking',
        required=False,
    )
    subparser.set_defaults(func=run_cmd)

    subparser = subparsers.add_parser(
        'label', help='Hand-label a song into sections in the browser (requires dash extra)'
    )
    subparser.add_argument('audio', help='Path to the audio file to label')
    subparser.add_argument(
        '--port', type=int, default=8070, help='Labeler Dash server port (default: 8070)'
    )
    subparser.add_argument(
        '--seed',
        action='store_true',
        help="Start from the published section boundaries when there is no working file yet, so only the names need changing",
    )
    subparser.set_defaults(func=label_cmd)

    from autolux.simulate.cli import add_simulate_subparser
    from cli.starfire import add_subparser as add_starfire_subparser

    add_simulate_subparser(subparsers)
    add_starfire_subparser(subparsers)
    return parser


async def main():
    parser = build_parser()
    argcomplete.autocomplete(parser)
    arguments = sys.argv[1:]
    if arguments and arguments[0] == '--prod':
        arguments = ['prod', *arguments[1:]]
    args = parser.parse_args(arguments)
    from autolux.lib.logging_config import configure_logging

    configure_logging(verbose=getattr(args, 'verbose', False))

    if not hasattr(args, 'func'):
        parser.print_help()
        return

    await args.func(args)
