import asyncio
import dataclasses
import datetime
import json
import logging
import os
import queue
import tempfile
from pathlib import Path

from autolux.lib.audio_config import BUFFER_SIZE, SAMPLE_RATE
from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.effect_definitions import (
    DEFAULT_AUTOLOOP_META,
    DEFAULT_AUTOLOOPS,
    AutoloopMetaTable,
    AutoloopTable,
    ShowSettings,
)
from autolux.lib.engine.static_mode import StaticRig
from autolux.lib.ui_bridge import DEFAULT_DASHBOARD_PORT, PALETTE_POLICY

LOGGER = logging.getLogger(__name__)

# Production mode copies this to the native audio sender configuration.
# simulate/runner.py uses the same delay for replay.
PLAYBACK_DELAY_SEC = 14.0
_UI_ONLY_WINDOW_SEC = 60.0
SHOW_CONFIG_PATH = Path(__file__).resolve().parents[2] / '.live' / 'show.json'
CONTROL_QUEUE_SIZE = 16
SETTINGS_SAVE_SETTLE_SECONDS = 1.0
# One Starfire frame at 40 Hz: draining operator commands faster cannot reach the rig sooner.
STARFIRE_FRAME_PERIOD_SEC = 1.0 / 40.0


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


def apply_static(light_engine, command: dict) -> None:
    looks = light_engine.static()
    kind = command['kind']
    if kind == 'look':
        light_engine.static_look(
            command['target'], colour=command['colour'], dimmer=command['dimmer']
        )
    elif kind == 'preset':
        looks.preset(command['preset'])
    elif kind == 'blacklight':
        looks.blacklight()
    elif kind == 'blackout':
        looks.set_blackout(command['enabled'])
    elif kind == 'strobe':
        looks.strobe(command.get('enabled'))
    elif kind == 'special':
        looks.special(
            command['group'],
            command['colour_slot'],
            command['energy'],
            command['speed'],
            command['pulse_beats'],
        )
    elif kind == 'special_clear':
        looks.clear_special()


class ShowControl:
    def __init__(self, config_path: Path, event_buffer=None, clock: Clock = SYSTEM_CLOCK):
        self.config_path = Path(config_path)
        self.settings = load_show_settings(self.config_path)
        self.commands: queue.Queue = queue.Queue(maxsize=CONTROL_QUEUE_SIZE)
        self._event_buffer = event_buffer
        self._clock = clock
        self._unsaved_change_at: float | None = None

    async def apply_pending(self, light_engine) -> None:
        applied = False
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            applied = True
            try:
                await self._apply(light_engine, command)
            finally:
                # Releases the bridge request waiting for this hand-off.
                self.commands.task_done()
        if applied:
            self.publish(light_engine)
        if (
            self._unsaved_change_at is not None
            and self._clock.monotonic() - self._unsaved_change_at >= SETTINGS_SAVE_SETTLE_SECONDS
        ):
            self.save_settings()

    async def _apply(self, light_engine, command: dict) -> None:
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
        elif action == 'mode':
            if command['mode'] == 'static':
                light_engine.enter_static()
            else:
                await light_engine.leave_static()
        elif action == 'static':
            try:
                apply_static(light_engine, command)
            except ValueError as error:
                LOGGER.warning('[main] static command refused: %s', error)
        elif action == 'palette':
            light_engine.effect_controller.pin_palette(
                None if command['name'] == PALETTE_POLICY else command['name']
            )
        elif action in ('settings', 'intensity'):
            settings = (
                command['settings']
                if action == 'settings'
                else dataclasses.replace(self.settings, intensity=command['level'])
            )
            if settings == self.settings:
                return
            self.settings = settings
            if action == 'intensity':
                light_engine.intensity_changed()
            else:
                light_engine.settings_changed()
            self._unsaved_change_at = self._clock.monotonic()

    def save_settings(self) -> None:
        if self._unsaved_change_at is None:
            return
        self._unsaved_change_at = None
        try:
            save_show_settings(self.config_path, self.settings)
        except OSError:
            LOGGER.exception('[main] show settings were not saved; they last until the show stops')
            return
        LOGGER.info('[main] show settings %s', self.settings.to_dict())

    def start(self, light_engine) -> None:
        light_engine.send_renderer_settings()

    def publish(self, light_engine) -> None:
        if self._event_buffer is not None:
            self._event_buffer.set_control_state(
                paused=light_engine.paused,
                master_brightness=light_engine.master_level,
                settings=self.settings.to_dict(),
                mode=light_engine.mode,
                static=light_engine.static_looks.state(),
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
        static_rig: StaticRig = StaticRig(),
    ):
        from autolux.lib.analyser.music_analyser import MusicAnalyser
        from autolux.lib.clients.pyaudio_client import PyAudioClient
        from autolux.lib.delayed_monitor import DelayedMonitor
        from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
        from autolux.lib.engine.effect_controller import EffectController
        from autolux.lib.engine.light_engine import LightEngine
        from autolux.lighting.special_catalogue import discover_specials

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
            specials=discover_specials(),
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
            static_rig=static_rig,
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

        self._monitor.arm()
        drain = asyncio.create_task(self._drain_operator_commands())
        try:
            await self._play(drain)
        finally:
            drain.cancel()

    async def _drain_operator_commands(self) -> None:
        while True:
            await asyncio.sleep(STARFIRE_FRAME_PERIOD_SEC)
            await self.control.apply_pending(self.light_engine)

    async def _play(self, drain: asyncio.Task) -> None:
        last_100ms_callback_execution: datetime.datetime = datetime.datetime.now()
        last_1sec_callback_execution: datetime.datetime = datetime.datetime.now()
        last_10sec_callback_execution: datetime.datetime = datetime.datetime.now()
        while self.is_running:
            now = datetime.datetime.now()
            audio_signal = self.audio_client.read()
            # The blocking read never yields; this lets the drain task run once its period is up.
            await asyncio.sleep(0)
            if drain.done():
                drain.result()  # Surfaces a failed operator command instead of silently ignoring clicks.
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
            ('show settings', self.control.save_settings),
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
        await self.light_engine.on_100ms_callback()

    async def _do_1s_callback(self):
        await self.light_engine.on_1sec_callback()

    async def _do_10s_callback(self):
        await self.light_engine.on_10sec_callback()
