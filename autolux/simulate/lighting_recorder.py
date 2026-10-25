"""Record the same semantic commands that live mode sends to Starfire."""

from copy import deepcopy

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock


class RecordingStarfireClient(StarfireClient):
    def __init__(self, clock: Clock = SYSTEM_CLOCK):
        self.events: list[dict] = []
        self._clock = clock
        super().__init__(self._record)

    def _record(self, command: dict) -> None:
        self.events.append({'at_seconds': self._clock.monotonic(), 'command': deepcopy(command)})
