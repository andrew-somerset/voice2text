from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from voice2text.recorder import Recorder, RecordingError, _block_size, _resample_audio


class FakeStream:
    def __init__(self, callback: Callable[..., None]) -> None:
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def emit(self, values: list[float]) -> None:
        data = np.asarray(values, dtype=np.float32).reshape(-1, 1)
        self.callback(data, len(values), None, None)


class FakeFactory:
    def __init__(self) -> None:
        self.stream: FakeStream | None = None

    def __call__(self, callback: Callable[..., Any]) -> FakeStream:
        self.stream = FakeStream(callback)
        return self.stream


def test_recorder_concatenates_memory_only_chunks() -> None:
    factory = FakeFactory()
    recorder = Recorder(stream_factory=factory)

    recorder.start()
    assert factory.stream is not None
    factory.stream.emit([0.1, 0.2])
    factory.stream.emit([0.3])
    audio = recorder.stop()

    np.testing.assert_allclose(audio, np.asarray([0.1, 0.2, 0.3], dtype=np.float32))
    assert audio.dtype == np.float32
    assert recorder.is_recording is False


def test_cancel_discards_audio_and_next_recording_is_fresh() -> None:
    factory = FakeFactory()
    recorder = Recorder(stream_factory=factory)

    recorder.start()
    assert factory.stream is not None
    factory.stream.emit([0.8])
    recorder.cancel()
    factory.stream.emit([0.9])

    recorder.start()
    factory.stream.emit([0.2])
    audio = recorder.stop()

    np.testing.assert_array_equal(audio, np.asarray([0.2], dtype=np.float32))


def test_callback_ignores_frames_when_not_recording() -> None:
    factory = FakeFactory()
    recorder = Recorder(stream_factory=factory)
    recorder.open()
    assert factory.stream is not None

    factory.stream.emit([0.7])
    recorder.start()
    audio = recorder.stop()

    assert audio.size == 0


def test_duplicate_start_and_stop_are_rejected() -> None:
    recorder = Recorder(stream_factory=FakeFactory())

    recorder.start()
    with pytest.raises(RecordingError, match="already active"):
        recorder.start()
    recorder.cancel()
    with pytest.raises(RecordingError, match="no recording"):
        recorder.stop()


def test_close_releases_stream_and_discards_active_audio() -> None:
    factory = FakeFactory()
    recorder = Recorder(stream_factory=factory)
    recorder.start()
    assert factory.stream is not None
    factory.stream.emit([0.4])

    recorder.close()

    assert factory.stream.closed is True
    assert recorder.is_recording is False


def test_native_windows_rate_is_resampled_to_whisper_rate() -> None:
    input_rate = 48_000
    output_rate = 16_000
    duration_seconds = 0.1
    time_axis = np.arange(round(input_rate * duration_seconds), dtype=np.float32) / input_rate
    source = np.sin(2 * np.pi * 440 * time_axis).astype(np.float32)

    result = _resample_audio(source, input_rate, output_rate)

    assert result.dtype == np.float32
    assert result.size == round(output_rate * duration_seconds)
    assert np.max(np.abs(result)) <= 1.0


def test_native_windows_rate_keeps_requested_callback_duration() -> None:
    assert _block_size(48_000, 20) == 960
    assert _block_size(16_000, 20) == 320
