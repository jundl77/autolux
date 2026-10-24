from unittest.mock import Mock
import datetime

import pytest

from autolux.lib.clients.midi_client import MidiClient
from autolux.lib.clients.midi_message import MidiChannel, get_midi_msg_off, get_midi_msg_on


@pytest.mark.parametrize('send_toggle', [False, True])
def test_playback_does_not_toggle_again_when_os2l_controls_it(monkeypatch, send_toggle):
    output = Mock()
    output.get_ports.return_value = ['test']
    monkeypatch.setattr('autolux.lib.clients.midi_client.rtmidi.MidiOut', lambda: output)
    monkeypatch.setattr('autolux.lib.clients.midi_client.time.sleep', lambda _: None)
    midi = MidiClient(0, send_midi_play_pause_toggle=send_toggle)
    midi.start()
    midi.on_sound_start()
    midi.on_sound_start()
    assert not midi.soundswitch_is_paused
    midi.on_sound_stop()
    midi.stop()
    assert midi.soundswitch_is_paused

    messages = [call.args[0] for call in output.send_message.call_args_list]
    assert messages.count(get_midi_msg_on(MidiChannel.PLAY_PAUSE)) == (2 if send_toggle else 0)
    assert [176, MidiChannel.AUTOLOOP_INTENSITY.value, 127] in messages
    assert [176, MidiChannel.AUTOLOOP_INTENSITY.value, 0] in messages
    output.close_port.assert_called_once()


@pytest.mark.parametrize('stop_early', [False, True])
async def test_strobe_releases_at_its_deadline_or_when_stopped(monkeypatch, stop_early):
    now = datetime.datetime(2026, 1, 1)
    clock = Mock()
    clock.now.side_effect = lambda: now
    output = Mock()
    output.get_ports.return_value = ['test']
    monkeypatch.setattr('autolux.lib.clients.midi_client.rtmidi.MidiOut', lambda: output)
    monkeypatch.setattr('autolux.lib.clients.midi_client.datetime.datetime', clock)
    midi = MidiClient(0, send_midi_play_pause_toggle=False)
    midi.start()
    strobe = MidiChannel.SPECIAL_EFFECT_STROBE
    await midi.set_special_effect(strobe, duration_sec=1)
    now += datetime.timedelta(seconds=0.9)
    await midi.on_100ms_callback()
    assert output.send_message.call_args_list[-1].args[0] == get_midi_msg_on(strobe)

    if stop_early:
        midi.stop()
    else:
        now += datetime.timedelta(seconds=0.2)
        await midi.on_100ms_callback()
    messages = [call.args[0] for call in output.send_message.call_args_list]
    assert messages.count(get_midi_msg_off(strobe)) == 1
    now += datetime.timedelta(seconds=2)
    await midi.on_100ms_callback()
    assert output.send_message.call_count == len(messages)
