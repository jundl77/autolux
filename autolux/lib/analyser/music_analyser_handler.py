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
        pass

    @abstractmethod
    def on_signal_return(self):
        pass

    def on_digital_gap(self, started_sec: float, seconds: float) -> None:
        pass

    @abstractmethod
    async def on_beat(self, beat_number: int, bpm: float, bpm_changed: bool) -> None:
        pass

    @abstractmethod
    async def on_note(self):
        pass
