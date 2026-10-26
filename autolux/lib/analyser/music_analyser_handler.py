import logging
from abc import ABC, abstractmethod


class IMusicAnalyserHandler(ABC):
    @abstractmethod
    def on_sound_start(self):
        pass

    @abstractmethod
    def on_sound_stop(self):
        pass

    @abstractmethod
    def on_digital_silence(self):
        """The input held exact zeros long enough to mean the source stopped."""

    @abstractmethod
    def on_signal_return(self):
        """Signal after digital silence; analysis has restarted from scratch."""

    @abstractmethod
    async def on_cycle(self):
        pass

    @abstractmethod
    async def on_beat(self, beat_number: int, bpm: float, bpm_changed: bool) -> None:
        pass

    @abstractmethod
    async def on_note(self):
        pass
