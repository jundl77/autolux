import asyncio
import datetime
import logging
import random
import time
from pathlib import Path

from autolux.lib.audio_config import BUFFER_SIZE, PLAYBACK_DELAY_SEC, SAMPLE_RATE
from autolux.lib.clock import SYSTEM_CLOCK, Clock, VirtualClock
from autolux.simulate import cell_cache

LOGGER = logging.getLogger(__name__)

TIMING_TOLERANCE_SEC = 0.050
FAST_SIM_RANDOM_SEED = 1337


def build_simulation(
    audio_client,
    event_buffer=None,
    clock: Clock = SYSTEM_CLOCK,
    section: object | None = None,
    threaded: bool = False,
    data_dir=None,
):
    from autolux.lib.analyser.drift_watchdog import DriftWatchdog
    from autolux.lib.analyser.music_analyser import MusicAnalyser
    from autolux.lib.engine.delayed_command_queue import DelayedCommandQueue
    from autolux.lib.engine.effect_controller import EffectController
    from autolux.lib.engine.light_engine import LightEngine
    from autolux.simulate.lighting_recorder import RecordingStarfireClient

    lighting_client = RecordingStarfireClient(clock=clock)
    command_queue = DelayedCommandQueue(PLAYBACK_DELAY_SEC, clock=clock)

    watchdog = None
    if threaded:
        watchdog = DriftWatchdog(BUFFER_SIZE / SAMPLE_RATE, clock=clock)
    if section is None:
        section = load_section_chain(
            watchdog=watchdog, audio_client=audio_client, data_dir=data_dir
        )

    effect_controller = EffectController(lighting_client, event_buffer=event_buffer, clock=clock)
    light_engine = LightEngine(
        lighting_client,
        effect_controller,
        command_queue,
        event_buffer=event_buffer,
        playback_delay_sec=PLAYBACK_DELAY_SEC,
        section_chain=None if section is None else section.stream,
        section_decoder=None if section is None else section.decoder,
        watchdog=watchdog,
        clock=clock,
    )

    music_analyser = MusicAnalyser(
        SAMPLE_RATE, BUFFER_SIZE, light_engine, clock=clock, watchdog=watchdog
    )
    light_engine.set_analyser(music_analyser)

    return {
        'audio_client': audio_client,
        'lighting_client': lighting_client,
        'command_queue': command_queue,
        'music_analyser': music_analyser,
        'light_engine': light_engine,
        'event_buffer': event_buffer,
        'section': section,
    }, command_queue


def load_section_chain(watchdog=None, audio_client=None, data_dir=None):
    global _SECTION_CHAIN, _SECTION_CHAIN_DATA_DIR
    from autolux.lib import section_chain

    data_dir = Path(data_dir if data_dir is not None else section_chain.corpus_dir()).resolve()
    if watchdog is not None:
        if not _artifacts_or_degrade(data_dir):
            return None
        return section_chain.build_section_chain(data_dir=data_dir, watchdog=watchdog)

    plan = _cell_cache_plan(audio_client, data_dir)
    tracker_plan = _tracker_cache_plan(audio_client, data_dir)
    if plan is not None:
        expected = _expected_samples(audio_client)
        replay, reason = cell_cache.open_replay(*plan, expected_samples=expected)
        tracker_replay = None
        if tracker_plan is not None:
            from autolux.simulate import tracker_cache

            tracker_replay, tracker_reason = tracker_cache.open_replay(
                *tracker_plan, expected_samples=expected
            )
        else:
            tracker_reason = "no_tracker"
        # One decision for both caches: a run is warm only when every stage
        # this machine would run can be replayed.
        if replay is not None and (tracker_plan is None or tracker_replay is not None):
            LOGGER.info(
                f'[sim] replaying cached extractor cells '
                f'← {plan[0].name}'
                + (
                    ''
                    if tracker_replay is None
                    else f' and tracker chunks ← {tracker_plan[0].name}'
                )
            )
            return section_chain.build_section_chain(
                data_dir=data_dir, extractor=lambda _: replay, tracker=tracker_replay
            )
        LOGGER.info(
            f'[sim] extractor cells: {reason} / tracker chunks: '
            f'{tracker_reason} — this run needs the GPU'
        )

    if _SECTION_CHAIN is _UNBUILT or _SECTION_CHAIN_DATA_DIR != data_dir:
        _SECTION_CHAIN = (
            section_chain.build_section_chain(data_dir=data_dir)
            if _artifacts_or_degrade(data_dir)
            else None
        )
        _SECTION_CHAIN_DATA_DIR = data_dir
    if _SECTION_CHAIN is None:
        return None
    _SECTION_CHAIN.stream.reset()
    _SECTION_CHAIN.decoder.reset()
    if plan is None:
        return _SECTION_CHAIN
    return cell_cache.recording_chain(
        _SECTION_CHAIN,
        *plan,
        tracker_plan=tracker_plan,
        source_samples=lambda: audio_client.samples_read,
    )


def _artifacts_or_degrade(data_dir=None) -> bool:
    from autolux.lib import section_chain

    if section_chain.artifacts_present(data_dir):
        return True
    LOGGER.warning(
        '[sim] no NN artifacts on this machine — running '
        'the degradation state (beats, silence, held intent)'
    )
    return False


def _expected_samples(audio_client):
    if not hasattr(audio_client, 'total_samples'):
        return None
    audio_client.start_streams()
    return audio_client.total_samples


def _cell_cache_plan(audio_client, data_dir=None):
    from autolux.lib import section_chain

    path = getattr(audio_client, 'path', None)
    decode = getattr(audio_client, 'decode_path', None)
    if path is None or decode is None or not section_chain.artifacts_present(data_dir):
        return None
    geometry = section_chain.read_geometry(data_dir).stream
    return (
        cell_cache.sidecar_path(path, decode),
        cell_cache.cache_key(
            geometry, source_rate=SAMPLE_RATE, audio_path=path, decode_path=decode
        ),
    )


def _tracker_cache_plan(audio_client, data_dir=None):
    from autolux.lib import section_chain
    from autolux.lib.analyser import bar_tracker
    from autolux.simulate import tracker_cache

    path = getattr(audio_client, 'path', None)
    decode = getattr(audio_client, 'decode_path', None)
    if path is None or decode is None or not section_chain.bar_tracker_present(data_dir):
        return None
    record = bar_tracker.load_record(section_chain.generation_dir(data_dir))
    return (
        tracker_cache.sidecar_path(path, decode),
        tracker_cache.cache_key(
            record, source_rate=SAMPLE_RATE, audio_path=path, decode_path=decode
        ),
    )


def cache_misses(audio_client, data_dir=None) -> list[tuple[str, str]]:
    """The sidecars a run of this client would recompute rather than replay, as (kind, reason).

    Same plans and lookups as load_section_chain, so an empty result means the run replays
    every stage this machine would otherwise compute.
    """
    plan = _cell_cache_plan(audio_client, data_dir)
    tracker_plan = _tracker_cache_plan(audio_client, data_dir)
    if plan is None and tracker_plan is None:
        return []
    expected = _expected_samples(audio_client)
    misses = []
    if plan is not None:
        _, reason = cell_cache.open_replay(*plan, expected_samples=expected)
        if reason != 'hit':
            misses.append(('mertcells', reason))
    if tracker_plan is not None:
        from autolux.simulate import tracker_cache

        _, reason = tracker_cache.open_replay(*tracker_plan, expected_samples=expected)
        if reason != 'hit':
            misses.append(('bartracker', reason))
    return misses


_UNBUILT = object()
_SECTION_CHAIN = _UNBUILT
_SECTION_CHAIN_DATA_DIR = None


async def run_fast_simulation_components(
    audio_client,
    duration_sec: float = float('inf'),
    seed: int = FAST_SIM_RANDOM_SEED,
    *,
    data_dir=None,
):
    from autolux.lib.engine.event_buffer import EventBuffer

    random.seed(seed)
    clock = VirtualClock()
    event_buffer = EventBuffer(
        window_sec=float('inf'), clock=clock, look_ahead_sec=PLAYBACK_DELAY_SEC
    )
    components, command_queue = build_simulation(
        audio_client, event_buffer, clock=clock, data_dir=data_dir
    )
    event_buffer.start()
    await run_simulation(components, duration_sec, clock=clock)
    return components, command_queue


async def run_fast_simulation(
    audio_client,
    duration_sec: float = float('inf'),
    seed: int = FAST_SIM_RANDOM_SEED,
    *,
    data_dir=None,
):
    components, command_queue = await run_fast_simulation_components(
        audio_client, duration_sec, seed, data_dir=data_dir
    )
    return components['audio_client'], components['event_buffer'], command_queue


async def run_simulation(
    components: dict,
    duration_sec: float,
    clock: Clock = SYSTEM_CLOCK,
    pace_real_time: bool = False,
    monitor=None,
):
    audio_client = components['audio_client']
    music_analyser = components['music_analyser']
    command_queue = components['command_queue']

    is_virtual = isinstance(clock, VirtualClock)
    buffer_sec = BUFFER_SIZE / SAMPLE_RATE

    audio_client.start_streams()
    start_mono = clock.monotonic()
    wall_start = time.monotonic()
    buffers_fed = 0

    last_100ms = clock.now()
    last_1s = clock.now()
    last_10s = clock.now()

    LOGGER.info(
        f'[sim] starting simulation loop for {duration_sec:.1f}s '
        f'({"virtual" if is_virtual else "wall"} time)'
    )
    if monitor is not None:
        monitor.arm()
    while clock.monotonic() - start_mono < duration_sec and not audio_client.exhausted:
        audio_signal = audio_client.read()
        buffers_fed += 1
        if is_virtual:
            clock.advance(buffer_sec)
        if pace_real_time:
            deadline = wall_start + buffers_fed * buffer_sec
            sleep_sec = deadline - time.monotonic()
            if sleep_sec > 0:
                await asyncio.sleep(sleep_sec)

        await components['light_engine'].on_audio(audio_signal)
        monitored = await music_analyser.analyse(audio_signal)
        await command_queue.drain()
        if monitor is not None:
            monitor.feed(monitored)

        now = clock.now()
        if now - last_100ms > datetime.timedelta(milliseconds=100):
            last_100ms = now
            await components['light_engine'].on_100ms_callback()

        if now - last_1s > datetime.timedelta(seconds=1):
            last_1s = now
            await components['light_engine'].on_1sec_callback()

        if now - last_10s > datetime.timedelta(seconds=10):
            last_10s = now
            await components['light_engine'].on_10sec_callback()

    event_buffer = components.get('event_buffer')
    if event_buffer is not None:
        event_buffer.mark_end()

    if is_virtual:
        flush_until = clock.monotonic() + command_queue.delay_sec
        while clock.monotonic() < flush_until:
            clock.advance(buffer_sec)
            await command_queue.drain()
    elif pace_real_time:
        while command_queue.pending or (monitor is not None and monitor.buffered):
            await asyncio.sleep(buffer_sec)
            await command_queue.drain()
            if monitor is not None:
                monitor.drain()

    lighting_client = components.get('lighting_client')
    if lighting_client is not None:
        lighting_client.stop()
    if event_buffer is not None:
        event_buffer.stop_effects()
    audio_client.close()
    section = components.get('section')
    if section is not None:
        section.stop()
    LOGGER.info('[sim] simulation complete')
