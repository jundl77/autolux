"""Send one blackout overlay to the local Radius overlay listener."""

from autolux.lib.clients.overlay_client import OverlayClient


def main():
    client = OverlayClient(host="127.0.0.1")
    try:
        client.clear_all()
        client.flush_messages()
    finally:
        client.socket.close()


if __name__ == "__main__":
    main()
