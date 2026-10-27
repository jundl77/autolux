from unittest.mock import Mock

import pyaudio
import pytest

from autolux.lib.clients.pyaudio_client import PyAudioClient


@pytest.mark.parametrize('code', [pyaudio.paUnanticipatedHostError, pyaudio.paInputOverflowed])
def test_capture_failure_explains_host_errors_and_preserves_unrelated_errors(monkeypatch, code):
    monkeypatch.setattr('autolux.lib.clients.pyaudio_client.pyaudio.PyAudio', Mock)
    client = PyAudioClient(sample_rate=44100, buffer_size=256)
    error = OSError(code, 'capture failed')
    client.stream_in = Mock()
    client.stream_in.read.side_effect = error

    if code == pyaudio.paUnanticipatedHostError:
        with pytest.raises(RuntimeError, match='RDP session change') as failure:
            client.read()
        assert failure.value.__cause__ is error
    else:
        with pytest.raises(OSError) as failure:
            client.read()
        assert failure.value is error


@pytest.mark.parametrize('close_fails', [False, True])
def test_close_releases_both_streams_after_capture_device_failure(monkeypatch, close_fails):
    audio = Mock()
    monkeypatch.setattr('autolux.lib.clients.pyaudio_client.pyaudio.PyAudio', lambda: audio)
    client = PyAudioClient(sample_rate=48000, buffer_size=512)
    capture, playback = Mock(), Mock()
    client.stream_in, client.stream_out = capture, playback
    capture.stop_stream.side_effect = OSError('Stream not open')
    if close_fails:
        capture.close.side_effect = OSError('Capture device failed')
        with pytest.raises(OSError, match='Capture device failed'):
            client.close()
    else:
        client.close()

    capture.close.assert_called_once()
    playback.close.assert_called_once()
    audio.terminate.assert_called_once()
    capture.stop_stream.assert_not_called()
    assert client.stream_in is None
    assert client.stream_out is None
