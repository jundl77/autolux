import datetime
import json
from unittest.mock import Mock

from autolux.lib.clock import VirtualClock
from autolux.lib.clients import os2l_messages
from autolux.lib.clients.os2l_client import Os2lClient
from autolux.lib.clients.os2l_sender import Os2lSender


def sender():
    clock = VirtualClock()
    client = Os2lSender(playback_delay_sec=14, clock=clock)
    client.os2l_socket.close()
    client.logon_complete = True
    client._send_message = Mock()
    return client, clock


def test_playhead_is_a_delayed_snapshot_not_the_current_analyser_position():
    client, clock = sender()
    analyser = Mock()
    analyser.is_song_playing.return_value = True
    analyser.get_beat_position.return_value = 4.5
    analyser.get_song_current_duration.return_value = datetime.timedelta(seconds=2)
    client.set_analyser(analyser)
    clock.advance(2)
    client._send_update_if_due()
    analyser.get_beat_position.return_value = 32.5
    analyser.get_song_current_duration.return_value = datetime.timedelta(seconds=16)
    clock.advance(13.99)
    client._poll_queue()
    client._send_message.assert_not_called()
    clock.advance(0.02)
    client._poll_queue()
    client._send_message.assert_called_once_with(os2l_messages.update_message(4.5, 2000))


def test_future_playhead_does_not_hold_up_an_already_delayed_beat():
    client, clock = sender()
    client.send_message('future playhead', delay_sec=14)
    client.send_message('room beat')
    client._poll_queue()
    assert [call.args[0] for call in client._send_message.call_args_list] == ['room beat']
    clock.advance(14)
    client._poll_queue()
    assert [call.args[0] for call in client._send_message.call_args_list] == ['room beat', 'future playhead']


def test_equal_deadline_messages_keep_their_order():
    client, clock = sender()
    for message in ('start', 'position', 'stop'):
        client.send_message(message, delay_sec=14)
    clock.advance(14)
    client._poll_queue()
    assert [call.args[0] for call in client._send_message.call_args_list] == ['start', 'position', 'stop']


async def test_client_delays_boundaries_but_never_delays_engine_beats_twice():
    client = Os2lClient(playback_delay_sec=14)
    client.os2l_sender.os2l_socket.close()
    client.os2l_sender = Mock()
    client.on_sound_start(0, 0, 20000, 120)
    client.on_sound_stop()
    await client.send_beat(False, 8, 120, 0.5)
    calls = client.os2l_sender.send_message.call_args_list
    assert calls[0].kwargs == {'delay_sec': 14}
    assert calls[0].args[0].endswith(os2l_messages.play_start_message())
    assert calls[1].kwargs == {'delay_sec': 14}
    assert calls[2].kwargs == {}
    assert calls[2].args[0] == os2l_messages.beat_message(False, 8, 120, 0.5)


def test_no_queued_message_is_sent_before_the_subscription():
    client, clock = sender()
    client.logon_complete = False
    client.send_message('room beat')
    client._poll_queue()
    client._send_message.assert_not_called()
    client.logon_complete = True
    client._poll_queue()
    client._send_message.assert_called_once_with('room beat')


def test_every_wire_message_is_a_stream_of_valid_json_objects():
    decoder = json.JSONDecoder()
    for message in (
        os2l_messages.logon_message(),
        os2l_messages.song_loaded_message(0, 0, 20000, 120),
        os2l_messages.play_start_message(),
        os2l_messages.play_stop_message(),
        os2l_messages.shutdown_message(),
        os2l_messages.update_message(4.5, 2000),
        os2l_messages.beat_message(False, 8, 120, 0.5),
    ):
        count = 0
        while message.strip():
            value, end = decoder.raw_decode(message.lstrip())
            assert value['evt'] in ('subscribed', 'beat')
            message = message.lstrip()[end:]
            count += 1
        assert count > 0
