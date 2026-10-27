import asyncio

import pytest

from autolux.lib import media_session
from autolux.lib.engine.effect_definitions import LightIntent
from autolux.lib.main import AutoluxController
from autolux.lib.media_session import (
    QUIET_SECONDS,
    MediaSessionListener,
    Playback,
    Position,
    TrackChanged,
    TrackSource,
)
from autolux.tests.test_light_engine import _selections, _Show

PLAYBACK_DELAY_SEC = 14.0
GAP_SECONDS = 16 * 256 / 44100


class _FedShow(_Show):
    """A show whose media session is a fake feed delivered the way the listener delivers it."""

    def __init__(self):
        super().__init__()
        self.track_source = TrackSource(clock=self.clock)
        self.app = object.__new__(AutoluxController)
        self.app.track_source = self.track_source
        self.app.light_engine = self.light
        self.app.music_analyser = self.analyser
        self.app.event_buffer = self.events

    def feed(self, event) -> None:
        self.app._on_media_event(event)

    def track(self, title='Worth It.', artist='RAYE', album='My 21st Century Blues'):
        self.feed(TrackChanged(title, artist, album, 246.8, at=self.clock.monotonic()))

    def playback(self, status: str) -> None:
        self.feed(Playback(status, at=self.clock.monotonic()))

    async def keep_reporting(self, seconds: float, level: float) -> None:
        for _ in range(round(seconds)):
            self.feed(Position(self.clock.monotonic(), 246.8, at=self.clock.monotonic()))
            await self.play(1.0, level)


def new_track_for_an_intro_after_a_drop(light) -> bool:
    light._after_silence = False
    light._entered_intent = LightIntent.DROP
    return light._selection_context(
        LightIntent.ATMOSPHERIC, 'intro', light._clock.monotonic()
    ).new_track


async def test_a_track_change_starts_a_track_when_the_room_hears_it_and_names_it():
    show = _FedShow()
    show.playback('playing')
    await show.keep_reporting(20.0, 0.2)
    show.light._after_silence = False
    show.track('NUEVAYoL', 'Bad Bunny', 'DeBÍ TiRAR MáS FOToS')
    assert show.events.snapshot()['track'] == {
        'title': 'NUEVAYoL',
        'artist': 'Bad Bunny',
        'album': 'DeBÍ TiRAR MáS FOToS',
        'source': 'media_session',
    }, 'the dashboard names the track the moment the session reports it'
    await show.keep_reporting(PLAYBACK_DELAY_SEC - 1.0, 0.2)
    assert not show.light._after_silence, 'the lighting cue waits for the delayed audio'
    await show.keep_reporting(2.0, 0.2)
    assert show.light._after_silence


@pytest.mark.parametrize('status', ['paused', 'stopped'])
async def test_a_pause_stops_the_show_at_once_and_drops_the_delayed_audio(status):
    show = _FedShow()
    show.playback('playing')
    await show.keep_reporting(20.0, 0.2)
    assert show.queue.pending > 0
    before = len(show.lighting.events)
    show.playback(status)
    assert show.light.standby and show.analyser.in_standby
    commands = show.commands(before)
    assert commands.count({'kind': 'discard_delayed_audio'}) == 1
    assert show.monitor_silenced == 1
    assert show.queue.pending == 0
    assert ('standby', 'warm_white') in _selections(commands)


async def test_the_fade_after_a_pause_does_not_rearm_but_play_does():
    show = _FedShow()
    show.playback('playing')
    await show.keep_reporting(20.0, 0.2)
    show.playback('paused')
    await show.play(0.5, 0.05)
    assert show.light.standby, 'a paused session owns the stop; the fade-out tail is not a return'
    resets = show.chain.resets
    show.playback('playing')
    assert not show.analyser.in_standby and not show.light.standby
    parked = len(show.lighting.events)
    await show.keep_reporting(1.0, 0.2)
    assert show.chain.resets > resets, 'the show restarts from scratch as after silence'
    await show.keep_reporting(PLAYBACK_DELAY_SEC + 1.0, 0.2)
    assert _selections(show.commands(parked)) == [('atmospheric', 'cold_breathe')]


async def test_the_audio_cues_are_dormant_while_the_session_reports_and_return_after_it_goes_quiet():
    show = _FedShow()
    show.playback('playing')
    await show.keep_reporting(20.0, 0.2)
    show.light._after_silence = False
    await show.play(GAP_SECONDS, 0.0)
    await show.keep_reporting(PLAYBACK_DELAY_SEC + 2.0, 0.2)
    assert not show.light._after_silence, 'no gap cue while the session is live'
    assert not new_track_for_an_intro_after_a_drop(show.light)
    await show.play(3.0, 0.0)
    assert not show.light.standby, 'no zeros standby while the session is live'

    await show.play(QUIET_SECONDS + 1.0, 0.2)
    assert show.app.track_source.name == 'zeros'
    assert new_track_for_an_intro_after_a_drop(show.light)
    await show.play(3.0, 0.0)
    assert show.light.standby


async def test_a_session_stopped_for_thirty_seconds_counts_as_absent():
    show = _FedShow()
    show.playback('stopped')
    assert show.light.standby
    await show.play(QUIET_SECONDS - 2.0, 0.0)
    assert show.app.track_source.media_session_live
    await show.play(0.5, 0.2)
    assert show.light.standby, 'audio from elsewhere waits while the session is still live'
    await show.play(2.0, 0.0)
    await show.play(0.5, 0.2)
    assert not show.light.standby, 'the returning signal re-arms once the zeros own the show'
    show.app._publish_track()
    assert show.events.snapshot()['track']['source'] == 'zeros'


async def test_without_a_session_the_zeros_still_mark_tracks_and_standby():
    show = _FedShow()
    await show.play(20.0, 0.2)
    show.light._after_silence = False
    await show.play(GAP_SECONDS, 0.0)
    await show.play(PLAYBACK_DELAY_SEC + 1.0, 0.2)
    assert show.light._after_silence
    await show.play(2.2, 0.0)
    assert show.light.standby
    show.app._publish_track()
    assert show.events.snapshot()['track'] == {
        'title': None,
        'artist': None,
        'album': None,
        'source': 'zeros',
    }


async def test_an_unavailable_session_reports_so_and_starts_nothing(monkeypatch, caplog):
    monkeypatch.setattr(media_session, 'winrt_media_control', lambda: None)
    listener = MediaSessionListener(lambda event: pytest.fail(f'unexpected {event}'))
    with caplog.at_level('INFO'):
        assert not listener.start(asyncio.get_running_loop())
    assert 'unavailable' in caplog.text
    listener.stop()


def test_the_listener_reports_only_changes_and_ignores_untitled_tracks():
    listener = MediaSessionListener(lambda event: None)

    def state(track, status, position):
        return {'track': track, 'status': status, 'position_s': position, 'duration_s': 200.0}

    playing = state(('A', 'B', 'C'), 'playing', 10.2)
    first = listener.changes(playing, at=1.0)
    assert [type(event) for event in first] == [TrackChanged, Playback, Position]
    assert listener.changes(state(('A', 'B', 'C'), 'playing', 10.7), at=1.5) == []
    assert listener.changes(state(('', '', ''), None, 10.9), at=1.7) == []
    assert listener.changes(state(('A', 'B', 'C'), 'paused', 10.9), at=2.0) == [
        Playback('paused', at=2.0)
    ]
    assert listener.changes(None, at=3.0) == [Playback('stopped', at=3.0)]


def test_the_track_status_follows_health_and_scrolls_long_titles_in_full():
    from autolux.simulate.visualizer_app import _build_show_status

    title = 'An Extraordinarily Long Song Title That Keeps Going'
    track = {'title': title, 'artist': 'RAYE', 'album': 'X', 'source': 'media_session'}
    status = _build_show_status({'track': track}, now=0.0)
    captions = [item.children[0].children for item in status]
    assert captions[captions.index('Health') + 1] == 'Track'
    item = status[captions.index('Track')]
    marquee = item.children[1].children
    assert marquee.className == 'marquee'
    assert [copy.children for copy in marquee.children] == [f'RAYE – {title}'] * 2

    short = _build_show_status({'track': dict(track, title='Escapism')}, now=0.0)
    item = short[[i.children[0].children for i in short].index('Track')]
    assert item.children[1].children == 'RAYE – Escapism'

    for absent in (None, dict(track, source='zeros'), dict(track, title=None)):
        status = _build_show_status({'track': absent}, now=0.0)
        assert 'Track' not in [item.children[0].children for item in status]
