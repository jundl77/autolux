import datetime
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from autolux.lib.analyser.drift_watchdog import DriftWatchdog
from autolux.lib.analyser.madmom_rhythm import FRAME_SIZE, SAMPLE_RATE, MadmomRhythm
from autolux.lib.analyser.music_analyser_handler import IMusicAnalyserHandler
from autolux.lib.clock import SYSTEM_CLOCK, Clock
from autolux.lib.engine.effect_definitions import ShowSettings

LOGGER = logging.getLogger(__name__)

_BPM_BEAT_WINDOW = 9
_BPM_FOLD_MIN = 85.0
_BPM_FOLD_MAX = 170.0
_NOTE_REFRACTORY = datetime.timedelta(milliseconds=75)
_RHYTHM_LOG_INTERVAL = datetime.timedelta(seconds=10)

_ENERGY_WINDOW_BUFFERS = 26

_SILENCE_RMS = 1.5e-4

# MME delivers a stopped player as a few 16-bit LSB (3e-5 each) of noise, not exact zeros.
_DIGITAL_SILENCE_PEAK = 1e-4
TRACK_GAP_MIN_SECONDS = 0.05
TRACK_GAP_IGNORED_AFTER_START_SECONDS = 2.0

_SONG_CLOCK_HORIZON = datetime.timedelta(minutes=15)

BASS_CEILING_HZ = 250.0
TREBLE_FLOOR_HZ = 4000.0
_SPECTRUM_WINDOW = np.hanning(FRAME_SIZE)
_SPECTRUM_HZ = np.fft.rfftfreq(FRAME_SIZE, 1.0 / SAMPLE_RATE)
_BASS_BINS = _SPECTRUM_HZ < BASS_CEILING_HZ
_TREBLE_BINS = _SPECTRUM_HZ >= TREBLE_FLOOR_HZ
_SILENT_POWER = 1e-12
BEAT_BANDS_HZ = {'bass': (20.0, 150.0), 'mid': (150.0, 2500.0), 'high': (2500.0, 16000.0)}
_BEAT_BAND_BINS = {
    name: (_SPECTRUM_HZ >= low) & (_SPECTRUM_HZ < high)
    for name, (low, high) in BEAT_BANDS_HZ.items()
}
_KICK_BINS = _SPECTRUM_HZ <= 150.0
ONSET_WINDOW_SECONDS = 0.1
REFERENCE_HALF_LIFE_BEATS = 256
_REFERENCE_DECAY = 0.5 ** (1.0 / REFERENCE_HALF_LIFE_BEATS)
BAND_RANGE_DB = 36.0


@dataclass(frozen=True)
class SpectralBalance:
    centroid_hz: float
    bass_over_treble_db: float


@dataclass(frozen=True)
class BeatFeatures:
    onset: float
    bass: float
    mid: float
    high: float


def kick_flux(previous: np.ndarray, magnitude: np.ndarray) -> float:
    return float(np.maximum(magnitude[_KICK_BINS] - previous[_KICK_BINS], 0.0).sum())


def band_powers(magnitude: np.ndarray) -> dict[str, float]:
    power = magnitude**2
    return {name: float(power[bins].sum()) for name, bins in _BEAT_BAND_BINS.items()}


def decayed_reference(reference: float, value: float) -> float:
    return max(value, reference * _REFERENCE_DECAY)


def relative_level(power: float, reference: float) -> float:
    if reference <= _SILENT_POWER:
        return 0.0
    below_db = 10.0 * math.log10((power + _SILENT_POWER) / reference)
    return min(1.0, max(0.0, 1.0 + below_db / BAND_RANGE_DB))


def onset_strength(peak: float, reference: float) -> float:
    return min(1.0, peak / reference) if reference > 0.0 else 0.0


def spectral_balance(frame: np.ndarray) -> SpectralBalance:
    power = np.abs(np.fft.rfft(frame * _SPECTRUM_WINDOW)) ** 2
    total = float(power.sum())
    if total <= _SILENT_POWER:
        return SpectralBalance(0.0, 0.0)
    bass = float(power[_BASS_BINS].sum()) + _SILENT_POWER
    treble = float(power[_TREBLE_BINS].sum()) + _SILENT_POWER
    return SpectralBalance(
        centroid_hz=float((_SPECTRUM_HZ * power).sum() / total),
        bass_over_treble_db=10.0 * math.log10(bass / treble),
    )


class MusicAnalyser:
    def __init__(
        self,
        sample_rate: int,
        buffer_size: int,
        handler: IMusicAnalyserHandler,
        clock: Clock = SYSTEM_CLOCK,
        note_clicks: bool = False,
        watchdog: DriftWatchdog | None = None,
        settings: Callable[[], ShowSettings] = ShowSettings,
        zeros_mark_tracks: Callable[[], bool] = lambda: True,
    ):
        self._clock: Clock = clock
        self._settings = settings
        self._zeros_mark_tracks = zeros_mark_tracks
        self.sample_rate: int = sample_rate
        self.buffer_size: int = buffer_size
        self.handler: IMusicAnalyserHandler = handler
        self.note_clicks: bool = note_clicks

        self.click_sound: np.ndarray = 0.15 * np.sin(
            2.0 * np.pi * np.arange(self.buffer_size) / self.buffer_size * self.sample_rate / 3000.0
        )

        self._rhythm: MadmomRhythm = MadmomRhythm(self.sample_rate)
        self._drift: DriftWatchdog = watchdog or DriftWatchdog(
            self.buffer_size / self.sample_rate, clock=self._clock
        )
        self._rhythm_log_at: datetime.datetime = self._clock.now() + _RHYTHM_LOG_INTERVAL
        self._beats_since_log: int = 0
        self._last_beat_activation: float = 0.0
        self._audio_started = False
        self._digital_silence_samples: int = 0
        self._sample_clock: int = 0
        self._track_start_sample: int | None = None
        self.in_standby: bool = False
        self._reset_state()

    def _roll_song_clock(self) -> None:
        # madmom stays locked: re-locking mid-audio would rotate the bar grid's four-count.
        playing = self.is_playing
        self._reset_song_clock()
        self.is_playing = playing

    def _reset_state(self) -> None:
        self._rhythm.reset()
        self._beat_stream_times: deque = deque(maxlen=_BPM_BEAT_WINDOW)
        self.last_bpm: float = 0.0
        self.beat_count: int = 0
        self.last_beat_detected: datetime.datetime = self._clock.now()
        self.last_note_detected: datetime.datetime = self._clock.now()
        self._reset_song_clock()

    def _reset_song_clock(self) -> None:
        self.is_playing: bool = False
        self.song_start_time: datetime.datetime = self._clock.now()
        self.song_current_time: datetime.datetime = self._clock.now()
        self.silence_period_start: datetime.datetime = self._clock.now()
        self._rms_window: deque = deque(maxlen=_ENERGY_WINDOW_BUFFERS)
        self._spectrum_frame: np.ndarray = np.zeros(FRAME_SIZE)
        self._magnitude: np.ndarray = np.zeros(FRAME_SIZE // 2 + 1)
        self._flux_window: deque = deque(
            maxlen=max(1, math.ceil(ONSET_WINDOW_SECONDS * self.sample_rate / self.buffer_size))
        )
        self._band_sums: dict[str, float] = dict.fromkeys(BEAT_BANDS_HZ, 0.0)
        self._band_buffers = 0
        self._onset_reference = 0.0
        self._band_references: dict[str, float] = dict.fromkeys(BEAT_BANDS_HZ, 0.0)
        self._beat_features: BeatFeatures | None = None

    def get_song_current_duration(self) -> datetime.timedelta:
        if self.is_playing:
            return self.song_current_time - self.song_start_time
        else:
            return datetime.timedelta(seconds=0)

    def get_bpm(self) -> float:
        return self._fold_bpm(self.get_detector_bpm())

    def get_detector_bpm(self) -> float:
        if not self.is_playing or len(self._beat_stream_times) < 3:
            return 0.0
        interval = float(np.median(np.diff(np.array(self._beat_stream_times))))
        return 60.0 / interval if interval > 0 else 0.0

    @staticmethod
    def _fold_bpm(bpm: float) -> float:
        if not math.isfinite(bpm) or bpm <= 0:
            return 0.0
        while bpm >= _BPM_FOLD_MAX:
            bpm /= 2.0
        while bpm < _BPM_FOLD_MIN:
            bpm *= 2.0
        return bpm

    def is_song_playing(self) -> bool:
        return self.is_playing

    def get_seconds_since_last_beat(self) -> float:
        return (self._clock.now() - self.last_beat_detected).total_seconds()

    def get_rms_energy(self) -> float:
        if not self._rms_window:
            return 0.0
        return sum(self._rms_window) / len(self._rms_window)

    def get_spectral_balance(self) -> SpectralBalance:
        return spectral_balance(self._spectrum_frame)

    def get_beat_features(self) -> BeatFeatures | None:
        return self._beat_features

    def _track_spectrum(self) -> None:
        magnitude = np.abs(np.fft.rfft(self._spectrum_frame * _SPECTRUM_WINDOW))
        self._flux_window.append(kick_flux(self._magnitude, magnitude))
        self._magnitude = magnitude
        for name, power in band_powers(magnitude).items():
            self._band_sums[name] += power
        self._band_buffers += 1

    def _measure_beat(self) -> None:
        peak = max(self._flux_window, default=0.0)
        self._onset_reference = decayed_reference(self._onset_reference, peak)
        levels = {}
        for name, total in self._band_sums.items():
            average = total / max(1, self._band_buffers)
            self._band_references[name] = decayed_reference(self._band_references[name], average)
            levels[name] = relative_level(average, self._band_references[name])
        self._band_sums = dict.fromkeys(BEAT_BANDS_HZ, 0.0)
        self._band_buffers = 0
        self._beat_features = BeatFeatures(
            onset=onset_strength(peak, self._onset_reference), **levels
        )

    async def analyse(self, audio_signal: np.ndarray) -> np.ndarray:
        silent = self._track_digital_silence(audio_signal)
        self._sample_clock += len(audio_signal)
        if silent:
            self._drift.observe()
            return audio_signal
        now = self._clock.now()
        if not self._audio_started:
            self._track_start_sample = self._sample_clock - len(audio_signal)
            started = now - datetime.timedelta(seconds=self.buffer_size / self.sample_rate)
            self.song_start_time = self.silence_period_start = started
            self.last_beat_detected = self.last_note_detected = started
            self._rhythm_log_at = now + _RHYTHM_LOG_INTERVAL
            self._audio_started = True

        self._drift.observe()

        rms = float(np.sqrt(np.mean(audio_signal**2)))
        self._rms_window.append(rms)
        self._spectrum_frame = np.concatenate((self._spectrum_frame, audio_signal))[-FRAME_SIZE:]
        self._track_spectrum()
        self._track_song_duration(rms, now)

        rhythm = self._rhythm.process(audio_signal)
        if rhythm.beats:
            self._last_beat_activation = rhythm.beat_activation
        is_beat = await self._track_beat(rhythm.beats, now)
        await self._track_note(rhythm.beats, now)
        self._log_rhythm_state(now)

        if self.get_song_current_duration() > _SONG_CLOCK_HORIZON:
            self._roll_song_clock()

        if is_beat and self.note_clicks:
            audio_signal += self.click_sound

        return audio_signal

    def _track_digital_silence(self, audio_signal: np.ndarray) -> bool:
        if np.max(np.abs(audio_signal), initial=0.0) >= _DIGITAL_SILENCE_PEAK:
            gap, self._digital_silence_samples = self._digital_silence_samples, 0
            if not self._zeros_mark_tracks():
                return self.in_standby
            if self.in_standby:
                self.leave_standby()
            elif gap:
                self._note_gap(gap)
            return False
        self._digital_silence_samples += len(audio_signal)
        threshold = self._settings().digital_silence_seconds * self.sample_rate
        if self._digital_silence_samples >= threshold and self._zeros_mark_tracks():
            seconds = self._digital_silence_samples / self.sample_rate
            self.enter_standby(f'{seconds:.1f}s of digital silence')
        return self.in_standby

    def _note_gap(self, gap_samples: int) -> None:
        started = self._sample_clock - gap_samples
        if (
            gap_samples < TRACK_GAP_MIN_SECONDS * self.sample_rate
            or self._track_start_sample is None
            or started - self._track_start_sample
            < TRACK_GAP_IGNORED_AFTER_START_SECONDS * self.sample_rate
        ):
            return
        self._track_start_sample = self._sample_clock
        seconds = gap_samples / self.sample_rate
        LOGGER.info(f'[rhythm] {seconds * 1000:.0f} ms of digital silence — new-track cue')
        self.handler.on_digital_gap(started / self.sample_rate, seconds)

    def enter_standby(self, reason: str) -> None:
        if self.in_standby:
            return
        LOGGER.info(f'[rhythm] {reason} — standby')
        self.in_standby = True
        self._on_sound_stop()
        self.handler.on_digital_silence()

    def leave_standby(self) -> None:
        if not self.in_standby:
            return
        LOGGER.info('[rhythm] signal returned — analysis restarts from scratch')
        self.in_standby = False
        self._reset_state()
        self._audio_started = False
        self.handler.on_signal_return()

    def _log_rhythm_state(self, now: datetime.datetime) -> None:
        if now < self._rhythm_log_at:
            return
        window = (now - (self._rhythm_log_at - _RHYTHM_LOG_INTERVAL)).total_seconds()
        self._rhythm_log_at = now + _RHYTHM_LOG_INTERVAL
        drift = self.get_drift_status()
        LOGGER.debug(
            f'[rhythm] madmom {self._beats_since_log} beats '
            f'({self._beats_since_log / window:.2f}/s) '
            f'over {window:.1f}s | bpm={self.get_bpm():.1f} '
            f'| last beat activation={self._last_beat_activation:.3f} '
            f'| drift={drift["drift_sec"]:+.3f}s shed={drift["shed_level"]}'
            f'{"" if drift["fault"] is None else "/" + drift["fault"]} '
            f'| adapter lag={drift["adapter_latency_sec"] * 1000:.1f}ms'
        )
        self._beats_since_log = 0

    def get_drift_status(self) -> dict:
        return {
            'shed_level': self._drift.level.name,
            'fault': self._drift.fault,
            'drift_sec': round(self._drift.drift_sec, 4),
            'peak_drift_sec': round(self._drift.peak_drift_sec, 4),
            'total_drift_sec': round(self._drift.total_drift_sec, 4),
            'adapter_latency_sec': round(self._rhythm.pending_latency_sec, 5),
        }

    async def _track_beat(self, beats: list, now: datetime.datetime) -> bool:
        for beat_time in beats:
            interval = beat_time - self._beat_stream_times[-1] if self._beat_stream_times else 0.0
            self._beat_stream_times.append(beat_time)
            self._beats_since_log += 1
            this_bpm: float = self.get_bpm()
            LOGGER.debug(
                f'[rhythm] beat #{self.beat_count + 1} @ {beat_time:.3f}s '
                f'(madmom stream), interval={interval:.3f}s, bpm={this_bpm:.1f}, '
                f'activation={self._last_beat_activation:.3f}'
            )
            bpm_changed: bool = self._has_bpm_changed(this_bpm)
            self.beat_count += 1
            self._measure_beat()
            await self.handler.on_beat(self.beat_count, this_bpm, bpm_changed)
            self.last_bpm = this_bpm
            self.last_beat_detected = now
        return bool(beats)

    async def _track_note(self, beats: list, now: datetime.datetime) -> bool:
        is_note = bool(beats) and now - self.last_note_detected > _NOTE_REFRACTORY
        if is_note:
            await self.handler.on_note()
            self.last_note_detected = now
        return is_note

    @staticmethod
    def _is_silence(rms: float) -> bool:
        return bool(rms < _SILENCE_RMS)

    def _track_song_duration(self, rms: float, now: datetime.datetime) -> None:
        if not self._is_silence(rms):
            self.silence_period_start = now

        if now - self.silence_period_start > datetime.timedelta(seconds=0.3):
            self._on_sound_stop()
        else:
            self.song_current_time = now

        if not self.is_playing and now - self.song_start_time > datetime.timedelta(seconds=0.3):
            self._on_sound_start()

    def _on_sound_start(self):
        self.is_playing = True
        self.handler.on_sound_start()

    def _on_sound_stop(self):
        was_playing = self.is_playing
        self._reset_state()
        if was_playing:
            self.handler.on_sound_stop()

    def _has_bpm_changed(self, current_bpm: float) -> bool:
        if not self.is_playing or current_bpm <= 0:
            return False
        return (abs(current_bpm - self.last_bpm) / current_bpm) > 0.05
