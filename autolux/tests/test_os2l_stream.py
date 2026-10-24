"""Actual TCP boundaries, disconnects and failed startup must be observable."""
import json
import socket
import threading
import time
from unittest.mock import Mock

import pytest

from autolux.lib.clients.os2l_sender import Os2lSender


@pytest.fixture
def stream():
    client = Os2lSender()
    client.os2l_socket.close()
    client.os2l_socket, peer = socket.socketpair()
    client.os2l_socket.settimeout(1)
    client._send_message = Mock()
    try:
        yield client, peer
    finally:
        client.stop()
        peer.close()


def test_split_utf8_and_coalesced_messages_are_not_treated_as_datagrams(stream):
    client, peer = stream
    message = json.dumps({'evt': 'subscribe', 'frequency': '25', 'name': 'é'}, ensure_ascii=False).encode()
    cut = message.index('é'.encode()) + 1
    peer.sendall(message[:cut])
    client._poll_socket()
    assert not client.logon_complete
    peer.sendall(message[cut:] + b'{}{"evt":"unrecognized"}')
    client._poll_socket()
    assert client.logon_complete
    assert client._receive_buffer == ''
    client._send_message.assert_called_once()


def test_peer_shutdown_is_a_failure_not_an_empty_json_document(stream):
    client, peer = stream
    peer.close()
    with pytest.raises(ConnectionError, match='closed'):
        client._poll_socket()


def test_incomplete_json_has_a_deadline(stream):
    client, peer = stream
    peer.sendall(b'{"evt":')
    client._poll_socket()
    client._receive_started = time.monotonic() - 6
    with pytest.raises(ValueError, match='Incomplete'):
        client._poll_socket()


@pytest.mark.parametrize('frequency', [0, -1, 60001, 'bad'])
def test_invalid_subscriptions_cannot_drive_an_unbounded_scheduler(stream, frequency):
    client, _ = stream
    with pytest.raises(ValueError):
        client._on_message({'evt': 'subscribe', 'frequency': frequency})


def test_connection_failure_reaches_the_caller():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    client = Os2lSender()
    try:
        with pytest.raises(RuntimeError, match='OS2L connection failed'):
            client.start('127.0.0.1', port)
    finally:
        client.stop()
    assert not client.sending_thread.is_alive()


def test_disconnect_after_subscription_stops_the_show_via_health_check():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    disconnect = threading.Event()
    def serve():
        with listener.accept()[0] as connection:
            connection.sendall(b'{"evt":"subscribe","frequency":"25"}')
            disconnect.wait(3)
    server = threading.Thread(target=serve)
    server.start()
    client = Os2lSender()
    client.analyser = Mock()
    client.analyser.is_song_playing.return_value = False
    try:
        client.start(*listener.getsockname())
        disconnect.set()
        client.sending_thread.join(timeout=3)
        with pytest.raises(RuntimeError, match='OS2L connection failed'):
            client.check_health()
        with pytest.raises(RuntimeError):
            client.send_message('must not accumulate behind a dead sender')
    finally:
        disconnect.set()
        client.stop()
        server.join(timeout=3)
        listener.close()
