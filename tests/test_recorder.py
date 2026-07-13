"""CI-safe tests for Recorder buffer logic.

sounddevice is stubbed out before import so these tests never touch PortAudio
(and never import-fail on Linux CI). Buffer behaviour is exercised by calling
_callback directly; no stream is ever opened.
"""

import sys
from unittest.mock import MagicMock

import numpy as np

sys.modules.setdefault("sounddevice", MagicMock())

from voice2text.recorder import Recorder  # noqa: E402


def _chunk(values: list[float]) -> np.ndarray:
    """Build a (N, 1) float32 block, as sounddevice delivers to the callback."""
    return np.asarray(values, dtype=np.float32).reshape(-1, 1)


def test_frames_discarded_when_not_recording() -> None:
    recorder = Recorder()
    recorder._callback(_chunk([0.1, 0.2]), 2, None, None)
    audio = recorder.take()
    assert audio.size == 0
    assert audio.dtype == np.float32


def test_take_returns_concatenation_in_order() -> None:
    recorder = Recorder()
    recorder.start()
    recorder._callback(_chunk([0.1, 0.2]), 2, None, None)
    recorder._callback(_chunk([0.3]), 1, None, None)
    recorder.stop()
    audio = recorder.take()
    assert audio.ndim == 1
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, np.asarray([0.1, 0.2, 0.3], dtype=np.float32))


def test_take_clears_buffer() -> None:
    recorder = Recorder()
    recorder.start()
    recorder._callback(_chunk([0.5]), 1, None, None)
    recorder.stop()
    assert recorder.take().size == 1
    second = recorder.take()
    assert second.size == 0
    assert second.dtype == np.float32


def test_take_with_nothing_recorded_returns_empty_float32() -> None:
    audio = Recorder().take()
    assert isinstance(audio, np.ndarray)
    assert audio.ndim == 1
    assert audio.size == 0
    assert audio.dtype == np.float32


def test_stop_prevents_further_capture() -> None:
    recorder = Recorder()
    recorder.start()
    recorder._callback(_chunk([0.1]), 1, None, None)
    recorder.stop()
    recorder._callback(_chunk([0.9]), 1, None, None)
    np.testing.assert_allclose(recorder.take(), np.asarray([0.1], dtype=np.float32))


def test_start_clears_stale_buffer() -> None:
    recorder = Recorder()
    recorder.start()
    recorder._callback(_chunk([0.1]), 1, None, None)
    recorder.stop()
    recorder.start()
    recorder._callback(_chunk([0.2]), 1, None, None)
    recorder.stop()
    np.testing.assert_allclose(recorder.take(), np.asarray([0.2], dtype=np.float32))
