from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from autolux.lib.analyser.bar_tracker import TrackerChunk
from autolux.lib.analyser.music_analyser import MusicAnalyser
from autolux.lib.analyser.music_analyser_handler import IMusicAnalyserHandler
from autolux.lib.audio_config import SAMPLE_RATE
from autolux.lib.clients.starfire_client import StarfireClient, rms_to_normalized_level
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import LightIntent, intent_for_class
from autolux.lib.engine.event_buffer import SILENCE_TRIGGER, STOP_PERSISTENCE_SEC

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

_BEAT_ABSENCE_SEC = 2.5

_LATENCY_LOG_STEP_SEC = 0.25
_SHED_PUBLISH_SEC = 1.0
_BYPASSED_ON_STOP = ('sound', 'intent', 'beat')
COLD_START_FLOOR_MARGIN_SEC = 4.0


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
        silence_monitor=None,
        clock: Clock = SYSTEM_CLOCK,
    ):
        self.lighting_client = lighting_client
        self.effect_controller: EffectController = effect_controller
        self.command_queue: DelayedCommandQueue | None = command_queue
        self.event_buffer: EventBuffer | None = event_buffer
        self.analyser: MusicAnalyser = None
        self.section_chain = section_chain
        self.section_decoder = section_decoder
        self._watchdog = watchdog
        self._silence_monitor = silence_monitor
        self._playback_delay_sec: float = playback_delay_sec
        self._clock: Clock = clock
        self._current_intent: LightIntent | None = None
        self._section_class_at_playback: str | None = None
        self._pending_intents: list = []
        self._published_bpm: dict = {}
        self._session_beat_count = 0
        self._intent_commits: int = 0
        self._audio_sec: float = 0.0
        self._last_logged_latency_seconds: float | None = None
        self._committing_late: bool = False
        self._floor_armed: bool = True
        self._source_playing = False
        self._room_playing = False
        self._room_start_monotonic_seconds: float | None = None
        self._bypass_due_at: float | None = None
        self._shed_published_at: float = float('-inf')
        self._committed = None
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
        self._bypass_due_at = None
        if self.event_buffer:
            self.event_buffer.set_playing(True)
        self._at_the_room('sound', self._show_sound_start)

    def on_sound_stop(self):
        LOGGER.info('[engine] sound stop')
        self._source_playing = False
        if self.event_buffer:
            self.event_buffer.set_playing(False)
        self._bypass_due_at = self._clock.monotonic() + STOP_PERSISTENCE_SEC
        self._restart_for_the_next_song()

    def _bypass_if_the_silence_held(self) -> None:
        if self._bypass_due_at is None:
            return
        if self._clock.monotonic() < self._bypass_due_at:
            return
        self._bypass_due_at = None
        LOGGER.info(
            f'[engine] silence held {STOP_PERSISTENCE_SEC:.1f}s — '
            f'cutting the tail the room has not reached'
        )
        if self._silence_monitor is not None:
            self._silence_monitor()
        self._quiet_the_room_now()

    def _quiet_the_room_now(self) -> None:
        # Invalidate callbacks already removed from the queue by drain(), too.
        self._room_playing = False
        self._room_start_monotonic_seconds = None
        self._pending_intents.clear()
        if self.command_queue:
            for label in _BYPASSED_ON_STOP:
                self.command_queue.drop_pending(label, float('-inf'), inclusive=True)

            async def command():
                self._show_sound_stop()

            self.command_queue.schedule('sound', command, delay_sec=0.0)
        else:
            self._show_sound_stop()
        if self.event_buffer:
            self.event_buffer.set_intent(
                LightIntent.ATMOSPHERIC.value,
                song_sec=self.event_buffer.elapsed(),
                trigger=SILENCE_TRIGGER,
            )

    def _show_sound_start(self) -> None:
        if self._room_start_monotonic_seconds is None:
            return
        self._room_playing = True
        self.lighting_client.set_blackout(False)

    def _show_sound_stop(self) -> None:
        self._room_playing = False
        self._current_intent = None
        self._section_class_at_playback = None
        self.lighting_client.stop()
        if self.event_buffer is not None:
            self.event_buffer.stop_effects()

    def _at_the_room(self, label: str, action) -> None:
        if self.command_queue:

            async def command():
                action()

            self.command_queue.schedule(label, command)
        else:
            action()

        self._restart_for_the_next_song()

    def _restart_for_the_next_song(self) -> None:
        self._current_intent = None
        self._section_class_at_playback = None
        self._floor_armed = True
        if self.command_queue:
            self.command_queue.drop_pending('intent', float('-inf'))
        self._pending_intents = []
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
        self._bypass_if_the_silence_held()
        self._publish_shed_state()
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
        self._session_beat_count += 1
        session_beat_position = self._session_beat_count
        current_second = self.analyser.get_song_current_duration().total_seconds()
        rms_energy = self.analyser.get_rms_energy()

        LOGGER.debug(f'[engine] [{current_second:.2f}s] beat #{beat_number}  bpm={bpm:.1f}')
        if self.event_buffer:
            self.event_buffer.add_beat(bpm, bpm_changed, rms=rms_energy)

        if self.section_decoder is not None:
            await self._commit(self.section_decoder.push_beat(self._audio_sec))

        published_bpm = self._publishable_bpm(self._published_bpm, bpm)
        normalized_rms_level = rms_to_normalized_level(rms_energy)

        async def publish():
            self.lighting_client.set_normalized_rms_level(normalized_rms_level)
            if published_bpm > 0:
                self.lighting_client.beat(session_beat_position, published_bpm)

        if self.command_queue:
            await self.command_queue.enqueue('beat', publish)
        else:
            await publish()

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
        decided_section_label = (
            self._pending_intents[-1][2]
            if self._pending_intents
            else self._section_class_at_playback
        )
        if intent is self.decided_intent and (
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
        await self.effect_controller.change_effect(intent)

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

    async def on_100ms_callback(self):
        if not self.analyser.is_song_playing():
            return
        await self._floor_if_nothing_arrived()
        if (
            self.decided_intent is None
            and self.analyser.get_seconds_since_last_beat() > _BEAT_ABSENCE_SEC
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
