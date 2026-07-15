"""In-memory microphone capture through Windows WASAPI shared mode."""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from voice2text.config import AudioConfig

LOGGER = logging.getLogger(__name__)
FloatAudio = NDArray[np.float32]
AudioCallback = Callable[[NDArray[Any], int, Any, Any], None]


class RecordingError(RuntimeError):
    """Raised when the microphone cannot satisfy the local Whisper contract."""


class InputStream(Protocol):
    """Minimal PortAudio stream surface used by `Recorder`."""

    def start(self) -> Any: ...

    def stop(self) -> Any: ...

    def close(self) -> Any: ...


StreamFactory = Callable[[AudioCallback], InputStream]
Resampler = Callable[[FloatAudio, int, int], FloatAudio]


class Recorder:
    """Capture mono float32 chunks only while an explicit recording is active."""

    def __init__(
        self,
        config: AudioConfig | None = None,
        *,
        stream_factory: StreamFactory | None = None,
        capture_sample_rate: int | None = None,
        resampler: Resampler | None = None,
    ) -> None:
        self._config = config or AudioConfig()
        self._stream_factory = stream_factory or self._create_wasapi_stream
        self._capture_sample_rate = capture_sample_rate or self._config.sample_rate
        self._resampler = resampler or _resample_audio
        self._stream: InputStream | None = None
        self._chunks: list[FloatAudio] = []
        self._lock = threading.Lock()
        self._recording = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def open(self) -> None:
        """Open and configure the stream without activating microphone capture."""

        if self._stream is None:
            self._stream = self._stream_factory(self._audio_callback)

    def start(self) -> None:
        """Clear stale data and activate microphone capture."""

        self.open()
        with self._lock:
            if self._recording:
                raise RecordingError("a recording is already active")
            self._chunks.clear()
            self._recording = True

        try:
            self._require_stream().start()
        except Exception as exc:
            with self._lock:
                self._recording = False
                self._chunks.clear()
            raise RecordingError(
                "Could not start the microphone. Check Settings > Privacy & security > Microphone "
                "and enable microphone access for desktop apps."
            ) from exc

    def stop(self) -> FloatAudio:
        """Stop capture and return one contiguous, memory-only audio array."""

        with self._lock:
            if not self._recording:
                raise RecordingError("no recording is active")

        try:
            self._require_stream().stop()
        except Exception as exc:
            with self._lock:
                self._recording = False
                self._chunks.clear()
            raise RecordingError("Could not stop the microphone stream cleanly") from exc

        with self._lock:
            self._recording = False
            chunks = self._chunks
            self._chunks = []

        if not chunks:
            return np.empty(0, dtype=np.float32)
        audio = np.concatenate(chunks).astype(np.float32, copy=False)
        if self._capture_sample_rate == self._config.sample_rate:
            return audio
        try:
            resampled = self._resampler(
                audio,
                self._capture_sample_rate,
                self._config.sample_rate,
            )
        finally:
            audio.fill(0)
        return np.asarray(resampled, dtype=np.float32)

    def cancel(self) -> None:
        """Stop capture and discard all buffered audio."""

        with self._lock:
            if not self._recording:
                self._chunks.clear()
                return
        try:
            self._require_stream().stop()
        finally:
            with self._lock:
                self._recording = False
                self._chunks.clear()

    def close(self) -> None:
        """Release PortAudio resources and drop any audio references."""

        if self.is_recording:
            self.cancel()
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        with self._lock:
            self._chunks.clear()

    def __enter__(self) -> Recorder:
        self.open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _audio_callback(
        self,
        input_data: NDArray[Any],
        _frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        if status:
            LOGGER.warning("Microphone callback reported a capture status warning")
        with self._lock:
            if not self._recording:
                return
            if input_data.ndim != 2 or input_data.shape[1] != self._config.channels:
                LOGGER.error("Microphone callback returned an unexpected channel layout")
                return
            self._chunks.append(np.asarray(input_data[:, 0], dtype=np.float32).copy())

    def _create_wasapi_stream(self, callback: AudioCallback) -> InputStream:
        try:
            import sounddevice as sounddevice
        except ImportError as exc:  # pragma: no cover - dependency installation failure
            raise RecordingError("sounddevice is not installed") from exc

        host_apis = sounddevice.query_hostapis()
        wasapi = next(
            (item for item in host_apis if item.get("name") == "Windows WASAPI"),
            None,
        )
        if wasapi is None or int(wasapi.get("default_input_device", -1)) < 0:
            raise RecordingError("No Windows WASAPI default input device is available")
        device = int(wasapi["default_input_device"])
        device_info = sounddevice.query_devices(device)
        self._capture_sample_rate = round(float(device_info["default_samplerate"]))

        try:
            sounddevice.check_input_settings(
                device=device,
                channels=self._config.channels,
                dtype=self._config.dtype,
                samplerate=self._capture_sample_rate,
            )
        except Exception as exc:
            raise RecordingError(
                "The default WASAPI microphone does not support mono float32 capture at its "
                "shared-mode sample rate. Select a compatible input device."
            ) from exc

        return sounddevice.InputStream(
            device=device,
            samplerate=self._capture_sample_rate,
            channels=self._config.channels,
            dtype=self._config.dtype,
            blocksize=_block_size(self._capture_sample_rate, self._config.block_duration_ms),
            callback=callback,
            extra_settings=sounddevice.WasapiSettings(exclusive=False),
        )

    def _require_stream(self) -> InputStream:
        if self._stream is None:
            raise RecordingError("microphone stream is not open")
        return self._stream


def _resample_audio(audio: FloatAudio, input_rate: int, output_rate: int) -> FloatAudio:
    """Convert native WASAPI audio to Whisper's 16 kHz contract entirely on-device."""

    if input_rate <= 0 or output_rate <= 0:
        raise RecordingError("sample rates must be positive")
    try:
        import soxr
    except ImportError as exc:  # pragma: no cover - dependency installation failure
        raise RecordingError("soxr is required when the microphone is not natively 16 kHz") from exc
    result = np.asarray(
        soxr.resample(audio, input_rate, output_rate, quality="HQ"),
        dtype=np.float32,
    )
    np.clip(result, -1.0, 1.0, out=result)
    return result


def _block_size(sample_rate: int, duration_ms: int) -> int:
    """Keep callback duration stable when WASAPI captures above 16 kHz."""

    return sample_rate * duration_ms // 1_000


def main(argv: list[str] | None = None) -> int:
    """Capture a short memory-only sample and print non-content diagnostics."""

    parser = argparse.ArgumentParser(description="Test Windows WASAPI microphone capture")
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    if not 0.1 <= args.seconds <= 30.0:
        parser.error("--seconds must be between 0.1 and 30")

    print(f"Recording for {args.seconds:g} seconds; audio remains in memory only.")
    with Recorder() as recorder:
        started_ns = time.monotonic_ns()
        recorder.start()
        time.sleep(args.seconds)
        audio = recorder.stop()
        elapsed_ms = (time.monotonic_ns() - started_ns) / 1_000_000
    duration = audio.size / 16_000
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    print(f"Captured {duration:.3f}s in {elapsed_ms:.1f}ms; peak={peak:.3f}")
    audio.fill(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
