"""Resident local whisper.cpp model with strict input and model validation."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import re
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from voice2text.config import AudioConfig, TranscriberConfig

LOGGER = logging.getLogger(__name__)
FloatAudio = NDArray[np.float32]
_ONLY_PUNCTUATION = re.compile(r"^[\W_]+$", re.UNICODE)


class TranscriberError(RuntimeError):
    """Raised when local transcription cannot run safely."""


class Segment(Protocol):
    text: str


class WhisperModel(Protocol):
    def transcribe(self, media: NDArray[Any], **params: Any) -> list[Segment]: ...


ModelFactory = Callable[[str, int, str], WhisperModel]


class Transcriber:
    """Load one checksum-verified model and serialize local inference calls."""

    def __init__(
        self,
        config: TranscriberConfig,
        audio_config: AudioConfig | None = None,
        *,
        model_factory: ModelFactory | None = None,
        warm_up: bool = True,
    ) -> None:
        self._config = config
        self._audio_config = audio_config or AudioConfig()
        model_path = self._validate_model()
        factory = model_factory or _create_pywhispercpp_model
        self._model = factory(str(model_path), config.n_threads, config.language)
        self._lock = threading.Lock()
        if warm_up:
            self.warm_up()

    def warm_up(self) -> None:
        """Pay native allocation and initialization costs before first dictation."""

        silence = np.zeros(self._audio_config.sample_rate, dtype=np.float32)
        with self._lock:
            self._model.transcribe(silence)
        silence.fill(0)

    def transcribe(self, audio: FloatAudio) -> str:
        """Transcribe valid 16 kHz mono float32 audio and normalize segment text."""

        self._validate_audio(audio)
        minimum_frames = round(self._config.min_utterance_seconds * self._audio_config.sample_rate)
        if audio.size < minimum_frames:
            return ""

        with self._lock:
            segments = self._model.transcribe(audio)
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
        normalized = " ".join(text.split())
        if not normalized or _ONLY_PUNCTUATION.fullmatch(normalized):
            return ""
        return normalized

    def _validate_model(self) -> Path:
        model_path = self._config.model_path
        expected_sha256 = self._config.model_sha256
        if model_path is None:
            raise TranscriberError(
                "VOICE2TEXT_MODEL_PATH must point to a locally managed whisper.cpp model"
            )
        if not model_path.is_file():
            raise TranscriberError(f"Whisper model does not exist: {model_path}")
        if expected_sha256 is None:
            raise TranscriberError(
                "VOICE2TEXT_MODEL_SHA256 is required; production models are never "
                "downloaded at runtime"
            )
        actual_sha256 = sha256_file(model_path)
        if not hmac.compare_digest(actual_sha256.lower(), expected_sha256.lower()):
            raise TranscriberError("Whisper model SHA-256 checksum does not match configuration")
        return model_path

    def _validate_audio(self, audio: FloatAudio) -> None:
        if not isinstance(audio, np.ndarray):
            raise TypeError("audio must be a numpy array")
        if audio.ndim != 1:
            raise TranscriberError("audio must be a one-dimensional mono array")
        if audio.dtype != np.float32:
            raise TranscriberError("audio must use float32 samples")
        if not np.all(np.isfinite(audio)):
            raise TranscriberError("audio contains non-finite samples")
        if audio.size and (float(audio.min()) < -1.0 or float(audio.max()) > 1.0):
            raise TranscriberError("audio samples must be in the range [-1, 1]")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Calculate a model checksum without loading the full model into Python memory."""

    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        while chunk := model_file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _create_pywhispercpp_model(path: str, n_threads: int, language: str) -> WhisperModel:
    try:
        from pywhispercpp.model import Model
    except ImportError as exc:  # pragma: no cover - dependency installation failure
        raise TranscriberError("pywhispercpp is not installed") from exc

    LOGGER.info("Loading checksum-verified local Whisper model")
    return Model(
        path,
        n_threads=n_threads,
        language=language,
        print_progress=False,
        print_realtime=False,
        print_timestamps=False,
        redirect_whispercpp_logs_to=False,
    )


def _load_manual_test_wav(path: Path) -> FloatAudio:
    """Load only the exact PCM format used by the explicit manual fixture command."""

    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getframerate() != 16_000:
            raise TranscriberError("manual test WAV must use a 16 kHz sample rate")
        if wav_file.getnchannels() != 1:
            raise TranscriberError("manual test WAV must be mono")
        if wav_file.getsampwidth() != 2:
            raise TranscriberError("manual test WAV must use 16-bit PCM samples")
        frames = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def main(argv: list[str] | None = None) -> int:
    """Transcribe an explicit WAV fixture with an explicit managed model."""

    parser = argparse.ArgumentParser(description="Test local Whisper transcription")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)

    config = TranscriberConfig(
        model_path=args.model.resolve(),
        model_sha256=args.sha256,
        n_threads=args.threads,
    )
    transcriber = Transcriber(config)
    # Runtime audio never touches disk; this loader exists only for an explicit fixture command.
    audio = _load_manual_test_wav(args.audio)
    text = transcriber.transcribe(audio)
    print(text)
    audio.fill(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
