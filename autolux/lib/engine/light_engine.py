from __future__ import annotations

import dataclasses
import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from autolux.lib.analyser.bar_tracker import TrackerChunk
from autolux.lib.analyser.music_analyser import MusicAnalyser
from autolux.lib.analyser.music_analyser_handler import IMusicAnalyserHandler
from autolux.lib.audio_config import SAMPLE_RATE
from autolux.lib.clients.starfire_client import StarfireClient, rms_to_normalized_level
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.autoloop_policy import (
    LOUDNESS_TREND_BEATS,
    SelectionContext,
    arrival_of_peak,
    loudness_trend,
)
from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
from autolux.lib.engine.effect_controller import OPERATOR_FADE_SECONDS, EffectController
from autolux.lib.engine.effect_definitions import (
    BEAT_LOSS_SECONDS,
    QUIET_HOLD_SECONDS,
    QUIET_RMS,
    LightIntent,
    ShowSettings,
    intensity_level,
    intent_for_class,
    live_crossfade_beats,
)
from autolux.lib.engine.event_buffer import (
    SILENCE_TRIGGER,
    STANDBY_INTENT,
    STOP_PERSISTENCE_SEC,
)
from autolux.lib.engine.intensity_follow import (
    CALM_OPENING_BEATS,
    LEVEL_INTENSITIES,
    FollowMusic,
)
from autolux.lib.engine.section_decoder import BEATS_PER_BAR
from autolux.lib.engine.static_mode import StaticLooks, StaticRig
from autolux.lib.engine.track_mood import (
    MAX_MEASURED_BARS,
    MeasuredBeat,
    TrackMoodMeter,
    level_drive,
)
from autolux.lib.media_session import TrackChanged
from autolux.lighting.palettes import Mood

if TYPE_CHECKING:
    from autolux.lib.engine.event_buffer import EventBuffer

LOGGER = logging.getLogger(__name__)

_LATENCY_LOG_STEP_SEC = 0.25
_SHED_PUBLISH_SEC = 1.0
COLD_START_FLOOR_MARGIN_SEC = 4.0
BRIGHTNESS_FADE_SECONDS = 1.0
FALLBACK_BAR_SECONDS = BEATS_PER_BAR * 60.0 / 128.0
BUILD_INTENTS = (LightIntent.BREAKDOWN, LightIntent.BUILDUP)
OUTRO_LABELS = ('outro', 'altoutro')
INTRO_LABELS = ('intro', 'altintro')
BUILD_INTERRUPTION_BARS = 8
MEASUREMENT_ENDING_INTENTS = (LightIntent.BUILDUP, LightIntent.DROP)
# Radius bridges a capture resync with inserted zeros and reports it through telemetry that
# Autolux polls; a gap this close to a reported resync is the bridge, not a track change.
RESYNC_GUARD_SECONDS = 3.0
TRACK_LEVEL_BEATS = 2048


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
        static_rig: StaticRig = StaticRig(),  # noqa: B008
        media_session_live: Callable[[], bool] = lambda: False,
    ):
        self.lighting_client = lighting_client
        self._media_session_live = media_session_live
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
        # The last positive detector tempo; a zero-tempo warm-up keeps it.
        self._last_bpm: float | None = None
        self._beat_index_never_reset: int = 0
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
        self._room_quiet = False
        self._paused = False
        self._standby = False
        self._showing_standby = False
        self._master_level = 1.0
        self._sent_brightness = 1.0
        self._sent_crossfade_beats: float | None = None
        self._room_levels: deque[float] = deque(maxlen=LOUDNESS_TREND_BEATS)
        self._entered_intent: LightIntent | None = None
        self._entered_label: str | None = None
        self._build_started_at: float | None = None
        self._build_interrupted_at: float | None = None
        self._last_drop_at: float | None = None
        self._after_silence = True
        self._beat_levels: deque[tuple[float, float]] = deque(maxlen=TRACK_LEVEL_BEATS)
        self._track_started_audio_sec = 0.0
        self._current_arrival_of_peak: float | None = None
        self._capture_discontinuities: int | None = None
        self._capture_discontinuity_seen_at: deque[float] = deque(maxlen=8)
        self._track_mood = TrackMoodMeter(MAX_MEASURED_BARS * BEATS_PER_BAR)
        self._measured_track_mood: Mood | None = None
        self._mood_closes_at_next_section = False
        self._track_beats: list[MeasuredBeat] | None = None
        self.follow_music = FollowMusic()
        # Set by the show: applies an automatic level through the operator's path.
        self.on_auto_intensity: Callable[[float, str], None] | None = None
        self._static_mode = False
        self.static_looks = StaticLooks(lighting_client, static_rig, clock, settings)
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
    def mode(self) -> str:
        return 'static' if self._static_mode else 'auto'

    @property
    def master_level(self) -> float:
        return self._master_level

    @property
    def standby(self) -> bool:
        return self._standby

    def pause(self) -> None:
        if self._paused:
            return
        LOGGER.info('[engine] pause')
        self._paused = True
        self._held_intent = None
        self.lighting_client.set_paused(True)
        self._send_brightness(BRIGHTNESS_FADE_SECONDS)

    async def resume(self) -> None:
        if not self._paused:
            return
        LOGGER.info('[engine] play')
        self._paused = False
        self.lighting_client.set_paused(False)
        self._send_brightness(BRIGHTNESS_FADE_SECONDS)
        await self._rejoin_current_intent()

    async def _rejoin_current_intent(self) -> None:
        if self._holding() or self._current_intent is None or not self._room_playing:
            return
        self._held_intent = self._current_intent
        if self._room_beat_phase is None:
            await self._switch_held_intent()

    def _holding(self) -> bool:
        return self._paused or self._static_mode

    def _holding_the_look(self) -> bool:
        return self._static_mode or (self._paused and not self._showing_standby)

    def enter_static(self) -> None:
        if self._static_mode:
            return
        LOGGER.info('[engine] static mode')
        self._static_mode = True
        self._held_intent = None
        self.effect_controller.release_special()
        self._send_brightness(OPERATOR_FADE_SECONDS)

    async def leave_static(self) -> None:
        if not self._static_mode:
            return
        LOGGER.info('[engine] auto mode')
        self.static_looks.clear()
        self._static_mode = False
        self._send_brightness(OPERATOR_FADE_SECONDS)
        await self._rejoin_current_intent()

    async def sync_static(self) -> None:
        # The overlay, not a mode switch, decides: any look, floor or blackout holds the show.
        if self.static_looks.active:
            self.enter_static()
        else:
            await self.leave_static()

    def next_autoloop(self) -> None:
        if self._paused:
            raise ValueError('the show is paused')
        if self._static_mode:
            raise ValueError('a static overlay is active')
        intent = self._entered_intent
        if intent is None or self._showing_standby:
            raise ValueError('no section is playing yet')
        context = self._selection_context(intent, self._entered_label, self._clock.monotonic())
        self.effect_controller.skip_autoloop(context)

    def set_master_level(self, level: float) -> None:
        if type(level) not in (int, float) or not 0.0 <= level <= 1.0:
            raise ValueError('master brightness must be within 0..1')
        self._master_level = float(level)
        self._send_brightness(OPERATOR_FADE_SECONDS)

    def settings_changed(self) -> None:
        self.send_renderer_settings()
        self._send_brightness(OPERATOR_FADE_SECONDS)

    def send_renderer_settings(self) -> None:
        settings = self._settings()
        self.lighting_client.set_paused(self._paused)
        self._send_crossfade()
        self.lighting_client.set_strobe_limits(
            settings.strobe_max_rate_hz, settings.strobe_max_burst_seconds
        )
        self.send_intensity()

    def _send_crossfade(self) -> None:
        self._sent_crossfade_beats = live_crossfade_beats(self._settings())
        self.lighting_client.set_default_crossfade_beats(self._sent_crossfade_beats)

    def send_intensity(self) -> None:
        level = self._settings().intensity
        LOGGER.info('[engine] intensity %.2f (%s) sent to Starfire', level, intensity_level(level))
        self.lighting_client.set_intensity(level)

    def intensity_changed(self) -> None:
        self.send_intensity()
        # The level scales the crossfade; only a level change moves it.
        if live_crossfade_beats(self._settings()) != self._sent_crossfade_beats:
            self._send_crossfade()
            LOGGER.info('[engine] crossfade %g beats', self._sent_crossfade_beats)
        if self._holding() or self._showing_standby or self._entered_intent is None:
            return
        context = self._selection_context(
            self._entered_intent, self._entered_label, self._clock.monotonic()
        )
        self.effect_controller.apply_intensity(
            dataclasses.replace(context, new_track=False), self._last_bpm
        )

    def _send_brightness(self, fade_seconds: float = BRIGHTNESS_FADE_SECONDS) -> None:
        quiet = self._room_quiet and not self._static_mode
        dimmed = (quiet or self._paused) and not self._showing_standby
        brightness = self._master_level * (self._settings().quiet_brightness if dimmed else 1.0)
        if brightness == self._sent_brightness:
            return
        self._sent_brightness = brightness
        LOGGER.debug(f'[engine] set_master {brightness:.3f} fade {fade_seconds:g}s')
        self.lighting_client.set_master(brightness, fade_seconds=fade_seconds)

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
        self.effect_controller.enter_standby(self._last_bpm, renderer_paused=self._paused)
        if self._static_mode:
            self.static_looks.resend()

    def on_digital_gap(self, started_sec: float, seconds: float) -> None:
        detected_at = self._clock.monotonic()

        async def cue():
            if self._resync_near(detected_at):
                LOGGER.info('[engine] silence gap at a capture resync — not a new track')
                return
            LOGGER.info(
                f'[engine] {seconds * 1000:.0f} ms silence gap — the next look starts a track'
            )
            self._after_silence = True

        if self.command_queue:
            self.command_queue.schedule('track_cue', cue)
        elif not self._resync_near(detected_at):
            self._after_silence = True

    def on_media_track(self, track: TrackChanged) -> None:
        def cue():
            LOGGER.info(
                f'[engine] media session: "{track.artist} – {track.title}" — '
                'the next look starts a track'
            )
            self._after_silence = True

        if self.command_queue:

            async def command():
                cue()

            delay_sec = max(0.0, track.at + self.command_queue.delay_sec - self._clock.monotonic())
            self.command_queue.schedule('track_cue', command, delay_sec=delay_sec)
        else:
            cue()

    def note_capture_discontinuities(self, total: int | None) -> None:
        if total is None:
            return
        if self._capture_discontinuities is not None and total > self._capture_discontinuities:
            self._capture_discontinuity_seen_at.append(self._clock.monotonic())
        self._capture_discontinuities = total

    def _resync_near(self, at: float) -> bool:
        return any(
            abs(seen - at) <= RESYNC_GUARD_SECONDS for seen in self._capture_discontinuity_seen_at
        )

    def on_signal_return(self):
        LOGGER.info('[engine] signal returned — the show restarts from scratch')
        self._standby = False
        self._last_bpm = None
        self._room_beat_at = float('-inf')
        self._committing_late = False

    def _forget_room_look(self) -> None:
        self._room_playing = False
        self._startup_baseline_pending = False
        self._current_intent = None
        self._section_class_at_playback = None
        self._held_intent = None
        self._room_beat_phase = None
        self._room_levels.clear()
        if self._entered_intent == LightIntent.DROP:
            self._last_drop_at = self._clock.monotonic()
        self._entered_intent = None
        self._build_started_at = None
        self._build_interrupted_at = None
        self._after_silence = True
        self._discard_track_mood()
        if self.event_buffer is not None:
            self.event_buffer.stop_effects()

    def _show_sound_start(self) -> None:
        self._room_playing = True
        self._startup_baseline_pending = True
        self.lighting_client.set_blackout(self.static_looks.blackout)

    def _show_sound_stop(self) -> None:
        self._forget_room_look()
        self._showing_standby = False
        self.lighting_client.stop()
        if self._static_mode:
            self.static_looks.resend()

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
        self._beat_levels.clear()
        self._track_started_audio_sec = 0.0
        if self.section_chain is not None:
            self.section_chain.reset()
        if self.section_decoder is not None:
            self.section_decoder.reset()

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
        spectrum = self.analyser.get_spectral_balance()

        LOGGER.debug(f'[engine] [{current_second:.2f}s] beat #{beat_number}  bpm={bpm:.1f}')
        if self.event_buffer:
            self.event_buffer.add_beat(bpm, bpm_changed, rms=rms_energy)

        phase = None
        if self.section_decoder is not None:
            await self._commit(self.section_decoder.push_beat(self._audio_sec))
            phase = self.section_decoder.beat_phase
        self._beat_index_never_reset += 1
        beat_index = self._beat_index_never_reset

        detector_bpm = self.analyser.get_detector_bpm()
        if detector_bpm > 0:
            self._last_bpm = detector_bpm
        published_bpm = self._last_bpm or 0.0
        normalized_rms_level = rms_to_normalized_level(rms_energy)
        self._beat_levels.append((self._audio_sec, normalized_rms_level))
        measured = MeasuredBeat(
            bpm, normalized_rms_level, spectrum.centroid_hz, spectrum.bass_over_treble_db
        )

        async def publish():
            self.lighting_client.set_normalized_rms_level(normalized_rms_level)
            self._room_levels.append(normalized_rms_level)
            if published_bpm > 0:
                self.lighting_client.beat(beat_index, phase, BEATS_PER_BAR, published_bpm)
            await self._on_room_beat(phase)
            if bpm > 0:
                self._measure_track_mood(measured, phase)

        if self.command_queue:
            await self.command_queue.enqueue('beat', publish)
        else:
            await publish()

    async def _on_room_beat(self, phase: int | None) -> None:
        self._room_beat_phase = phase
        self._room_beat_at = self._clock.monotonic()
        if phase == 0:
            await self._switch_held_intent()

    def _measure_track_mood(self, beat: MeasuredBeat, phase: int | None) -> None:
        if self._track_beats is not None:
            self._track_beats.append(beat)
            if len(self._track_beats) == CALM_OPENING_BEATS:
                self._follow_opening()
        self._measured_track_mood = self._track_mood.add(beat) or self._measured_track_mood
        if self._measured_track_mood is not None and phase in (0, None):
            mood, self._measured_track_mood = self._measured_track_mood, None
            self.effect_controller.choose_track_palette(mood, send_now=not self._holding_the_look())
            self._follow_track_mood()

    def _finish_track_mood(self) -> None:
        mood = self._track_mood.finish() or self._measured_track_mood
        self._measured_track_mood = None
        if mood is not None:
            self.effect_controller.choose_track_palette(mood, send_now=False)
            self._follow_track_mood()

    def _follow_track_mood(self) -> None:
        drive = level_drive(self._track_beats)
        level = self.follow_music.on_track_mood(drive, self._intensity_level())
        self._follow(level, f'drive {drive:+.2f}')

    def _follow_opening(self) -> None:
        level = self.follow_music.on_opening_heard()
        if level is not None:
            self._follow(
                level,
                f'no drop in the opening, mean energy {self.follow_music.mean_energy:.2f}',
            )

    def _follow_drop(self, arrival: float | None) -> None:
        if not self._settings().intensity_auto:
            return
        drive = None
        if self._track_beats and not self._track_mood.measuring:
            drive = level_drive(self._track_beats)
        level = self.follow_music.on_drop(arrival, self._intensity_level(), drive)
        if drive is not None and not self.follow_music.capped:
            self._follow(level, f'drive {drive:+.2f}')
        else:
            self._follow(
                level, 'drop arrival ' + ('unknown' if arrival is None else f'{arrival:.2f}')
            )

    def _intensity_level(self) -> str:
        return intensity_level(self._settings().intensity)

    def _follow(self, level: str | None, why: str) -> None:
        if (
            level is None
            or level == self._intensity_level()
            or not self._settings().intensity_auto
            or self.on_auto_intensity is None
        ):
            return
        self.on_auto_intensity(LEVEL_INTENSITIES[level], why)

    def _discard_track_mood(self) -> None:
        self._track_mood.finish()
        self._measured_track_mood = None
        self._mood_closes_at_next_section = False
        self._track_beats = None

    def _section_closes_track_mood(self, intent: LightIntent) -> bool:
        # A full window still waiting for its downbeat rides this section's select instead.
        if self._measured_track_mood is not None:
            return True
        if not self._track_mood.measuring:
            return False
        if self._mood_closes_at_next_section:
            return self._track_mood.ready
        if intent not in MEASUREMENT_ENDING_INTENTS:
            return False
        if self._track_mood.ready:
            return True
        self._mood_closes_at_next_section = True
        return False

    def _start_track_mood(self, intent: LightIntent) -> None:
        self._discard_track_mood()
        self._track_mood.start()
        self._track_beats = []
        self.follow_music.start_track(intent)

    async def _switch_held_intent(self) -> None:
        intent, self._held_intent = self._held_intent, None
        if intent is None or self._holding_the_look():
            return
        label = self._section_class_at_playback
        now = self._clock.monotonic()
        context = self._selection_context(intent, label, now)
        if context.new_track:
            self._start_track_mood(intent)
            self._track_started_audio_sec = self._audio_sec - self._playback_delay_sec
        elif self._section_closes_track_mood(intent):
            self._finish_track_mood()
        cut = self._paused and self._showing_standby
        bpm = self._last_bpm
        if await self.effect_controller.change_effect(intent, bpm, context=context, cut=cut):
            self._note_new_look(intent, now)
        self._entered_label = label
        if self._showing_standby:
            self._showing_standby = False
            self._send_brightness()

    def _selection_context(
        self, intent: LightIntent, label: str | None, now: float
    ) -> SelectionContext:
        previous = self._entered_intent
        new_track = self._after_silence or (
            not self._media_session_live()
            and intent == LightIntent.ATMOSPHERIC
            and label in INTRO_LABELS
            and (previous == LightIntent.DROP or self._entered_label in OUTRO_LABELS)
        )
        building = intent in (LightIntent.BUILDUP, LightIntent.DROP) and (
            previous in BUILD_INTENTS or self._resumes_build(intent, now)
        )
        bar_seconds = self._bar_seconds()
        ending_drop = previous == LightIntent.DROP and intent != LightIntent.DROP
        left_drop_at = now if ending_drop else self._last_drop_at
        return SelectionContext(
            intent,
            seconds_since_drop=None if left_drop_at is None else now - left_drop_at,
            build_bars=(now - self._build_started_at) / bar_seconds
            if building and self._build_started_at is not None
            else 0.0,
            loudness=loudness_trend(self._room_levels),
            new_track=new_track,
            arrival_of_peak=self._current_arrival_of_peak if intent == LightIntent.DROP else None,
        )

    def _bar_seconds(self) -> float:
        bpm = self._last_bpm
        return BEATS_PER_BAR * 60.0 / bpm if bpm else FALLBACK_BAR_SECONDS

    def _resumes_build(self, intent: LightIntent, now: float) -> bool:
        return (
            intent in (*BUILD_INTENTS, LightIntent.DROP)
            and self._entered_intent == LightIntent.ATMOSPHERIC
            and self._build_interrupted_at is not None
            and now - self._build_interrupted_at <= BUILD_INTERRUPTION_BARS * self._bar_seconds()
        )

    def _note_new_look(self, intent: LightIntent, now: float) -> None:
        previous = self._entered_intent
        if previous == LightIntent.DROP and intent != LightIntent.DROP:
            self._last_drop_at = now
        resumed = self._resumes_build(intent, now)
        if intent == LightIntent.ATMOSPHERIC and previous in BUILD_INTENTS:
            self._build_interrupted_at = now
        else:
            self._build_interrupted_at = None
            if intent not in BUILD_INTENTS:
                self._build_started_at = None
            elif previous not in BUILD_INTENTS and not resumed:
                self._build_started_at = now
        self._entered_intent = intent
        self._after_silence = False

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
        arrival = (
            arrival_of_peak(
                self._beat_levels, self._audio_sec - age_sec, self._track_started_audio_sec
            )
            if intent == LightIntent.DROP
            else None
        )
        if not self.command_queue:
            await self._apply_intent(None, intent, song_sec, section_label, arrival)
            return
        entry = (fire_at, intent, section_label)
        self._pending_intents.append(entry)
        await self.command_queue.enqueue(
            'intent',
            lambda: self._apply_intent(entry, intent, song_sec, section_label, arrival),
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
        self,
        entry,
        intent: LightIntent,
        song_sec: float | None,
        section_label: str | None,
        arrival: float | None = None,
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
        self._current_arrival_of_peak = arrival
        self.follow_music.on_intent(intent)
        if intent == LightIntent.DROP:
            self._follow_drop(arrival)
        self._section_class_at_playback = section_label
        self._intent_commits += 1
        if self._holding_the_look():
            return
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
