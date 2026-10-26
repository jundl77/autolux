"""Committed sections select loops at room playback time."""

import logging
from collections import deque

import numpy as np
import pytest

from autolux.lib.audio_config import SAMPLE_RATE
from autolux.lib.clock import VirtualClock
from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
from autolux.lib.engine.effect_controller import EffectController
from autolux.lib.engine.effect_definitions import (
    BEAT_LOSS_SECONDS,
    PREFERRED_AUTOLOOPS,
    QUIET_BRIGHTNESS,
    LightIntent,
)
from autolux.lib.engine.event_buffer import SILENCE_TRIGGER, STOP_PERSISTENCE_SEC
from autolux.lib.engine.light_engine import LightEngine
from autolux.lib.engine.section_decoder import BarDecision, BarObservation
from autolux.simulate.lighting_recorder import RecordingStarfireClient


class FakeAnalyser:
    def __init__(self):
        self.playing = True
        self.since_beat = 0.0
        self.bpm = 128.0
        self.rms = 0.1  # about -20 dBFS: comfortably loud

    def is_song_playing(self):
        return self.playing

    def get_seconds_since_last_beat(self):
        return self.since_beat

    def get_bpm(self):
        return self.bpm

    def get_detector_bpm(self):
        return self.bpm

    def get_rms_energy(self):
        return self.rms

    def get_song_current_duration(self):
        import datetime

        return datetime.timedelta(seconds=1.0)


class FakeChain:
    def __init__(self, posteriors=(), gap=False):
        from autolux.lib.analyser.section_model import Drained

        self._drained = Drained
        self.pending = list(posteriors)
        self.gap = gap
        self.samples = 0
        self.resets = 0

    def push_audio(self, samples):
        self.samples += len(samples)
        out, self.pending = self.pending, []
        gap, self.gap = self.gap, False
        return self._drained(gap, out)

    def reset(self):
        self.resets += 1
        self.pending = []


class FakeDecoder:
    chain_latency_sec = 13.66
    feature_latency_sec = 7.9938
    bar_sec = 1.8898
    classes = (
        'intro',
        'altintro',
        'buildup',
        'breakdown',
        'bridge',
        'drop',
        'cooldown',
        'outro',
        'altoutro',
    )

    @property
    def bar_edges(self):
        return list(self._edges)

    @property
    def first_bar(self):
        return self._first_bar

    class params:
        lag_bars = 2

    # None is a grid with no bar phase; tests set 0..3 before a beat to place it.
    beat_phase = None

    def __init__(self, script=None):
        self.beats: list = []
        self.cells: list = []
        self.resets = 0
        self.cold_starts: list = []
        self.recent_observations = deque(maxlen=4)
        self._edges: list = []
        self._first_bar = 0
        self._script = list(script or [])

    def push_beat(self, at_sec):
        self.beats.append(at_sec)
        return self._next()

    def push_posterior(self, at_sec, posterior, boundary):
        self.cells.append((at_sec, boundary))
        return self._next()

    def _next(self):
        return self._script.pop(0) if self._script else []

    def reset(self, *, cold_start=True):
        self.resets += 1
        self.cold_starts.append(cold_start)


def engine(
    *,
    decoder=None,
    chain=None,
    playback_delay_sec=14.0,
    clock=None,
    queue=True,
    events=False,
    playing=True,
):
    clock = clock or VirtualClock()
    lighting = RecordingStarfireClient(clock=clock)
    command_queue = DelayedCommandQueue(playback_delay_sec, clock=clock) if queue else None
    buffer = None
    if events:
        from autolux.lib.engine.event_buffer import EventBuffer

        buffer = EventBuffer(
            window_sec=float('inf'), clock=clock, look_ahead_sec=playback_delay_sec
        )
        buffer.start()
    light = LightEngine(
        lighting,
        EffectController(lighting, event_buffer=buffer),
        command_queue,
        event_buffer=buffer,
        playback_delay_sec=playback_delay_sec,
        section_chain=chain,
        section_decoder=decoder,
        clock=clock,
    )
    light.set_analyser(FakeAnalyser())
    # Most tests begin during an established show; startup tests use playing=False.
    light.analyser.playing = playing
    light._source_playing = playing
    light._room_playing = playing
    light._room_start_monotonic_seconds = clock.monotonic() if playing else None
    return light, command_queue, clock, lighting


async def test_beat_and_note_details_are_verbose_but_music_status_is_concise(caplog):
    light, _, _, _ = engine()
    with caplog.at_level(logging.INFO):
        for beat in range(20):
            await light.on_beat(beat, 128, False)
            await light.on_note()
        await light.on_10sec_callback()
    assert len(caplog.records) == 1
    assert '[music] bpm=128' in caplog.text
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await light.on_beat(21, 128, False)
        await light.on_note()
    assert 'beat #21' in caplog.text and 'note detected' in caplog.text


def decisions(*labels, start=0):
    return [BarDecision(start + i, label, (start + i) * 1.9) for i, label in enumerate(labels)]


async def elapse(light, clock, sec: float) -> None:
    await light.on_audio(np.zeros(int(sec * SAMPLE_RATE), dtype=np.float32))
    clock.advance(sec)


async def hold_silence(light, clock, sec: float = STOP_PERSISTENCE_SEC + 0.1) -> None:
    remaining = sec
    while remaining > 1e-9:
        step = min(0.1, remaining)
        clock.advance(step)
        await light.on_audio(np.zeros(int(step * SAMPLE_RATE), dtype=np.float32))
        remaining -= step


async def _noop() -> None:
    return None


async def commit(light, decoder, clock, label, *, age_sec=13.7, bar=0):
    decoder._script.append([BarDecision(bar, label, light.audio_sec - age_sec)])
    await light.on_beat(1, 128.0, False)


async def bars(light, decoder, clock, *labels, age_sec=13.7, bar_sec=1.9):
    for index, label in enumerate(labels):
        await elapse(light, clock, bar_sec)
        await commit(light, decoder, clock, label, age_sec=age_sec, bar=index)


def queued_intents(queue):
    return [item for item in queue._queue if item[4] == 'intent']


async def settle(light, clock, queue, sec=20.0, step=0.25):
    for _ in range(int(sec / step)):
        await elapse(light, clock, step)
        await queue.drain()


@pytest.mark.parametrize(
    'label,intent',
    [
        ('intro', LightIntent.ATMOSPHERIC),
        ('altintro', LightIntent.ATMOSPHERIC),
        ('altoutro', LightIntent.ATMOSPHERIC),
        ('bridge', LightIntent.BREAKDOWN),
        ('cooldown', LightIntent.BREAKDOWN),
        ('outro', LightIntent.ATMOSPHERIC),
        ('buildup', LightIntent.BUILDUP),
        ('breakdown', LightIntent.BREAKDOWN),
        ('drop', LightIntent.DROP),
    ],
)
async def test_a_decision_commits_the_intent_its_class_maps_to(label, intent):
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await bars(light, decoder, clock, label)
    await settle(light, clock, queue)
    assert light.current_intent is intent


@pytest.mark.parametrize('section_label', ['intro', 'drop'])
async def test_repeated_class_holds_the_loop_without_retriggering_its_strobe(section_label):
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await bars(light, decoder, clock, section_label, section_label, section_label)
    await settle(light, clock, queue)
    assert len(selections(lighting)) == 1
    strobes = [event for event in lighting.events if event['command']['kind'] == 'set_override']
    assert len(strobes) == (1 if section_label == 'drop' else 0)
    assert light.intent_commits == 1


async def test_a_sustained_drop_stays_drop_with_no_engine_derived_promotion():
    """A long drop holds its selected effect until the section changes."""
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await bars(light, decoder, clock, *(['drop'] * 12))
    await settle(light, clock, queue)
    assert light.current_intent is LightIntent.DROP
    assert light.intent_commits == 1


async def test_on_beat_feeds_the_bar_grid_in_the_audio_time_base():
    decoder = FakeDecoder()
    chain = FakeChain()
    light, _, _, _ = engine(decoder=decoder, chain=chain)
    buffer = np.zeros(SAMPLE_RATE, dtype=np.float32)
    await light.on_audio(buffer)
    await light.on_beat(1, 128.0, False)
    await light.on_audio(buffer)
    await light.on_beat(2, 128.0, False)
    assert decoder.beats == pytest.approx([1.0, 2.0])


async def test_on_audio_drives_the_chain_into_the_decoder():
    chain = FakeChain([_posterior(0.9288, 0.3), _posterior(1.0217, 0.4)])
    decoder = FakeDecoder()
    light, _, _, _ = engine(decoder=decoder, chain=chain)
    await light.on_audio(np.zeros(256, dtype=np.float32))
    assert decoder.cells == [(0.9288, 0.3), (1.0217, 0.4)]


async def test_a_gap_from_the_feature_stage_clears_the_decoder_before_it_is_fed():
    chain = FakeChain([_posterior(0.9288, 0.3)], gap=True)
    decoder = FakeDecoder()
    light, _, _, _ = engine(decoder=decoder, chain=chain)
    await light.on_audio(np.zeros(256, dtype=np.float32))
    assert decoder.resets == 1
    assert decoder.cells == [(0.9288, 0.3)]
    assert decoder.cold_starts == [False], (
        'the feature stage gapped, not the beat source: madmom kept running and '
        'pays no warm-up, so the grid must not re-apply the first-beat anchor'
    )


async def test_a_shed_and_unshed_cycle_leaves_the_grid_on_the_phase_it_had():
    from autolux.tests.test_section_decoder import decoder as section

    live, chain = section(lag_bars=2), FakeChain()
    light, _, clock, _ = engine(decoder=live, chain=chain)

    pushed = []
    for index in range(12):
        if index == 6:
            chain.gap = True
            await light.on_audio(np.zeros(0, dtype=np.float32))
        await elapse(light, clock, 0.5)
        await light.on_beat(index + 1, 128.0, False)
        pushed.append(light.audio_sec)

    uninterrupted = section(lag_bars=2)
    for beat in pushed:
        uninterrupted.push_beat(beat)

    assert live.bar_edges == [
        pytest.approx(line) for line in uninterrupted.bar_edges if line > pushed[5]
    ]


async def test_a_beat_inside_the_sound_start_margin_leaves_the_anchor_alone():
    """9 of 215 val tracks put their first beat under the 0.3 s margin."""
    from autolux.tests.test_section_decoder import decoder as section

    live = section(lag_bars=2)
    light, _, clock, _ = engine(decoder=live, chain=FakeChain())

    await elapse(light, clock, 0.2)
    await light.on_beat(1, 128.0, False)
    light.on_sound_start()

    for index in range(4):
        await elapse(light, clock, 0.5)
        await light.on_beat(index + 2, 128.0, False)

    assert live.bar_edges == [pytest.approx(2.0)]


async def test_a_missing_chain_is_the_degradation_state_rather_than_a_crash():
    """#144: missing artifacts are a degraded show, not a reason to die."""
    light, _, _, _ = engine(decoder=None, chain=None)
    await light.on_audio(np.zeros(256, dtype=np.float32))
    await light.on_beat(1, 128.0, False)
    assert light.current_intent is None


@pytest.mark.parametrize('queued', [False, True])
async def test_startup_silence_keeps_inference_running_without_selecting_an_autoloop(queued):
    decoder = FakeDecoder([decisions('drop')])
    chain = FakeChain([_posterior(0.0, 0.9)])
    light, queue, clock, lighting = engine(
        decoder=decoder, chain=chain, queue=queued, playing=False
    )

    await elapse(light, clock, 30.0)
    await light.on_100ms_callback()
    if queue:
        await queue.drain()

    assert chain.samples == 30 * SAMPLE_RATE
    assert decoder.cells == [(0.0, 0.9)]
    assert selections(lighting) == []
    assert not any(event['command']['kind'] == 'set_override' for event in lighting.events)
    assert light.current_intent is None
    assert light.intent_commits == 0


async def test_an_aged_decision_waits_for_the_first_audible_music_before_drop_and_accent():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder, playing=False)
    await elapse(light, clock, 30.0)
    light.analyser.playing = True
    light.on_sound_start()
    await commit(light, decoder, clock, 'drop', age_sec=30.0)

    clock.advance(13.9)
    await queue.drain()
    assert selections(lighting) == []
    assert not any(event['command']['kind'] == 'set_override' for event in lighting.events)

    clock.advance(0.1)
    await queue.drain()
    assert len(selections(lighting)) == 1
    assert len([e for e in lighting.events if e['command']['kind'] == 'set_override']) == 1
    assert light.current_intent is LightIntent.DROP


async def test_the_startup_fallback_also_waits_for_delayed_music():
    light, queue, clock, lighting = engine(playing=False)
    light.analyser.playing = True
    light.on_sound_start()
    await tick_100ms(light, clock, 5.0)
    await queue.drain()
    assert selections(lighting) == []

    clock.advance(9.1)
    await queue.drain()
    assert light.current_intent is LightIntent.ATMOSPHERIC


async def test_room_audio_starts_with_atmospheric_without_waiting_for_section_warmup():
    light, queue, clock, lighting = engine(decoder=FakeDecoder(), playing=False, events=True)
    light.analyser.playing = True
    light.on_sound_start()

    clock.advance(13.9)
    await queue.drain()
    assert selections(lighting) == []
    clock.advance(0.1)
    await queue.drain()

    assert [event['command']['bank'] for event in selections(lighting)] == ['atmospheric']
    assert light.current_intent is LightIntent.ATMOSPHERIC
    assert light.intent_commits == 1
    assert light.event_buffer.to_report()['intents'][0]['song_t'] == pytest.approx(0.0)
    assert not any(event['command']['kind'] == 'set_override' for event in lighting.events)
    await tick_100ms(light, clock, 20.0)
    await queue.drain()
    assert len(selections(lighting)) == 1
    assert light.intent_commits == 1


async def test_startup_baseline_preserves_a_committed_section_due_after_room_start():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder, playing=False)
    light.analyser.playing = True
    light.on_sound_start()
    clock.advance(4.0)
    await commit(light, decoder, clock, 'drop', age_sec=2.0)

    clock.advance(10.0)
    await queue.drain()
    assert light.current_intent is LightIntent.ATMOSPHERIC
    assert len(queued_intents(queue)) == 1
    assert not any(event['command']['kind'] == 'set_override' for event in lighting.events)

    clock.advance(2.0)
    await queue.drain()
    assert [event['command']['bank'] for event in selections(lighting)] == ['atmospheric', 'drop']
    assert len([e for e in lighting.events if e['command']['kind'] == 'set_override']) == 1


async def test_first_decoded_intro_preserves_room_start_coverage_in_the_report():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder, playing=False, events=True)
    clock.advance(0.3)
    light.analyser.playing = True
    light.on_sound_start()
    clock.advance(11.7)
    await commit(light, decoder, clock, 'intro', age_sec=11.5)

    clock.advance(2.3)
    await queue.drain()
    room_start = light.event_buffer.to_report()
    assert room_start['intents'][0]['t'] == pytest.approx(14.3)
    assert room_start['intents'][0]['song_t'] == pytest.approx(0.3)

    clock.advance(0.2)
    await queue.drain()
    after_decoded_intro = light.event_buffer.to_report()
    assert len(after_decoded_intro['intents']) == len(after_decoded_intro['effects']) == 1
    assert after_decoded_intro['intents'][0]['t'] == pytest.approx(14.3)
    assert after_decoded_intro['intents'][0]['song_t'] == pytest.approx(0.3)
    assert after_decoded_intro['effects'][0]['t'] == pytest.approx(14.3)
    assert after_decoded_intro['beats'] == room_start['beats']
    assert len(selections(lighting)) == 1


async def test_queue_free_playback_starts_its_baseline_on_the_first_audio_cycle():
    light, _, _, lighting = engine(queue=False, playing=False)
    light.analyser.playing = True
    light.on_sound_start()
    await light.on_audio(np.zeros(256, dtype=np.float32))
    assert [event['command']['bank'] for event in selections(lighting)] == ['atmospheric']


async def test_a_short_song_keeps_its_delayed_start_and_stop():
    light, queue, clock, lighting = engine(playing=False)
    light.analyser.playing = True
    light.on_sound_start()
    clock.advance(1.0)
    light.analyser.playing = False
    light.on_sound_stop()
    await hold_silence(light, clock)
    clock.advance(14.0 - clock.monotonic())
    await queue.drain()
    assert [event['command']['bank'] for event in selections(lighting)] == ['atmospheric']
    assert light._room_playing
    clock.advance(1.0)
    await queue.drain()
    assert not light._room_playing
    assert lighting.events[-1] == {'at_seconds': 15.0, 'command': {'kind': 'stop'}}


async def test_source_stop_preserves_queued_intents_and_ignores_new_silence_predictions():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder)
    await elapse(light, clock, 30.0)
    await commit(light, decoder, clock, 'breakdown', age_sec=13.0)
    light.analyser.playing = False
    light.on_sound_stop()
    await commit(light, decoder, clock, 'drop', age_sec=30.0)

    clock.advance(1.0)
    await queue.drain()
    assert light.current_intent is LightIntent.BREAKDOWN
    assert [event['command']['bank'] for event in selections(lighting)] == ['breakdown']
    assert not any(event['command']['kind'] == 'set_override' for event in lighting.events)


async def test_a_confirmed_silence_does_not_cancel_an_already_due_tail_intent():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder)
    await elapse(light, clock, 30.0)
    await commit(light, decoder, clock, 'drop', age_sec=13.0)
    deliver = queued_intents(queue)[0][-1]
    light.on_sound_stop()
    await hold_silence(light, clock)

    await deliver()
    await queue.drain()
    await commit(light, decoder, clock, 'drop', age_sec=30.0)
    await queue.drain()
    assert [event['command']['bank'] for event in selections(lighting)] == ['drop']
    assert light._room_playing
    clock.advance(44.0 - clock.monotonic())
    await queue.drain()
    assert not light._room_playing


async def test_a_song_boundary_resets_both_stages_and_the_grid():
    chain, decoder = FakeChain(), FakeDecoder()
    light, _, _, _ = engine(decoder=decoder, chain=chain)
    await light.on_audio(np.zeros(SAMPLE_RATE, dtype=np.float32))
    light.on_sound_stop()
    assert (chain.resets, decoder.resets) == (1, 1)
    await light.on_beat(1, 128.0, False)
    assert decoder.beats == [0.0]


async def test_a_beat_waits_the_whole_playback_delay():
    light, queue, clock, _ = engine(decoder=FakeDecoder())
    await light.on_beat(1, 128.0, False)
    clock.advance(13.9)
    await queue.drain()
    assert queue.pending == 1
    clock.advance(0.2)
    await queue.drain()
    assert queue.pending == 0


async def test_an_intent_waits_only_what_the_chain_has_not_already_spent():
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', age_sec=13.7)
    intent = next(e for e in queue._queue if e[4] == 'intent')
    assert intent[3] == pytest.approx(14.0 - 13.7)
    assert intent[0] == pytest.approx(20.0 + 0.3)


async def test_a_chain_slower_than_the_budget_fires_at_once_and_says_so(caplog):
    """#154 accepted lateness on slow tempos rather than growing the budget."""
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder)
    await elapse(light, clock, 30.0)
    with caplog.at_level(logging.WARNING):
        await commit(light, decoder, clock, 'drop', age_sec=16.4)
        await commit(light, decoder, clock, 'breakdown', age_sec=16.4)
    intent = next(e for e in queue._queue if e[4] == 'intent')
    assert intent[3] == 0.0
    assert 'late' in caplog.text.lower()
    assert len([r for r in caplog.records if 'late' in r.message.lower()]) == 1, (
        'one line per transition, not one per bar'
    )


def test_the_startup_line_names_both_halves_of_the_measured_chain(caplog):
    with caplog.at_level(logging.INFO):
        engine(decoder=FakeDecoder())
    assert 'chain latency' in caplog.text.lower()
    assert '7.99' in caplog.text and '14.0' in caplog.text


async def test_beat_absence_still_commits_atmospheric():
    light, queue, clock, _ = engine(decoder=FakeDecoder())
    light.analyser.since_beat = 3.0
    await light.on_100ms_callback()
    clock.advance(14.0)
    await queue.drain()
    assert light.current_intent is LightIntent.ATMOSPHERIC


async def test_a_decoder_decision_after_silence_takes_the_stage_back():
    decoder = FakeDecoder([decisions('drop')])
    light, queue, clock, _ = engine(decoder=decoder)
    light.analyser.since_beat = 3.0
    await light.on_100ms_callback()
    clock.advance(14.0)
    await queue.drain()
    assert light.current_intent is LightIntent.ATMOSPHERIC

    light.analyser.since_beat = 0.0
    await light.on_beat(1, 128.0, False)
    clock.advance(14.0)
    await queue.drain()
    assert light.current_intent is LightIntent.DROP


async def test_a_stale_silence_atmospheric_cannot_take_a_stage_the_decoder_owns():
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', age_sec=13.7)
    await elapse(light, clock, 0.4)
    await queue.drain()
    assert light.current_intent is LightIntent.DROP

    light.analyser.since_beat = 3.0
    await light.on_100ms_callback()
    light.analyser.since_beat = 0.0

    await elapse(light, clock, 3.0)
    await commit(light, decoder, clock, 'drop', age_sec=13.7)

    for _ in range(40):
        await elapse(light, clock, 0.5)
        await queue.drain()
    assert light.current_intent is LightIntent.DROP, (
        'the timer clobbered the stage the committer owns'
    )
    assert queued_intents(queue) == []


async def test_a_run_of_real_intent_changes_is_not_swallowed_by_superseding():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder)
    await elapse(light, clock, 30.0)
    decoder._script.append(
        [BarDecision(0, 'breakdown', 30.0 - 13.9), BarDecision(1, 'drop', 30.0 - 12.0)]
    )
    await light.on_beat(1, 128.0, False)
    assert len(queued_intents(queue)) == 2, 'a later bar cancelled an earlier one'

    seen = []
    for _ in range(30):
        await elapse(light, clock, 0.25)
        await queue.drain()
        if light.current_intent is not None and (not seen or seen[-1] is not light.current_intent):
            seen.append(light.current_intent)
    assert seen == [LightIntent.BREAKDOWN, LightIntent.DROP]


async def test_the_stream_is_deduplicated_against_what_it_will_show():
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', age_sec=13.0)
    await elapse(light, clock, 0.2)
    await commit(light, decoder, clock, 'drop', age_sec=13.2)
    assert len(queued_intents(queue)) == 1


async def test_the_stage_goes_dark_when_the_confirmed_silence_reaches_the_room():
    light, queue, clock, lighting = engine(decoder=FakeDecoder())
    blackout = []
    lighting.stop = lambda: blackout.append(clock.monotonic())
    await elapse(light, clock, 5.0)
    at_stop = clock.monotonic()
    light.on_sound_stop()

    await hold_silence(light, clock)
    await queue.drain()
    assert blackout == []
    clock.advance(at_stop + queue.delay_sec - clock.monotonic())
    await queue.drain()
    assert blackout == [at_stop + queue.delay_sec]


async def test_the_engines_own_bookkeeping_at_a_boundary_is_not_delayed():
    chain, decoder = FakeChain(), FakeDecoder()
    light, _queue, clock, _ = engine(decoder=decoder, chain=chain)
    await elapse(light, clock, 5.0)
    light.on_sound_stop()
    assert (chain.resets, decoder.resets) == (1, 1)


async def test_a_block_records_the_song_instant_it_describes():
    decoder = FakeDecoder()
    light, queue, clock, _ = engine(decoder=decoder, events=True)
    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', age_sec=13.7)
    await settle(light, clock, queue)

    block = light.event_buffer.to_report()['intents'][0]
    assert block['song_t'] == pytest.approx(20.0 - 13.7, abs=1e-6)
    assert block['t'] == pytest.approx(20.3, abs=0.25)


def _posterior(time_sec, boundary):
    class P:
        pass

    p = P()
    p.time_sec = time_sec
    p.posterior = np.full(5, 0.2)
    p.boundary = boundary
    return p


async def test_two_clamped_decisions_both_reach_the_stage():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await elapse(light, clock, 40.0)

    decoder._script.append(
        [
            BarDecision(10, 'drop', light.audio_sec - 15.6),
            BarDecision(11, 'breakdown', light.audio_sec - 15.2),
        ]
    )
    await light.on_beat(1, 128.0, False)
    await settle(light, clock, queue)

    assert [block['intent'] for block in light.event_buffer.snapshot()['intents']] == [
        'drop',
        'breakdown',
    ]


async def test_a_clamped_pair_is_delivered_in_commit_order():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await elapse(light, clock, 40.0)

    decoder._script.append(
        [
            BarDecision(10, 'drop', light.audio_sec - 15.6),
            BarDecision(11, 'breakdown', light.audio_sec - 15.2),
        ]
    )
    await light.on_beat(1, 128.0, False)
    await settle(light, clock, queue)

    assert light.current_intent is LightIntent.BREAKDOWN


async def test_beat_absence_holds_the_loop_until_a_committed_section_change():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await elapse(light, clock, 40.0)
    await bars(light, decoder, clock, 'drop')
    await settle(light, clock, queue)

    light.analyser.since_beat = 3.0
    await light.on_100ms_callback()
    assert light.decided_intent is LightIntent.DROP

    light.analyser.since_beat = 0.0
    await bars(light, decoder, clock, 'breakdown')
    await settle(light, clock, queue)
    assert light.current_intent is LightIntent.BREAKDOWN


async def tick_100ms(light, clock, sec, step=0.1):
    for _ in range(int(sec / step)):
        await elapse(light, clock, step)
        await light.on_100ms_callback()


async def test_a_committer_that_never_speaks_still_lights_the_rig():
    from autolux.lib.engine.light_engine import COLD_START_FLOOR_MARGIN_SEC

    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder, events=True)
    await tick_100ms(light, clock, decoder.chain_latency_sec + COLD_START_FLOOR_MARGIN_SEC + 1.0)
    await settle(light, clock, queue)

    assert light.current_intent is LightIntent.ATMOSPHERIC
    assert [e['command']['kind'] for e in lighting.events if e['command']['kind'] == 'select'], (
        'the rig stayed dark'
    )


async def test_the_floor_fires_once_and_not_once_per_callback():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await tick_100ms(light, clock, decoder.chain_latency_sec + 20.0)
    await settle(light, clock, queue)

    assert len(light.event_buffer.snapshot()['intents']) == 1


async def test_a_decision_that_arrives_in_time_beats_the_floor():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await tick_100ms(light, clock, 2.0)
    await bars(light, decoder, clock, 'drop')
    await tick_100ms(light, clock, decoder.chain_latency_sec + 20.0)
    await settle(light, clock, queue)

    assert [b['intent'] for b in light.event_buffer.snapshot()['intents']] == ['drop']


async def test_the_floor_describes_the_audio_the_room_is_hearing_now():
    from autolux.lib.engine.light_engine import COLD_START_FLOOR_MARGIN_SEC

    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await tick_100ms(light, clock, decoder.chain_latency_sec + COLD_START_FLOOR_MARGIN_SEC + 0.5)

    pending = queued_intents(queue)
    assert len(pending) == 1
    assert pending[0][3] == pytest.approx(0.0, abs=1e-9)


async def test_a_new_song_re_arms_the_floor_and_commits_a_second_time():
    decoder = FakeDecoder()
    light, queue, clock, _lighting = engine(decoder=decoder, events=True)
    await tick_100ms(light, clock, decoder.chain_latency_sec + 10.0)
    await settle(light, clock, queue)
    commits = light.intent_commits
    assert commits == 1

    light.on_sound_stop()
    stopped_at = clock.monotonic()
    await hold_silence(light, clock)
    clock.advance(stopped_at + queue.delay_sec - clock.monotonic())
    await queue.drain()
    light.on_sound_start()
    await tick_100ms(light, clock, decoder.chain_latency_sec + 10.0)
    await settle(light, clock, queue)

    assert light.intent_commits == commits + 1


async def test_a_machine_with_no_committer_at_all_lights_the_floor_too():
    from autolux.lib.engine.light_engine import COLD_START_FLOOR_MARGIN_SEC

    light, queue, clock, _lighting = engine(events=True)
    await tick_100ms(light, clock, COLD_START_FLOOR_MARGIN_SEC + 1.0)
    await settle(light, clock, queue)

    assert light.current_intent is LightIntent.ATMOSPHERIC


async def boundary(light, chain, clock, score, *, age_sec=8.0, sec=0.5):
    from autolux.lib.analyser.section_model import Posterior

    chain.pending.append(Posterior(0, light.audio_sec + sec - age_sec, np.zeros(5), score))
    await elapse(light, clock, sec)


def selections(lighting):
    return [e for e in lighting.events if e['command']['kind'] == 'select']


async def held(light, decoder, chain, clock, queue, label='drop'):
    await elapse(light, clock, 20.0)
    await bars(light, decoder, clock, label)
    await settle(light, clock, queue)


@pytest.mark.parametrize('score', [0.0, 0.5, 1.0])
async def test_boundary_scores_never_replace_a_held_sections_loop(score):
    decoder, chain = FakeDecoder(), FakeChain()
    light, queue, clock, lighting = engine(decoder=decoder, chain=chain, events=True)
    await held(light, decoder, chain, clock, queue, label='breakdown')
    before = list(selections(lighting))
    blocks = len(light.event_buffer.snapshot()['intents'])

    for _ in range(3):
        await boundary(light, chain, clock, score)
        await settle(light, clock, queue)

    assert selections(lighting) == before
    assert len(light.event_buffer.snapshot()['intents']) == blocks


@pytest.mark.parametrize('queued', [False, True])
@pytest.mark.parametrize(
    'labels',
    [
        ('intro', 'altintro', 'outro', 'altoutro'),
        ('breakdown', 'bridge', 'cooldown'),
    ],
)
async def test_same_family_sections_preserve_the_autoloop_but_commit_their_boundaries(
    queued, labels
):
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder, queue=queued)
    await elapse(light, clock, 20.0)
    for label in labels:
        await bars(light, decoder, clock, label, label)
    if queued:
        await settle(light, clock, queue)

    assert len(selections(lighting)) == 1
    assert light.intent_commits == len(labels)
    assert selections(lighting)[0]['command']['bank'] == light.current_intent.value


async def test_a_same_family_section_change_waits_for_room_playback():
    decoder, chain = FakeDecoder(), FakeChain()
    light, queue, clock, lighting = engine(decoder=decoder, chain=chain)
    await held(light, decoder, chain, clock, queue, label='intro')
    before = list(selections(lighting))
    await commit(light, decoder, clock, 'outro', age_sec=8.0)

    clock.advance(5.99)
    await queue.drain()
    assert selections(lighting) == before
    clock.advance(0.02)
    await queue.drain()
    assert selections(lighting) == before
    assert light._section_class_at_playback == 'outro'
    assert light.current_intent is LightIntent.ATMOSPHERIC


async def test_decoder_gap_recovery_keeps_the_loop_for_the_same_section():
    decoder, chain = FakeDecoder(), FakeChain()
    light, queue, clock, lighting = engine(decoder=decoder, chain=chain)
    await held(light, decoder, chain, clock, queue, label='breakdown')
    before = list(selections(lighting))

    chain.gap = True
    await elapse(light, clock, 0.5)
    await boundary(light, chain, clock, 1.0)
    await bars(light, decoder, clock, 'breakdown')
    await settle(light, clock, queue)
    assert selections(lighting) == before


async def test_boundaries_leave_a_pending_section_change_to_fire_once():
    decoder, chain = FakeDecoder(), FakeChain()
    light, queue, clock, lighting = engine(decoder=decoder, chain=chain)
    await held(light, decoder, chain, clock, queue, label='intro')
    before = len(selections(lighting))
    await bars(light, decoder, clock, 'breakdown', age_sec=0.5)

    await boundary(light, chain, clock, 1.0)
    await settle(light, clock, queue)
    assert len(selections(lighting)) == before + 1
    assert light.current_intent is LightIntent.BREAKDOWN


async def test_a_boundary_before_anything_is_committed_lights_nothing():
    decoder, chain = FakeDecoder(), FakeChain()
    light, queue, clock, lighting = engine(decoder=decoder, chain=chain)
    await elapse(light, clock, 20.0)
    await boundary(light, chain, clock, 1.0)
    await settle(light, clock, queue)
    assert selections(lighting) == []


async def test_the_decoder_state_reaches_the_event_buffer():
    decoder = FakeDecoder()
    light, _, clock, _ = engine(decoder=decoder, events=True)
    decoder.recent_observations.append(
        BarObservation(7, 13.2, 15.1, np.array([0.05, 0.1, 0.1, 0.7, 0.05]), 0.62)
    )

    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', bar=5)

    state = light.event_buffer.snapshot()['decoder']
    assert state['classes'] == list(FakeDecoder.classes)
    assert state['posterior'] == [0.05, 0.1, 0.1, 0.7, 0.05]
    assert (state['observed_bar'], state['committed_bar']) == (7, 5)
    assert state['committed_label'] == 'drop'
    assert state['lag_bars'] == 2
    assert state['chain_latency_sec'] == FakeDecoder.chain_latency_sec


async def test_a_bar_with_no_evidence_says_so_rather_than_reading_as_silence():
    decoder = FakeDecoder()
    light, _, clock, _ = engine(decoder=decoder, events=True)
    decoder.recent_observations.append(BarObservation(7, 13.2, 15.1, None, float('nan')))

    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', bar=5)

    assert light.event_buffer.snapshot()['decoder']['posterior'] is None


async def test_the_decoder_state_stays_out_of_the_report():
    decoder = FakeDecoder()
    light, _, clock, _ = engine(decoder=decoder, events=True)
    decoder.recent_observations.append(BarObservation(7, 13.2, 15.1, np.array([0.2] * 5), 0.1))

    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', bar=5)

    report = light.event_buffer.to_report()
    assert 'decoder' not in report
    assert 'decoder' not in report['metrics']


async def test_a_song_boundary_forgets_the_commit_cursor():
    decoder = FakeDecoder()
    light, _, clock, _ = engine(decoder=decoder, events=True)
    decoder.recent_observations.append(BarObservation(7, 13.2, 15.1, np.array([0.2] * 5), 0.1))
    await elapse(light, clock, 20.0)
    await commit(light, decoder, clock, 'drop', bar=5)

    light.on_sound_start()
    decoder.recent_observations.clear()
    await light.on_beat(1, 128.0, False)

    state = light.event_buffer.snapshot()['decoder']
    assert state['committed_bar'] is None
    assert state['lag_bars'] is None


async def test_a_feature_gap_forgets_the_commit_cursor():
    decoder = FakeDecoder()
    chain = FakeChain()
    light, _, clock, _ = engine(decoder=decoder, chain=chain, events=True)
    decoder.recent_observations.append(BarObservation(34, 60.0, 62.0, np.array([0.2] * 5), 0.1))
    await elapse(light, clock, 60.0)
    await commit(light, decoder, clock, 'breakdown', bar=32)
    assert light.event_buffer.snapshot()['decoder']['committed_bar'] == 32

    chain.gap = True
    decoder.recent_observations.clear()
    await elapse(light, clock, 1.0)

    state = light.event_buffer.snapshot()['decoder']
    assert state['committed_bar'] is None
    assert state['lag_bars'] is None


async def test_a_song_boundary_keeps_the_intents_for_buffered_audio():
    decoder, chain = FakeDecoder(), FakeChain()
    light, queue, clock, _lighting = engine(decoder=decoder, chain=chain, events=True)
    await held(light, decoder, chain, clock, queue, label='drop')

    await bars(light, decoder, clock, 'breakdown', age_sec=0.5)
    assert len(queued_intents(queue)) == 1
    assert light.decided_intent is LightIntent.BREAKDOWN

    light.on_sound_stop()

    assert len(queued_intents(queue)) == 1
    assert light.decided_intent is LightIntent.BREAKDOWN


def test_the_grid_never_tears_itself_down_before_the_show_has_gone_quiet():
    from autolux.lib.engine import light_engine, section_decoder

    assert section_decoder._BEAT_GAP_SEC > light_engine.BEAT_LOSS_SECONDS, (
        'the bar grid re-anchors while the engine still believes a section is '
        'playing: the decisions either side of the gap would name bars on two '
        'different grids with nothing on screen saying the beat had stopped'
    )


async def test_a_brief_gap_between_songs_preserves_the_audible_tail():
    light, queue, clock, lighting = engine(events=True)
    blackout = []
    lighting.stop = lambda: blackout.append(clock.monotonic())
    await elapse(light, clock, 5.0)
    light.on_sound_stop()
    await hold_silence(light, clock, STOP_PERSISTENCE_SEC - 0.1)
    light.on_sound_start()
    await hold_silence(light, clock, 30.0)
    await queue.drain()
    assert blackout == []
    assert not any(
        block['trigger'] == SILENCE_TRIGGER for block in light.event_buffer.to_report()['intents']
    )


async def test_confirmed_stop_preserves_commands_until_the_tail_finishes():
    light, queue, clock, lighting = engine(events=True)
    fired = []
    lighting.stop = lambda: fired.append(('stop', clock.monotonic()))

    async def beat():
        fired.append(('beat', clock.monotonic()))

    queue.schedule('beat', beat)
    clock.advance(1.0)
    light.on_sound_stop()
    await hold_silence(light, clock)
    await queue.drain()
    assert fired == []
    clock.advance(14.0 - clock.monotonic())
    await queue.drain()
    assert fired == [('beat', 14.0)]
    clock.advance(1.0)
    await queue.drain()
    assert fired == [('beat', 14.0), ('stop', 15.0)]


async def test_a_later_song_survives_the_previous_songs_delayed_stop():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder, playing=False)
    light.on_sound_start()
    clock.advance(1.0)
    light.on_sound_stop()
    await hold_silence(light, clock)
    light.on_sound_start()
    second_start = clock.monotonic() + queue.delay_sec
    await commit(light, decoder, clock, 'drop', age_sec=0.0)

    clock.advance(14.0 - clock.monotonic())
    await queue.drain()
    assert light.current_intent is LightIntent.ATMOSPHERIC
    clock.advance(1.0)
    await queue.drain()
    assert not light._room_playing
    clock.advance(second_start - clock.monotonic())
    await queue.drain()
    assert light.current_intent is LightIntent.DROP
    assert [event['command']['bank'] for event in selections(lighting)] == ['atmospheric', 'drop']


async def test_same_intent_is_sent_again_after_a_confirmed_stop_before_the_next_song():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder)
    await commit(light, decoder, clock, 'drop', age_sec=14.0)
    await queue.drain()
    light.on_sound_stop()
    await hold_silence(light, clock)
    light.on_sound_start()
    await commit(light, decoder, clock, 'drop', age_sec=0.0)
    clock.advance(14.0 - clock.monotonic())
    await queue.drain()
    assert not light._room_playing
    clock.advance(2.1)
    await queue.drain()
    assert light.current_intent is LightIntent.DROP
    assert len(selections(lighting)) == 2


@pytest.mark.parametrize('queued', [False, True])
async def test_quiet_floor_reports_source_and_room_time_separately(queued):
    light, queue, clock, _ = engine(events=True, queue=queued)
    clock.advance(37.0)
    light.on_sound_stop()
    await hold_silence(light, clock)
    if queue:
        assert light.event_buffer.to_report()['intents'] == []
        clock.advance(51.0 - clock.monotonic())
        await queue.drain()
    block = light.event_buffer.to_report()['intents'][-1]
    assert block['trigger'] == SILENCE_TRIGGER
    assert block['song_t'] == 37.0
    assert block['t'] == pytest.approx(51.0 if queued else 39.0, abs=0.2)


async def test_the_engine_publishes_what_the_watchdog_knows_about_shedding():
    from autolux.lib.analyser.drift_watchdog import DriftWatchdog

    light, _, clock, _ = engine(events=True)
    light._watchdog = DriftWatchdog(256 / SAMPLE_RATE, clock=clock)

    await elapse(light, clock, 1.0)
    assert light.event_buffer.snapshot()['shed']['level'] == 'NONE'

    light._watchdog.report_fault('hung_pass')
    await elapse(light, clock, 1.0)
    shed = light.event_buffer.snapshot()['shed']
    assert (shed['level'], shed['fault']) == ('NN_SHED', 'hung_pass')
    assert (shed['sheds'], shed['sheds_per_min']) == (1, 1)


async def test_a_shed_shorter_than_the_publish_step_is_still_counted():
    from autolux.lib.analyser.drift_watchdog import DriftWatchdog

    light, _, clock, _ = engine(events=True)
    light._watchdog = DriftWatchdog(256 / SAMPLE_RATE, clock=clock)
    await elapse(light, clock, 1.0)

    light._watchdog.report_fault('hung_pass')
    light._watchdog.report_healthy()
    await elapse(light, clock, 1.0)

    shed = light.event_buffer.snapshot()['shed']
    assert shed['level'] == 'NONE', 'the level poll should have missed it'
    assert shed['sheds'] == 1, 'and the counter should not have'


async def test_a_show_with_no_watchdog_publishes_nothing_rather_than_zeroes():
    light, _, clock, _ = engine(events=True)
    await elapse(light, clock, 1.0)
    assert light.event_buffer.snapshot()['shed'] == {}


async def test_the_grid_is_published_on_the_clock_the_beats_are_stamped_on():
    decoder = FakeDecoder()
    light, _, clock, _ = engine(decoder=decoder, events=True)
    await elapse(light, clock, 20.0)
    await light.on_beat(1, 128.0, False)

    beat_at = light.event_buffer.snapshot()['beats'][-1]['t']
    decoder._edges = [light.audio_sec - 4.0, light.audio_sec - 2.0]
    light._publish_decoder_state(None)

    published = light.event_buffer.snapshot()['decoder']['bar_edges']
    assert published == [
        pytest.approx(beat_at - 4.0, abs=0.05),
        pytest.approx(beat_at - 2.0, abs=0.05),
    ]


async def test_the_grid_travels_with_the_bar_numbers_that_name_it():
    decoder = FakeDecoder()
    light, _, clock, _ = engine(decoder=decoder, events=True)
    await elapse(light, clock, 20.0)
    decoder._edges = [light.audio_sec - 2.0, light.audio_sec]
    decoder._first_bar = 41
    light._publish_decoder_state(None)

    state = light.event_buffer.snapshot()['decoder']
    assert state['first_bar'] == 41
    assert state['bar_sec'] == pytest.approx(FakeDecoder.bar_sec)


async def beat_at_phase(light, decoder, phase, *, decision=None):
    decoder.beat_phase = phase
    if decision is not None:
        decoder._script.append([BarDecision(0, decision, light.audio_sec)])
    await light.on_beat(1, 128.0, False)


def grid_positions(lighting):
    return [
        e['command']['position_beats'] for e in lighting.events if e['command']['kind'] == 'beat'
    ]


async def test_downbeats_land_on_bar_lines_and_a_phase_correction_never_rewinds_the_grid():
    decoder = FakeDecoder()
    light, _, _, lighting = engine(decoder=decoder, queue=False)
    # The tracker skips phase 2 (a forward correction), then pulls the bar back a beat.
    phases = [1, 2, 3, 0, 1, 3, 0, 1, 0]
    for phase in phases:
        await beat_at_phase(light, decoder, phase)

    positions = grid_positions(lighting)
    assert positions == [1, 2, 3, 4, 5, 7, 8, 9, 12]
    assert all(pos % 4 == 0 for pos, phase in zip(positions, phases) if phase == 0)
    assert positions == sorted(set(positions))


async def test_an_intent_change_waits_for_the_rooms_next_downbeat_and_the_newest_one_wins():
    decoder = FakeDecoder()
    light, queue, clock, lighting = engine(decoder=decoder)
    await elapse(light, clock, 20.0)
    await beat_at_phase(light, decoder, 1)
    await elapse(light, clock, 0.5)
    await beat_at_phase(light, decoder, 2, decision='buildup')
    await elapse(light, clock, 0.5)
    await beat_at_phase(light, decoder, 3, decision='drop')
    await elapse(light, clock, 0.5)
    await beat_at_phase(light, decoder, 0)
    downbeat_reaches_room = clock.monotonic() + 14.0

    clock.advance(13.9)
    await queue.drain()
    assert selections(lighting) == [], 'switched before the downbeat reached the room'
    assert light.current_intent is LightIntent.DROP

    clock.advance(0.2)
    await queue.drain()
    [selected] = selections(lighting)
    assert selected['command']['autoloop'] == PREFERRED_AUTOLOOPS[LightIntent.DROP]
    assert selected['at_seconds'] == pytest.approx(downbeat_reaches_room, abs=0.15)


async def test_a_held_intent_is_released_when_the_rooms_beats_stop():
    decoder = FakeDecoder()
    light, _, clock, lighting = engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 1)
    await beat_at_phase(light, decoder, 2, decision='breakdown')
    assert selections(lighting) == []

    clock.advance(3.0)
    await light.on_100ms_callback()
    assert [e['command']['autoloop'] for e in selections(lighting)] == [
        PREFERRED_AUTOLOOPS[LightIntent.BREAKDOWN]
    ]


def masters(lighting):
    return [
        e['command']['intensity'] for e in lighting.events if e['command']['kind'] == 'set_master'
    ]


async def tick(light, clock, sec: float) -> None:
    for _ in range(round(sec / 0.1)):
        clock.advance(0.1)
        await light.on_100ms_callback()


async def test_a_quiet_passage_dims_the_rig_once_and_recovers_once():
    """Live rig: a fade-out to near silence kept the loops at full output."""
    light, _, clock, lighting = engine(queue=False)
    light.analyser.rms = 0.003  # about -50 dBFS
    await tick(light, clock, 1.5)
    assert masters(lighting) == [], 'a short dip is not a quiet passage'
    await tick(light, clock, 3.0)
    assert masters(lighting) == [QUIET_BRIGHTNESS]

    light.analyser.rms = 0.1
    await tick(light, clock, 3.0)
    assert masters(lighting) == [QUIET_BRIGHTNESS, 1.0]


async def test_a_lost_beat_dims_the_rig_until_beats_resume():
    light, _, clock, lighting = engine(queue=False)
    light.analyser.since_beat = BEAT_LOSS_SECONDS + 0.5
    await tick(light, clock, 2.0)
    assert masters(lighting) == [QUIET_BRIGHTNESS]

    light.analyser.since_beat = 0.0
    await tick(light, clock, 2.0)
    assert masters(lighting) == [QUIET_BRIGHTNESS, 1.0]


async def test_a_stopped_source_is_quiet():
    light, _, clock, lighting = engine(queue=False)
    light.analyser.playing = False
    await tick(light, clock, 0.5)
    assert masters(lighting) == [QUIET_BRIGHTNESS]


async def test_the_dim_waits_for_the_room_to_hear_the_quiet():
    light, queue, clock, lighting = engine()
    light.analyser.since_beat = BEAT_LOSS_SECONDS + 0.5
    await tick(light, clock, 0.1)
    await queue.drain()
    assert masters(lighting) == []
    clock.advance(14.0)
    await queue.drain()
    assert masters(lighting) == [QUIET_BRIGHTNESS]


def master_commands(lighting):
    return [e['command'] for e in lighting.events if e['command']['kind'] == 'set_master']


async def test_pause_dims_with_a_fade_and_holds_the_running_loop():
    decoder = FakeDecoder()
    light, _, _, lighting = engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    assert len(selections(lighting)) == 1

    light.pause()
    assert master_commands(lighting) == [
        {'kind': 'set_master', 'intensity': QUIET_BRIGHTNESS, 'fade_seconds': 1.0}
    ]
    await beat_at_phase(light, decoder, 1, decision='drop')
    await beat_at_phase(light, decoder, 0)
    assert len(selections(lighting)) == 1, 'a paused show must not follow the model'
    assert light.current_intent is LightIntent.DROP


async def test_play_applies_the_current_intent_on_the_next_downbeat_and_restores_brightness():
    decoder = FakeDecoder()
    light, _, _, lighting = engine(decoder=decoder, queue=False)
    await beat_at_phase(light, decoder, 0, decision='breakdown')
    light.pause()
    await beat_at_phase(light, decoder, 1, decision='drop')

    await light.resume()
    assert master_commands(lighting)[-1] == {
        'kind': 'set_master',
        'intensity': 1.0,
        'fade_seconds': 1.0,
    }
    await beat_at_phase(light, decoder, 2)
    assert len(selections(lighting)) == 1, 'the change waits for the downbeat'
    await beat_at_phase(light, decoder, 0)
    assert selections(lighting)[-1]['command']['autoloop'] == PREFERRED_AUTOLOOPS[LightIntent.DROP]


async def test_master_scales_the_quiet_dim_and_bypasses_the_playback_delay():
    light, queue, clock, lighting = engine()
    light.set_master_level(0.5)
    assert masters(lighting) == [0.5], 'a knob turn must not wait out the show delay'

    light.analyser.since_beat = BEAT_LOSS_SECONDS + 0.5
    await tick(light, clock, 0.1)
    clock.advance(14.0)
    await queue.drain()
    assert masters(lighting) == [0.5, pytest.approx(0.075)]
    light.set_master_level(0.5)
    assert len(masters(lighting)) == 2, 'an unchanged level is not resent'


async def test_show_control_settings_survive_a_restart_and_apply_without_one(tmp_path):
    from autolux.lib.engine.effect_definitions import ShowSettings
    from autolux.lib.main import ShowControl, load_show_settings

    path = tmp_path / 'show.json'
    path.write_text('{"keep": "me"}')
    control = ShowControl(path)
    assert control.settings == ShowSettings()
    light, _, _, lighting = engine(queue=False)
    light._settings = lambda: control.settings
    light.pause()

    control.commands.put_nowait(
        {'action': 'settings', 'settings': ShowSettings(quiet_brightness=0.3)}
    )
    await control.apply_pending(light)
    assert masters(lighting)[-1] == pytest.approx(0.3)
    assert load_show_settings(path) == ShowSettings(quiet_brightness=0.3)
    assert ShowControl(path).settings == control.settings
    assert '"keep": "me"' in path.read_text()


@pytest.mark.parametrize(
    'bad',
    [
        {'quiet_brightness': 1.5},
        {'crossfade_beats': 0},
        {'strobe_burst_seconds': True},
        {'drop_strobe_cooldown_seconds': float('nan')},
        {'digital_silence_seconds': 0.1},
        {'unknown': 1},
    ],
)
def test_invalid_show_settings_are_refused(bad):
    from autolux.lib.engine.effect_definitions import ShowSettings

    with pytest.raises(ValueError):
        ShowSettings.from_dict(bad)


class _MetronomeRhythm:
    """A beat every half second (120 BPM), so standby tests run without madmom."""

    def __init__(self):
        from autolux.lib.analyser.madmom_rhythm import RhythmEvents

        self._events = RhythmEvents
        self.pending_latency_sec = 0.0
        self.samples = 0

    def reset(self):
        self.samples = 0

    def process(self, buffer):
        half_second = SAMPLE_RATE // 2
        before, self.samples = self.samples, self.samples + len(buffer)
        on_beat = self.samples // half_second > before // half_second
        return self._events(beats=[self.samples / SAMPLE_RATE] if on_beat else [])


class _Show:
    """The production loop on a virtual clock: engine, analyser, queue, timers."""

    def __init__(self):
        from autolux.lib.analyser.music_analyser import MusicAnalyser
        from autolux.lib.audio_config import BUFFER_SIZE

        self.clock = VirtualClock()
        self.lighting = RecordingStarfireClient(clock=self.clock)
        self.queue = DelayedCommandQueue(14.0, clock=self.clock)
        self.chain, self.decoder = FakeChain(), FakeDecoder()
        self.monitor_silenced = 0
        self.light = LightEngine(
            self.lighting,
            EffectController(self.lighting, clock=self.clock),
            self.queue,
            playback_delay_sec=14.0,
            section_chain=self.chain,
            section_decoder=self.decoder,
            clock=self.clock,
            silence_monitor=self._silence_monitor,
        )
        self.analyser = MusicAnalyser(SAMPLE_RATE, BUFFER_SIZE, self.light, clock=self.clock)
        self.analyser._rhythm = _MetronomeRhythm()
        self.light.set_analyser(self.analyser)
        self._buffer_size = BUFFER_SIZE
        self._next_tick = 0.1

    def _silence_monitor(self):
        self.monitor_silenced += 1

    async def play(self, seconds: float, level: float) -> None:
        step = self._buffer_size / SAMPLE_RATE
        for _ in range(round(seconds / step)):
            buffer = np.full(self._buffer_size, level, dtype=np.float32)
            self.clock.advance(step)
            await self.light.on_audio(buffer)
            await self.analyser.analyse(buffer)
            await self.queue.drain()
            if self.clock.monotonic() >= self._next_tick:
                self._next_tick += 0.1
                await self.light.on_100ms_callback()

    def commands(self, since: int = 0) -> list[dict]:
        return [event['command'] for event in self.lighting.events[since:]]


def _selections(commands):
    return [(c['bank'], c['autoloop']) for c in commands if c['kind'] == 'select']


async def test_two_seconds_of_digital_silence_park_the_rig_and_silence_the_room_at_once():
    show = _Show()
    await show.play(20.0, 0.2)
    assert _selections(show.commands()) == [('atmospheric', 'cold_breathe')]
    assert show.queue.pending > 0, 'beats for the buffered audio are still due'
    stopped = len(show.lighting.events)

    await show.play(1.9, 0.0)
    assert not show.light.standby
    assert {'kind': 'discard_delayed_audio'} not in show.commands(stopped)

    await show.play(0.2, 0.0)
    assert show.light.standby
    standby = show.commands(stopped)
    assert standby.count({'kind': 'discard_delayed_audio'}) == 1
    assert show.monitor_silenced == 1
    assert show.queue.pending == 0, 'the delayed queue must be purged'
    select = next(c for c in standby if c['kind'] == 'select')
    assert (select['bank'], select['autoloop']) == ('standby', 'warm_white')
    # One second at the established 120 BPM; Starfire fades in beats.
    assert select['transition'] == {
        'kind': 'crossfade',
        'duration_beats': pytest.approx(2.0, rel=0.01),
    }
    assert {'kind': 'clear_override', 'id': 'drop_arrival'} in standby

    parked = len(show.lighting.events)
    chain_samples = show.chain.samples
    await show.play(20.0, 0.0)
    assert show.commands(parked) == [], 'nothing delayed may reach the parked rig'
    assert show.chain.samples == chain_samples, 'model decisions must hold while silent'


async def test_a_short_dropout_keeps_the_show_and_its_buffered_audio():
    show = _Show()
    await show.play(20.0, 0.2)
    before = len(show.lighting.events)
    await show.play(1.5, 0.0)
    await show.play(5.0, 0.2)
    assert not show.light.standby
    assert {'kind': 'discard_delayed_audio'} not in show.commands(before)
    assert ('standby', 'warm_white') not in _selections(show.commands(before))
    assert show.monitor_silenced == 0


async def test_returning_signal_restarts_the_show_from_scratch_after_one_playback_delay():
    show = _Show()
    await show.play(20.0, 0.2)
    await show.play(3.0, 0.0)
    assert show.light.standby
    chain_resets, decoder_resets = show.chain.resets, show.decoder.resets
    parked = len(show.lighting.events)

    await show.play(1.0, 0.2)
    assert not show.light.standby
    assert show.chain.resets > chain_resets and show.decoder.resets > decoder_resets
    assert show.analyser.beat_count <= 2, 'the beat stream restarted with the signal'
    assert show.light.audio_sec <= 1.0, 'the chain clock counts only the returning audio'
    assert show.light._pending_intents == [] and show.light._held_intent is None

    # The room hears nothing new for one playback delay, so standby holds.
    await show.play(12.5, 0.2)
    assert _selections(show.commands(parked)) == []
    await show.play(1.5, 0.2)
    assert _selections(show.commands(parked)) == [('atmospheric', 'cold_breathe')]
    await show.play(3.0, 0.2)
    assert any(c['kind'] == 'beat' for c in show.commands(parked))
    positions = [c['position_beats'] for c in show.commands() if c['kind'] == 'beat']
    assert positions == sorted(set(positions)), (
        "Starfire's beat clock never moves backwards across the restart"
    )
