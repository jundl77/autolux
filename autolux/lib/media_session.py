"""The Windows system media session as the authority on track changes and pauses."""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from autolux.lib.clock import SYSTEM_CLOCK, Clock

LOGGER = logging.getLogger(__name__)

POLL_SECONDS = 1.0
QUIET_SECONDS = 30.0
MEDIA_SESSION_SOURCE = 'media_session'
ZEROS_SOURCE = 'zeros'
PREFERRED_APP = 'spotify'
# GlobalSystemMediaTransportControlsSessionPlaybackStatus; CHANGING (2) is transient and ignored.
PLAYBACK_STATUS = {0: 'stopped', 1: 'stopped', 3: 'stopped', 4: 'playing', 5: 'paused'}


@dataclass(frozen=True)
class TrackChanged:
    title: str
    artist: str
    album: str
    duration_s: float | None
    at: float


@dataclass(frozen=True)
class Playback:
    status: str
    at: float


@dataclass(frozen=True)
class Position:
    position_s: float
    duration_s: float | None
    at: float


MediaEvent = TrackChanged | Playback | Position


def winrt_media_control():
    try:
        from winrt.windows.media import control
    except ImportError:
        return None
    return control


class TrackSource:
    """The media session owns track boundaries and pauses while it reported within QUIET_SECONDS."""

    def __init__(self, clock: Clock = SYSTEM_CLOCK):
        self._clock = clock
        self._reported_at = float('-inf')
        self._was_live = False

    def note(self, event: MediaEvent) -> None:
        self._reported_at = event.at

    @property
    def media_session_live(self) -> bool:
        live = self._clock.monotonic() - self._reported_at <= QUIET_SECONDS
        if live != self._was_live:
            self._was_live = live
            LOGGER.info(
                '[media] track changes and pauses now come from the %s',
                'system media session' if live else 'audio (digital silence and gaps)',
            )
        return live

    @property
    def name(self) -> str:
        return MEDIA_SESSION_SOURCE if self.media_session_live else ZEROS_SOURCE


class MediaSessionListener:
    """Watches the system media session on its own thread and delivers deduplicated events
    on the show's event loop."""

    def __init__(self, deliver: Callable[[MediaEvent], None], clock: Clock = SYSTEM_CLOCK):
        self._deliver = deliver
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self._track: tuple[str, str, str] | None = None
        self._status: str | None = None
        self._position: tuple[int, float | None] | None = None

    def start(self, show_loop: asyncio.AbstractEventLoop) -> bool:
        control = winrt_media_control()
        if control is None:
            LOGGER.info('[media] system media session unavailable; track cues come from the audio')
            return False
        self._thread = threading.Thread(
            target=self._run, args=(control, show_loop), name='media-session', daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stopping = True
        if self._loop is not None and self._wake is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self, control, show_loop: asyncio.AbstractEventLoop) -> None:
        try:
            asyncio.run(self._watch(control, show_loop))
        except Exception:
            LOGGER.exception(
                '[media] media session listener stopped; track cues fall back to audio'
            )

    async def _watch(self, control, show_loop: asyncio.AbstractEventLoop) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        manager = await control.GlobalSystemMediaTransportControlsSessionManager.request_async()

        def wake(*_):
            self._loop.call_soon_threadsafe(self._wake.set)

        manager_tokens = (
            manager.add_sessions_changed(wake),
            manager.add_current_session_changed(wake),
        )
        subscribed = None
        try:
            while not self._stopping:
                session = self._pick(manager)
                app = None if session is None else session.source_app_user_model_id
                if subscribed is None or subscribed[0] != app:
                    self._unsubscribe(subscribed)
                    subscribed = self._subscribe(session, app, wake)
                    LOGGER.info('[media] watching media session %s', app or '(none)')
                self._report(await self._read(session), show_loop)
                try:
                    await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
                except TimeoutError:
                    pass
                self._wake.clear()
        finally:
            self._unsubscribe(subscribed)
            manager.remove_sessions_changed(manager_tokens[0])
            manager.remove_current_session_changed(manager_tokens[1])

    @staticmethod
    def _pick(manager):
        for session in manager.get_sessions():
            if PREFERRED_APP in session.source_app_user_model_id.lower():
                return session
        return manager.get_current_session()

    @staticmethod
    def _subscribe(session, app, wake):
        if session is None:
            return (app, None, ())
        return (
            app,
            session,
            (
                session.add_media_properties_changed(wake),
                session.add_playback_info_changed(wake),
                session.add_timeline_properties_changed(wake),
            ),
        )

    @staticmethod
    def _unsubscribe(subscribed) -> None:
        if subscribed is None or subscribed[1] is None:
            return
        _, session, (media, playback, timeline) = subscribed
        session.remove_media_properties_changed(media)
        session.remove_playback_info_changed(playback)
        session.remove_timeline_properties_changed(timeline)

    @staticmethod
    async def _read(session) -> dict | None:
        if session is None:
            return None
        properties = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        status = PLAYBACK_STATUS.get(int(session.get_playback_info().playback_status))
        duration = (timeline.end_time - timeline.start_time).total_seconds() or None
        position = timeline.position.total_seconds()
        if status == 'playing':
            # Position is as of last_updated_time, which Spotify leaves unchanged while it plays.
            updated = timeline.last_updated_time
            position += (datetime.datetime.now(datetime.UTC) - updated).total_seconds()
        if duration is not None:
            position = min(max(position, 0.0), duration)
        return {
            'track': (properties.title, properties.artist, properties.album_title),
            'status': status,
            'position_s': position,
            'duration_s': duration,
        }

    def _report(self, state: dict | None, show_loop: asyncio.AbstractEventLoop) -> None:
        for event in self.changes(state, self._clock.monotonic()):
            show_loop.call_soon_threadsafe(self._deliver, event)

    def changes(self, state: dict | None, at: float) -> list[MediaEvent]:
        if state is None:
            state = {'track': None, 'status': 'stopped', 'position_s': None, 'duration_s': None}
        events: list[MediaEvent] = []
        track = state['track']
        if track is not None and track[0] and track != self._track:
            self._track = track
            events.append(TrackChanged(*track, duration_s=state['duration_s'], at=at))
        if state['status'] is not None and state['status'] != self._status:
            self._status = state['status']
            events.append(Playback(state['status'], at=at))
        if state['position_s'] is not None:
            position = (int(state['position_s']), state['duration_s'])
            if position != self._position:
                self._position = position
                events.append(Position(state['position_s'], state['duration_s'], at=at))
        return events
