from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from autolux.lib.analyser.bar_tracker import TrackerChunk
from autolux.lib.analyser.music_analyser import MusicAnalyser
from autolux.lib.analyser.music_analyser_handler import IMusicAnalyserHandler
from autolux.lib.audio_config import SAMPLE_RATE
from autolux.lib.clients.starfire_client import StarfireClient, rms_to_normalized_level
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import (
    BEAT_LOSS_SECONDS,
    QUIET_HOLD_SECONDS,
    QUIET_RMS,
    LightIntent,
    ShowSettings,
    intent_for_class,
)
from autolux.lib.engine.event_buffer import (
    SILENCE_TRIGGER,
    STANDBY_INTENT,
    STOP_PERSISTENCE_SEC,
)
from autolux.lib.engine.section_decoder import BEATS_PER_BAR

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

_LATENCY_LOG_STEP_SEC = 0.25
_SHED_PUBLISH_SEC = 1.0
COLD_START_FLOOR_MARGIN_SEC = 4.0
BRIGHTNESS_FADE_SECONDS = 1.0


class LightEngine(IMusicAnalyserHandler):
    """Decoder decisions in, a show out."""

    def __init__(
        self,
        lighting_client: StarfireClient,
        effect_controller: EffectController,
        command_queue: DelayedCommandQueue | None = None,
        event_buffer: EventBuffer | None = None,
        playback_delay_sec: float = 0.0,
        section_chain=None,
        section_decoder=None,
        watchdog=None,
        clock: Clock = SYSTEM_CLOCK,
        settings: Callable[[], ShowSettings] = ShowSettings,
        silence_monitor: Callable[[], None] | None = None,
    ):
        self.lighting_client = lighting_client
        self._silence_monitor = silence_monitor
        self._settings = settings
        self.effect_controller: EffectController = effect_controller
        self.command_queue: DelayedCommandQueue | None = command_queue
        self.event_buffer: EventBuffer | None = event_buffer
        self.analyser: MusicAnalyser = None
        self.section_chain = section_chain
        self.section_decoder = section_decoder
        self._watchdog = watchdog
        self._playback_delay_sec: float = playback_delay_sec
        self._clock: Clock = clock
        self._current_intent: LightIntent | None = None
        self._section_class_at_playback: str | None = None
        self._pending_intents: list = []
        self._published_bpm: dict = {}
        self._grid_beat: int | None = None
        self._room_beat_phase: int | None = None
        self._room_beat_at: float = float('-inf')
        self._held_intent: LightIntent | None = None
        self._intent_commits: int = 0
        self._audio_sec: float = 0.0
        self._last_logged_latency_seconds: float | None = None
        self._committing_late: bool = False
        self._floor_armed: bool = True
        self._source_playing = False
        self._room_playing = False
        self._room_start_monotonic_seconds: float | None = None
        self._startup_baseline_pending = False
        self._pending_silence: tuple[float, float | None] | None = None
        self._shed_published_at: float = float('-inf')
        self._committed = None
        self._quiet_since: float | None = None
        self._source_quiet = False
        # The quiet state as the room hears it, after the playback delay.
        self._room_quiet = False
        self._paused = False
        # Source side: analysis holds. Room side: the rig shows the standby look
        # until the first look of the returning audio replaces it.
        self._standby = False
        self._showing_standby = False
        self._master_level = 1.0
        # Starfire boots at full brightness, so only a change needs sending.
        self._sent_brightness = 1.0
        self._log_chain_latency()

    def set_analyser(self, analyser: MusicAnalyser):
        self.analyser: MusicAnalyser = analyser

    @property
    def current_intent(self) -> LightIntent | None:
        return self._current_intent

    @property
    def decided_intent(self) -> LightIntent | None:
        return self._pending_intents[-1][1] if self._pending_intents else self._current_intent

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def master_level(self) -> float:
        return self._master_level

    @property
    def standby(self) -> bool:
        return self._standby

    def pause(self) -> None:
        """Freeze the look: the running loop continues, decisions stop switching it."""
        if self._paused:
            return
        LOGGER.info('[engine] paused by the operator')
        self._paused = True
        self._held_intent = None
        self._send_brightness()

    async def resume(self) -> None:
        if not self._paused:
            return
        LOGGER.info('[engine] resumed by the operator')
        self._paused = False
        self._send_brightness()
        # The model kept deciding while paused; its latest intent takes the
        # next downbeat like any committed change.
        if self._current_intent is not None and self._room_playing:
            self._held_intent = self._current_intent
            if self._room_beat_phase is None:
                await self._switch_held_intent()

    def set_master_level(self, level: float) -> None:
        if type(level) not in (int, float) or not 0.0 <= level <= 1.0:
            raise ValueError('master brightness must be within 0..1')
        self._master_level = float(level)
        # The operator is turning a knob now, so this skips the playback delay.
        self._send_brightness()

    def settings_changed(self) -> None:
        self._send_brightness()

    def _send_brightness(self) -> None:
        # Standby sets its own low level; dimming it again would leave the room dark.
        dimmed = (self._room_quiet or self._paused) and not self._showing_standby
        brightness = self._master_level * (self._settings().quiet_brightness if dimmed else 1.0)
        if brightness == self._sent_brightness:
            return
        self._sent_brightness = brightness
        LOGGER.info(f'[engine] master brightness {brightness:.3f}')
        self.lighting_client.set_master(brightness, fade_seconds=BRIGHTNESS_FADE_SECONDS)

    @property
    def audio_sec(self) -> float:
        return self._audio_sec

    @property
    def intent_commits(self) -> int:
        return self._intent_commits

    def on_sound_start(self):
        LOGGER.info('[engine] sound start')
        self._source_playing = True
        delay_sec = self.command_queue.delay_sec if self.command_queue else 0.0
        self._room_start_monotonic_seconds = self._clock.monotonic() + delay_sec
        self._pending_silence = None
        if self.event_buffer:
            self.event_buffer.set_playing(True)
        self._reset_source_analysis()
        if self.command_queue:

            async def command():
                self._show_sound_start()
                await self._start_room_baseline()

            self.command_queue.schedule('sound', command)
        else:
            self._show_sound_start()

    def on_sound_stop(self):
        LOGGER.info('[engine] sound stop')
        self._source_playing = False
        if self.event_buffer:
            self.event_buffer.set_playing(False)
        self._pending_silence = (
            self._clock.monotonic(),
            self.event_buffer.elapsed() if self.event_buffer else None,
        )
        self._reset_source_analysis()

    def _schedule_confirmed_silence(self) -> None:
        if self._pending_silence is None:
            return
        stopped_at, song_sec = self._pending_silence
        now = self._clock.monotonic()
        if now < stopped_at + STOP_PERSISTENCE_SEC:
            return
        self._pending_silence = None
        LOGGER.info('[engine] silence confirmed; lighting stops with the buffered audio')

        def stop():
            self._show_sound_stop()
            if self.event_buffer:
                self.event_buffer.set_intent(
                    LightIntent.ATMOSPHERIC.value,
                    song_sec=song_sec,
                    trigger=SILENCE_TRIGGER,
                )

        if self.command_queue:

            async def command():
                stop()

            self.command_queue.schedule(
                'sound',
                command,
                delay_sec=max(0.0, stopped_at + self.command_queue.delay_sec - now),
            )
        else:
            stop()

    def on_digital_silence(self):
        """The source stopped: silence the room and park the rig now, not one delay later."""
        LOGGER.info(
            '[engine] digital silence — standby; the delayed audio and commands are dropped'
        )
        self._standby = True
        if self.command_queue:
            self.command_queue.clear()
        self.lighting_client.discard_delayed_audio()
        if self._silence_monitor is not None:
            self._silence_monitor()
        self._source_playing = False
        self._pending_silence = None
        self._room_start_monotonic_seconds = None
        self._pending_intents.clear()
        self._forget_room_look()
        # Nothing touches the quiet state again until the signal returns, so
        # clearing it here is the reset on return too.
        self._quiet_since = None
        self._source_quiet = False
        self._room_quiet = False
        self._showing_standby = True
        self._send_brightness()
        self._committed = None
        self._publish_decoder_state(None)
        if self.event_buffer is not None:
            self.event_buffer.set_intent(
                STANDBY_INTENT, song_sec=self.event_buffer.elapsed(), trigger=SILENCE_TRIGGER
            )
        self.effect_controller.enter_standby(self._published_bpm.get('last'))

    def on_signal_return(self):
        """Restart as if freshly launched; the rig stays in standby until the new
        audio reaches the room through the ordinary sound-start path, whose
        on_sound_start also resets the section chain and decoder."""
        LOGGER.info('[engine] signal returned — the show restarts from scratch')
        self._standby = False
        self._published_bpm.clear()
        self._room_beat_at = float('-inf')
        self._committing_late = False
        # _grid_beat keeps counting: Starfire rejects a beat behind its last one, and
        # the next decoder phase re-aligns the grid like any phase correction.

    def _forget_room_look(self) -> None:
        """The room no longer sees a show; the caller decides what it sees instead."""
        self._room_playing = False
        self._startup_baseline_pending = False
        self._current_intent = None
        self._section_class_at_playback = None
        self._held_intent = None
        self._room_beat_phase = None
        if self.event_buffer is not None:
            self.event_buffer.stop_effects()

    def _show_sound_start(self) -> None:
        self._room_playing = True
        self._startup_baseline_pending = True
        self.lighting_client.set_blackout(False)

    def _show_sound_stop(self) -> None:
        self._forget_room_look()
        self._showing_standby = False
        self.lighting_client.stop()

    async def _start_room_baseline(self) -> None:
        if not self._startup_baseline_pending or not self._room_playing:
            return
        now = self._clock.monotonic()
        self._startup_baseline_pending = False
        if self._current_intent is not None or any(
            item[0] <= now for item in self._pending_intents
        ):
            return
        # Do not supersede a committed section that belongs to later room audio.
        self._floor_armed = False
        delay_sec = self.command_queue.delay_sec if self.command_queue else 0.0
        song_sec = None if self.event_buffer is None else self.event_buffer.elapsed() - delay_sec
        await self._apply_intent(None, LightIntent.ATMOSPHERIC, song_sec, None)

    def _reset_source_analysis(self) -> None:
        self._floor_armed = True
        self._committed = None
        self._audio_sec = 0.0
        if self.section_chain is not None:
            self.section_chain.reset()
        if self.section_decoder is not None:
            self.section_decoder.reset()

    async def on_cycle(self):
        pass

    def _publish_shed_state(self) -> None:
        if self.event_buffer is None or self._watchdog is None:
            return
        now = self._clock.monotonic()
        if now - self._shed_published_at < _SHED_PUBLISH_SEC:
            return
        self._shed_published_at = now
        self.event_buffer.set_shed_state(
            level=self._watchdog.level.name,
            fault=self._watchdog.fault,
            sheds=self._watchdog.sheds,
            sheds_per_min=self._watchdog.sheds_per_min,
            drift_sec=round(self._watchdog.drift_sec, 4),
        )

    async def on_audio(self, audio_signal) -> None:
        self._publish_shed_state()
        if self._standby:
            # Decisions hold: the chain restarts from the returning signal.
            return
        self._schedule_confirmed_silence()
        await self._start_room_baseline()
        self._audio_sec += len(audio_signal) / SAMPLE_RATE
        if self.section_chain is None or self.section_decoder is None:
            return
        drained = self.section_chain.push_audio(audio_signal)
        if drained.gap:
            self.section_decoder.reset(cold_start=False)
            self._committed = None
            self._publish_decoder_state(None)
        for posterior in drained.posteriors:
            if isinstance(posterior, TrackerChunk):
                self.section_decoder.push_evidence(posterior)
                continue
            await self._commit(
                self.section_decoder.push_posterior(
                    posterior.time_sec, posterior.posterior, posterior.boundary
                )
            )

    async def on_beat(self, beat_number: int, bpm: float, bpm_changed: bool) -> None:
        current_second = self.analyser.get_song_current_duration().total_seconds()
        rms_energy = self.analyser.get_rms_energy()

        LOGGER.debug(f'[engine] [{current_second:.2f}s] beat #{beat_number}  bpm={bpm:.1f}')
        if self.event_buffer:
            self.event_buffer.add_beat(bpm, bpm_changed, rms=rms_energy)

        phase = None
        if self.section_decoder is not None:
            await self._commit(self.section_decoder.push_beat(self._audio_sec))
            phase = self.section_decoder.beat_phase
        grid_beat = self._advance_grid(phase)

        published_bpm = self._publishable_bpm(self._published_bpm, self.analyser.get_detector_bpm())
        normalized_rms_level = rms_to_normalized_level(rms_energy)

        async def publish():
            self.lighting_client.set_normalized_rms_level(normalized_rms_level)
            if published_bpm > 0:
                self.lighting_client.beat(grid_beat, published_bpm)
            await self._on_room_beat(phase)

        if self.command_queue:
            await self.command_queue.enqueue('beat', publish)
        else:
            await publish()

    def _advance_grid(self, phase: int | None) -> int:
        # Starfire's clock rejects a beat behind the last one it saw, so a
        # phase correction moves the grid forward to the next matching beat.
        if self._grid_beat is None:
            # With no bar phase the grid keeps the plain one-based beat count.
            self._grid_beat = 1 if phase is None else phase
        else:
            self._grid_beat += 1
            if phase is not None:
                self._grid_beat += (phase - self._grid_beat) % BEATS_PER_BAR
        return self._grid_beat

    async def _on_room_beat(self, phase: int | None) -> None:
        self._room_beat_phase = phase
        self._room_beat_at = self._clock.monotonic()
        if phase == 0:
            await self._switch_held_intent()

    async def _switch_held_intent(self) -> None:
        intent, self._held_intent = self._held_intent, None
        # Pause freezes a show, but standby is no show: the first look replaces it.
        if intent is None or (self._paused and not self._showing_standby):
            return
        await self.effect_controller.change_effect(intent)
        if self._showing_standby:
            self._showing_standby = False
            self._send_brightness()

    @staticmethod
    def _publishable_bpm(state: dict, bpm: float) -> float:
        # A zero-tempo warm-up is unknown; retain an established tempo.
        if bpm > 0:
            state['last'] = bpm
            return bpm
        return state.get('last', 0.0)

    async def _commit(self, decisions) -> None:
        self._publish_decoder_state(decisions[-1] if decisions else None)
        for decision in decisions:
            intent = intent_for_class(decision.label)

            LOGGER.debug(
                f'[engine] bar {decision.bar} @ {decision.start_sec:.2f}s  '
                f'{decision.label} → {intent.name}'
            )
            await self._commit_intent(
                intent,
                max(0.0, self._audio_sec - decision.start_sec),
                section_label=decision.label,
            )

    def _publish_decoder_state(self, decision) -> None:
        if self.event_buffer is None or self.section_decoder is None:
            return
        decoder = self.section_decoder
        if decision is not None:
            self._committed = decision
        observed = decoder.recent_observations[-1] if decoder.recent_observations else None
        committed = self._committed
        self.event_buffer.set_decoder_state(
            classes=list(decoder.classes),
            posterior=(
                None
                if observed is None or observed.posterior is None
                else [round(float(p), 6) for p in observed.posterior]
            ),
            boundary=(None if observed is None else float(observed.boundary)),
            observed_bar=(None if observed is None else observed.bar),
            committed_bar=(None if committed is None else committed.bar),
            committed_label=(None if committed is None else committed.label),
            lag_bars=(
                None if observed is None or committed is None else observed.bar - committed.bar
            ),
            chain_latency_sec=decoder.chain_latency_sec,
            bar_sec=round(decoder.bar_sec, 4),
            first_bar=decoder.first_bar,
            bar_edges=self._edges_on_the_buffers_clock(decoder.bar_edges),
        )

    def _edges_on_the_buffers_clock(self, edges: list) -> list:
        detected_now = self.event_buffer.elapsed()
        return [round(detected_now - (self._audio_sec - edge), 4) for edge in edges]

    def _log_chain_latency(self) -> None:
        decoder = self.section_decoder
        if decoder is None:
            LOGGER.debug(
                f'[engine] no section decoder — holding intent; '
                f'playback delay {self._playback_delay_sec:.2f}s'
            )
            return
        latency = decoder.chain_latency_sec
        if (
            self._last_logged_latency_seconds is not None
            and abs(latency - self._last_logged_latency_seconds) < _LATENCY_LOG_STEP_SEC
        ):
            return
        log = LOGGER.info if self._last_logged_latency_seconds is None else LOGGER.debug
        self._last_logged_latency_seconds = latency
        log(
            f'[engine] chain latency {latency:.2f}s = '
            f'{decoder.feature_latency_sec:.2f}s features + '
            f'{latency - decoder.feature_latency_sec:.2f}s decoder '
            f'({decoder.params.lag_bars} + 1 lag × {decoder.bar_sec:.3f}s bars) | '
            f'playback delay {self._playback_delay_sec:.2f}s → queue delay '
            f'{max(0.0, self._playback_delay_sec - latency):.2f}s — ensure '
            f'Radius TX playback_delay_seconds matches'
        )

    async def _commit_intent(
        self, intent: LightIntent, age_sec: float, *, section_label: str | None = None
    ) -> None:
        if not self._source_playing:
            return
        self._floor_armed = False
        now = self._clock.monotonic()
        fire_at = now - age_sec + self._playback_delay_sec
        self._note_lateness(now - fire_at)
        fire_at = max(fire_at, now, self._room_start_monotonic_seconds or now)

        if self.command_queue:
            self.command_queue.drop_pending('intent', fire_at)
        self._pending_intents = [item for item in self._pending_intents if item[0] <= fire_at]
        source_start = (
            self._room_start_monotonic_seconds
            if self._room_start_monotonic_seconds is not None
            else now
        )
        source_pending = [item for item in self._pending_intents if item[0] >= source_start]
        if source_pending:
            _, decided_intent, decided_section_label = source_pending[-1]
        elif source_start <= now:
            decided_intent, decided_section_label = (
                self._current_intent,
                self._section_class_at_playback,
            )
        else:
            decided_intent = decided_section_label = None
        if intent is decided_intent and (
            section_label is None or section_label == decided_section_label
        ):
            return

        song_sec = None if self.event_buffer is None else self.event_buffer.elapsed() - age_sec
        if not self.command_queue:
            await self._apply_intent(None, intent, song_sec, section_label)
            return
        entry = (fire_at, intent, section_label)
        self._pending_intents.append(entry)
        await self.command_queue.enqueue(
            'intent',
            lambda: self._apply_intent(entry, intent, song_sec, section_label),
            delay_sec=fire_at - now,
        )

    def _note_lateness(self, late_sec: float) -> None:
        if late_sec > 0.0:
            if not self._committing_late:
                self._committing_late = True
                LOGGER.warning(
                    f'[engine] the chain is older than the '
                    f'{self._playback_delay_sec:.2f}s playback delay — intents '
                    f'commit {late_sec:.2f}s late (slow tempo, accepted)'
                )
        elif self._committing_late:
            self._committing_late = False
            LOGGER.info('[engine] the chain is back inside the playback delay')

    async def _apply_intent(
        self, entry, intent: LightIntent, song_sec: float | None, section_label: str | None
    ) -> None:
        if entry is not None:
            if not any(pending is entry for pending in self._pending_intents):
                return
            self._pending_intents.remove(entry)
        if not self._room_playing:
            return
        if self.event_buffer:
            self.event_buffer.set_intent(intent.value, song_sec=song_sec)
        self._current_intent = intent
        self._section_class_at_playback = section_label
        self._intent_commits += 1
        if self._paused and not self._showing_standby:
            return
        # Loops switch on the room's downbeat; without a known bar phase there
        # is no downbeat to wait for. Standby has no phrase to finish.
        self._held_intent = intent
        if self._showing_standby or self._room_beat_phase in (None, 0):
            await self._switch_held_intent()

    async def on_note(self):
        LOGGER.debug('[engine] note detected')

    async def _floor_if_nothing_arrived(self) -> None:
        if not self._floor_armed:
            return
        chain = 0.0 if self.section_decoder is None else self.section_decoder.chain_latency_sec
        if self._audio_sec < chain + COLD_START_FLOOR_MARGIN_SEC:
            return
        self._floor_armed = False
        LOGGER.warning(
            f'[engine] no decision after {self._audio_sec:.1f}s of audio '
            f'(chain {chain:.1f}s) — lighting ATMOSPHERIC as the floor rather '
            f'than leaving the rig dark'
        )
        await self._commit_intent(LightIntent.ATMOSPHERIC, self._playback_delay_sec)

    def _source_is_quiet(self) -> bool:
        if not self.analyser.is_song_playing():
            return True
        if self.analyser.get_seconds_since_last_beat() > BEAT_LOSS_SECONDS:
            return True
        now = self._clock.monotonic()
        if rms_to_normalized_level(self.analyser.get_rms_energy()) >= QUIET_RMS:
            self._quiet_since = None
            return False
        if self._quiet_since is None:
            self._quiet_since = now
        return now - self._quiet_since >= QUIET_HOLD_SECONDS

    def _update_master_brightness(self) -> None:
        quiet = self._source_is_quiet()
        if quiet == self._source_quiet:
            return
        self._source_quiet = quiet
        LOGGER.info(
            f'[engine] source {"quiet" if quiet else "loud"} — the rig follows '
            f'when the room hears it'
        )

        # Quiet is judged on source audio, so the dim waits out the playback
        # delay like beats do; sent at once it would dim before the room fades.
        def apply():
            self._room_quiet = quiet
            self._send_brightness()

        if self.command_queue:

            async def command():
                apply()

            self.command_queue.schedule('brightness', command)
        else:
            apply()

    async def on_100ms_callback(self):
        if self._standby:
            return
        self._update_master_brightness()
        if (
            self._held_intent is not None
            and self._clock.monotonic() - self._room_beat_at > BEAT_LOSS_SECONDS
        ):
            # The room's beats stopped, so no downbeat will release the hold.
            await self._switch_held_intent()
        if not self.analyser.is_song_playing():
            return
        await self._floor_if_nothing_arrived()
        if (
            self.decided_intent is None
            and self.analyser.get_seconds_since_last_beat() > BEAT_LOSS_SECONDS
        ):
            await self._commit_intent(LightIntent.ATMOSPHERIC, 0.0)

    async def on_1sec_callback(self):
        if not self.analyser.is_song_playing():
            return
        if self.event_buffer and self.command_queue:
            self.event_buffer.set_timing_log(self.command_queue.get_timing_log())

    async def on_10sec_callback(self):
        if not self.analyser.is_song_playing():
            return
        bpm = int(self.analyser.get_bpm())
        current_second = int(self.analyser.get_song_current_duration().total_seconds())
        intent_name = self._current_intent.name if self._current_intent else 'None'
        LOGGER.info('[music] bpm=%d | intent=%s | position=%ds', bpm, intent_name, current_second)
        self._log_chain_latency()
