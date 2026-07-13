"""Microphone capture into a thread-safe buffer.

The sounddevice stream is opened once and stays open for the app's lifetime —
opening a stream on key-down costs 100-300ms and clips the first word.
Recording toggles a flag; frames arriving while the flag is clear are discarded.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

from voice2text import config

logger = logging.getLogger(__name__)


class Recorder:
    """Always-open mic stream with flag-gated buffering.

    start()/stop() are near-instant (called from the event-tap callback path);
    take() is called from the worker thread; _callback runs on sounddevice's
    own audio thread.
    """

    def __init__(
        self,
        samplerate: int = config.SAMPLE_RATE,
        channels: int = config.CHANNELS,
    ) -> None:
        self._samplerate = samplerate
        self._channels = channels
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._recording = False
        # Most recent input peak amplitude (0..1); read by the UI for a live
        # waveform. Plain float assignment is atomic in CPython, so no lock.
        self._level = 0.0

    def open(self) -> None:
        """Create and start the permanent input stream. Idempotent."""
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            samplerate=self._samplerate,
            channels=self._channels,
            dtype=config.DTYPE,
            callback=self._callback,
        )
        self._stream.start()
        logger.info("audio stream open (%d Hz, %d ch)", self._samplerate, self._channels)

    def start(self) -> None:
        """Clear the buffer and begin capturing. Near-instant."""
        with self._lock:
            self._chunks.clear()
            self._recording = True
        self._level = 0.0

    def stop(self) -> None:
        """Stop capturing (buffer is kept for take()). Near-instant."""
        self._recording = False
        self._level = 0.0

    def level(self) -> float:
        """Most recent input peak amplitude (0..1) while recording, else 0."""
        return self._level

    def take(self) -> np.ndarray:
        """Return everything buffered as one 1-D float32 array and clear the buffer."""
        with self._lock:
            chunks = self._chunks
            self._chunks = []
        if not chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32, copy=False)

    def close(self) -> None:
        """Stop and close the stream. Idempotent."""
        self._recording = False
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
        logger.info("audio stream closed")

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags | None,
    ) -> None:
        """sounddevice callback — runs on the audio thread."""
        if status:
            logger.warning("audio stream status: %s", status)
        if not self._recording:
            return
        chunk = indata[:, 0].copy()
        # Cheap level meter for the live waveform UI (peak of this block).
        self._level = float(np.abs(chunk).max()) if chunk.size else 0.0
        with self._lock:
            if self._recording:
                self._chunks.append(chunk)


if __name__ == "__main__":
    import sys
    import time
    import wave

    logging.basicConfig(level=logging.INFO)
    out_path = sys.argv[1] if len(sys.argv) > 1 else "recording.wav"

    recorder = Recorder()
    recorder.open()
    print("Recording 3 seconds...")
    recorder.start()
    time.sleep(3)
    recorder.stop()
    audio = recorder.take()

    duration = audio.size / config.SAMPLE_RATE
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    print(f"Captured {duration:.2f}s, peak amplitude {peak:.3f}")

    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(out_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit PCM
        wav_file.setframerate(config.SAMPLE_RATE)
        wav_file.writeframes(pcm.tobytes())

    recorder.close()
    print(f"Saved to {out_path}")
    print(f"play it back with: afplay {out_path} — verify the first word is not clipped.")
