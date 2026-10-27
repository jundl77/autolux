import logging
from contextlib import ExitStack

import numpy as np
import pyaudio

LOGGER = logging.getLogger(__name__)


class PyAudioClient:
    def __init__(
        self,
        sample_rate: int,
        buffer_size: int,
        input_device_index: int | None = None,
        output_device_index: int | None = None,
    ):
        self.sample_rate: int = sample_rate
        self.buffer_size: int = buffer_size
        self.input_device_index: int = input_device_index
        self.output_device_index: int = output_device_index
        self.py_audio: pyaudio.PyAudio = pyaudio.PyAudio()
        self.stream_in: pyaudio.Stream = None
        self.stream_out: pyaudio.Stream = None

    def list_devices(self):
        # PortAudio repeats each device once per host API; only default-host-API indices work with open().
        default_api = self.py_audio.get_default_host_api_info()
        print(f'=== Audio devices ({default_api["name"]}) ===')
        for i in range(self.py_audio.get_device_count()):
            info = self.py_audio.get_device_info_by_index(i)
            if info['hostApi'] != default_api['index']:
                continue
            kinds = '/'.join(
                k
                for k, ch in (('in', info['maxInputChannels']), ('out', info['maxOutputChannels']))
                if ch > 0
            )
            print(f'index: {i:3d}  [{kinds:6s}]  {info["name"]}')

    def support_output(self) -> bool:
        return self.stream_out is not None

    @property
    def exhausted(self) -> bool:
        return False

    def start_streams(self, start_stream_out: bool = False, start_stream_in: bool = True) -> None:
        if start_stream_in:
            self._start_stream_in()
        if start_stream_out:
            self._start_stream_out()

    def _start_stream_in(self) -> None:
        if self.input_device_index is None:
            default_input_device_info = self.py_audio.get_default_input_device_info()
            self.input_device_index = default_input_device_info['index']
            LOGGER.info(
                f"[pyaudio] using default sound input device: {default_input_device_info['name']}"
            )
        else:
            device_name = self.py_audio.get_device_info_by_index(self.input_device_index)["name"]
            LOGGER.info(
                f"[pyaudio] using sound input device: {device_name} (index: {self.input_device_index})"
            )

        self.stream_in = self.py_audio.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.sample_rate,
            input_device_index=self.input_device_index,
            input=True,
            frames_per_buffer=self.buffer_size,
        )
        self.stream_in.start_stream()

    def _start_stream_out(self) -> None:
        if self.output_device_index is None:
            default_output_device_info = self.py_audio.get_default_output_device_info()
            self.output_device_index = default_output_device_info['index']
            LOGGER.info(
                f"[pyaudio] using default sound output device: {default_output_device_info['name']}"
            )
        else:
            device_name = self.py_audio.get_device_info_by_index(self.output_device_index)["name"]
            LOGGER.info(
                f"[pyaudio] using sound output device: {device_name} (index: {self.output_device_index})"
            )
        self.stream_out = self.py_audio.open(
            format=pyaudio.paFloat32,
            channels=1,
            output_device_index=self.output_device_index,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=self.buffer_size,
        )
        self.stream_out.start_stream()

    def read(self) -> np.ndarray:
        assert self.stream_in is not None, "stream_in was None"
        try:
            audio_buffer = self.stream_in.read(self.buffer_size, exception_on_overflow=False)
        except OSError as error:
            if error.errno != pyaudio.paUnanticipatedHostError:
                raise
            raise RuntimeError(
                'Audio capture failed: the Windows input stream became unavailable. '
                'An RDP session change or device reconfiguration can cause this; '
                'restart Autolux after the session and audio device are stable.'
            ) from error
        return np.fromstring(audio_buffer, dtype=np.float32)

    def play(self, audio_buffer: np.ndarray) -> None:
        assert self.stream_out is not None, "stream_out was None"
        self.stream_out.write(audio_buffer.tobytes())

    def close(self) -> None:
        LOGGER.info('[pyaudio] closing pyaudio')
        with ExitStack() as cleanup:
            cleanup.callback(self.py_audio.terminate)
            for stream in (self.stream_out, self.stream_in):
                if stream is not None:
                    cleanup.callback(stream.close)
            self.stream_in = None
            self.stream_out = None
