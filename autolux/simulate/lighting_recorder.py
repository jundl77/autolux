"""Record the same semantic commands that live mode sends to Starfire."""

from collections.abc import Sequence
from copy import deepcopy

from autolux.lib.clients.starfire_client import StarfireClient
from autolux.lib.clock import SYSTEM_CLOCK, Clock


class RecordingStarfireClient(StarfireClient):
    def __init__(self, clock: Clock = SYSTEM_CLOCK, groups: Sequence[dict] = ()):
        self.events: list[dict] = []
        self._clock = clock
        super().__init__(self._record, groups=lambda: groups)

    def _record(self, command: dict) -> None:
        self.events.append({'at_seconds': self._clock.monotonic(), 'command': deepcopy(command)})
