import datetime
import json
import logging
import os
import queue
import tempfile
from pathlib import Path

from autolux.lib.audio_config import BUFFER_SIZE, SAMPLE_RATE
from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    DEFAULT_AUTOLOOPS,
    AutoloopMetaTable,
    AutoloopTable,
    ShowSettings,
)
from autolux.lib.ui_bridge import DEFAULT_DASHBOARD_PORT

LOGGER = logging.getLogger(__name__)

# Production mode copies this to the native audio sender configuration.
# simulate/runner.py uses the same delay for replay.
PLAYBACK_DELAY_SEC = 14.0
_UI_ONLY_WINDOW_SEC = 60.0
SHOW_CONFIG_PATH = Path(__file__).resolve().parents[2] / '.live' / 'show.json'
CONTROL_QUEUE_SIZE = 16


def load_show_settings(path: Path) -> ShowSettings:
    try:
        config = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return ShowSettings()
    try:
        return ShowSettings.from_dict(config.get('settings', {}))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f'{path}: invalid show settings ({error})') from None


def save_show_settings(path: Path, settings: ShowSettings) -> None:
    path = Path(path)
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        config = {}
    config['settings'] = settings.to_dict()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        'w', encoding='utf-8', dir=path.parent, prefix='.show-', suffix='.json', delete=False
    ) as staged:
        json.dump(config, staged, indent=2, allow_nan=False)
        staged.write('\n')
    try:
        os.replace(staged.name, path)
    except OSError:
        Path(staged.name).unlink(missing_ok=True)
        raise


class ShowControl:
    def __init__(self, config_path: Path, event_buffer=None):
        self.config_path = Path(config_path)
        self.settings = load_show_settings(self.config_path)
        self.commands: queue.Queue = queue.Queue(maxsize=CONTROL_QUEUE_SIZE)
        self._event_buffer = event_buffer

    async def apply_pending(self, light_engine) -> None:
        applied = False
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            applied = True
            action = command['action']
            if action == 'pause':
                light_engine.pause()
            elif action == 'play':
                await light_engine.resume()
            elif action == 'next_autoloop':
                try:
                    light_engine.next_autoloop()
                except ValueError as error:
                    LOGGER.warning('[main] next autoloop refused: %s', error)
            elif action == 'master':
                light_engine.set_master_level(command['level'])
            elif action == 'settings':
                try:
                    save_show_settings(self.config_path, command['settings'])
                except OSError:
                    LOGGER.exception('[main] show settings were not saved; keeping the old ones')
                    continue
                self.settings = command['settings']
                light_engine.settings_changed()
                LOGGER.info('[main] show settings %s', self.settings.to_dict())
        if applied:
            self.publish(light_engine)

    def start(self, light_engine) -> None:
        light_engine.send_renderer_settings()

    def publish(self, light_engine) -> None:
        if self._event_buffer is not None:
            self._event_buffer.set_control_state(
                paused=light_engine.paused,
                master_brightness=light_engine.master_level,
                settings=self.settings.to_dict(),
            )


class AutoluxController:
    def __init__(
        self,
        lighting_client: StarfireClient,
        input_device_index: int | None = None,
        output_device_index: int | None = None,
        debug_mode: bool = False,
        enable_ui: bool = False,
        ui_port: int = DEFAULT_DASHBOARD_PORT,
        report_path: str | None = None,
        enable_monitoring: bool = False,
        fixture_setup: dict | None = None,
        show_config_path: Path = SHOW_CONFIG_PATH,
        autoloops: AutoloopTable = DEFAULT_AUTOLOOPS,
        autoloop_meta: AutoloopMetaTable = DEFAULT_AUTOLOOP_META,
        selection_seed: int = 0,
    ):
        from autolux.lib.analyser.music_analyser import MusicAnalyser
        from autolux.lib.clients.pyaudio_client import PyAudioClient
        from autolux.lib.delayed_monitor import DelayedMonitor
        from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
        from autolux.lib.engine.effect_controller import EffectController
        from autolux.lib.engine.light_engine import LightEngine

        self.lighting_client = lighting_client
        self.enable_ui: bool = enable_ui
        self._ui_port: int = ui_port
        self._ui = None
        self._fixture_setup = fixture_setup
        self.is_running: bool = False
        self._stop_requested = False
        self.health_check = None
        self.command_queue: DelayedCommandQueue = DelayedCommandQueue(PLAYBACK_DELAY_SEC)

        self._enable_playback: bool = debug_mode or output_device_index is not None
        self._monitor = DelayedMonitor(PLAYBACK_DELAY_SEC, self._play_monitored)
        self.audio_client: PyAudioClient = PyAudioClient(
            SAMPLE_RATE, BUFFER_SIZE, input_device_index, output_device_index
        )

        from autolux.lib.engine.event_buffer import EventBuffer

        self.event_buffer: EventBuffer | None = (
            EventBuffer(
                look_ahead_sec=PLAYBACK_DELAY_SEC,
                window_sec=(float('inf') if report_path else _UI_ONLY_WINDOW_SEC),
            )
            if (enable_ui or report_path or enable_monitoring)
            else None
        )

        from autolux.lib import section_chain
        from autolux.lib.analyser.drift_watchdog import DriftWatchdog

        self.drift_watchdog: DriftWatchdog = DriftWatchdog(BUFFER_SIZE / SAMPLE_RATE)
        self.section = (
            section_chain.build_section_chain(watchdog=self.drift_watchdog)
            if section_chain.artifacts_present()
            else None
        )
        if self.section is None:
            LOGGER.warning(
                '[main] no NN artifacts on this machine — the show '
                'will light the quiet cold-start floor and hold it '
                '(beats and silence still run)'
            )

        self.control = ShowControl(show_config_path, self.event_buffer)
        self.effect_controller: EffectController = EffectController(
            self.lighting_client,
            event_buffer=self.event_buffer,
            settings=lambda: self.control.settings,
            autoloops=autoloops,
            autoloop_meta=autoloop_meta,
            seed=selection_seed,
        )
        self.light_engine: LightEngine = LightEngine(
            self.lighting_client,
            self.effect_controller,
            self.command_queue,
            event_buffer=self.event_buffer,
            playback_delay_sec=PLAYBACK_DELAY_SEC,
            section_chain=None if self.section is None else self.section.stream,
            section_decoder=None if self.section is None else self.section.decoder,
            watchdog=self.drift_watchdog,
            settings=lambda: self.control.settings,
            silence_monitor=self._monitor.silence,
        )
        self.control.publish(self.light_engine)

        self.music_analyser: MusicAnalyser = MusicAnalyser(
            SAMPLE_RATE,
            BUFFER_SIZE,
            self.light_engine,
            note_clicks=debug_mode,
            watchdog=self.drift_watchdog,
            settings=lambda: self.control.settings,
        )
        self.light_engine.set_analyser(self.music_analyser)

    def _play_monitored(self, audio) -> None:
        self.audio_client.play(audio)

    def list_devices(self):
        self.audio_client.list_devices()

    async def run(self):
        LOGGER.info("[main] setting up Autolux..")
        try:
            await self._run()
        finally:
            self._shut_down()

    async def _run(self):
        self.audio_client.start_streams(start_stream_out=self._enable_playback)
        if self.event_buffer is not None:
            self.event_buffer.start()
        if self.enable_ui:
            from autolux.lib import ui_bridge

            self._ui = ui_bridge.start(
                self.event_buffer,
                self._ui_port,
                commands=self.control.commands,
                **({'fixture_setup': self._fixture_setup} if self._fixture_setup else {}),
            )
        if self._stop_requested:
            return
        self.is_running = True

        LOGGER.info("[main] Autolux is ready, starting")
        self.control.start(self.light_engine)

        last_100ms_callback_execution: datetime.datetime = datetime.datetime.now()
        last_1sec_callback_execution: datetime.datetime = datetime.datetime.now()
        last_10sec_callback_execution: datetime.datetime = datetime.datetime.now()
        self._monitor.arm()

        while self.is_running:
            now = datetime.datetime.now()
            audio_signal = self.audio_client.read()
            await self.light_engine.on_audio(audio_signal)
            new_audio_signal = await self.music_analyser.analyse(audio_signal)
            await self.command_queue.drain()

            if self.audio_client.support_output():
                self._monitor.feed(new_audio_signal)

            if now - last_100ms_callback_execution > datetime.timedelta(milliseconds=100):
                last_100ms_callback_execution = now
                await self._do_100ms_callback()

            if now - last_1sec_callback_execution > datetime.timedelta(seconds=1):
                last_1sec_callback_execution = now
                await self._do_1s_callback()

            if now - last_10sec_callback_execution > datetime.timedelta(seconds=10):
                last_10sec_callback_execution = now
                await self._do_10s_callback()

    def _shut_down(self) -> None:
        self.is_running = False
        for what, close in (
            ('visualizer', None if self._ui is None else self._ui.stop),
            ('audio', self.audio_client.close),
            ('section chain', None if self.section is None else self.section.stop),
            ('starfire', self.lighting_client.stop),
        ):
            if close is None:
                continue
            try:
                close()
            except Exception:
                LOGGER.exception('[main] %s did not close cleanly', what)
        LOGGER.info("[main] Autolux stopped, clean shutdown")

    def stop(self):
        self._stop_requested = True
        self.is_running = False

    async def _do_100ms_callback(self):
        if self.health_check is not None:
            self.health_check()
        await self.control.apply_pending(self.light_engine)
        await self.light_engine.on_100ms_callback()

    async def _do_1s_callback(self):
        await self.light_engine.on_1sec_callback()

    async def _do_10s_callback(self):
        await self.light_engine.on_10sec_callback()
