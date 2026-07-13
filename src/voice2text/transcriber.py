"""pywhispercpp wrapper: model loaded once at startup, warmed up, junk rejected.

The heavy import (pywhispercpp) happens lazily inside ``Transcriber.__init__``
so the pure helpers here (``too_short``, ``clean_text``, ``load_wav``) work on
machines without pywhispercpp installed (e.g. Linux CI).
"""

from __future__ import annotations

import inspect
import logging
import string
import time
import wave
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from voice2text import config

logger = logging.getLogger(__name__)

# whisper.cpp misbehaves on sub-1s input; pad shorter utterances with trailing
# silence up to this length before inference.
_MIN_INFERENCE_SECONDS: float = 1.1

# A transcript containing nothing but these characters is a hallucination
# artifact (whisper emits "...", "…" etc. on silence) and gets discarded.
_JUNK_CHARS: frozenset[str] = frozenset(string.punctuation + string.whitespace + "…")


def too_short(audio: np.ndarray, samplerate: int = config.SAMPLE_RATE) -> bool:
    """True if the utterance is below the accidental-tap threshold.

    Whisper hallucinates on near-empty audio ("Thank you." on silence is the
    classic failure), so callers must drop these instead of transcribing.
    """
    return audio.size < config.MIN_UTTERANCE_SECONDS * samplerate


def clean_text(raw: str) -> str:
    """Strip whitespace; return "" if only punctuation/whitespace remains."""
    text = raw.strip()
    if not text or all(ch in _JUNK_CHARS for ch in text):
        return ""
    return text


# Characters stripped from a token's edges before comparing it to a filler word,
# so "Um," / "uh." / "um…" all match.
_TOKEN_EDGE = string.punctuation + string.whitespace + "…—"


def remove_fillers(text: str, fillers: Iterable[str] = config.FILLER_WORDS) -> str:
    """Drop spoken filler words ("um", "uh", ...) as whole tokens.

    A token matches if it equals a filler once its surrounding punctuation is
    stripped, so the token's trailing comma/period goes with it (no stray
    punctuation left behind). The result is re-capitalized and tidied.
    """
    fset = {f.lower() for f in fillers}
    if not text or not fset:
        return text
    kept = [tok for tok in text.split() if tok.strip(_TOKEN_EDGE).lower() not in fset]
    out = " ".join(kept).strip()
    out = out.lstrip(_TOKEN_EDGE).strip()  # drop punctuation orphaned at the start
    if out:
        out = out[0].upper() + out[1:]
    return out


def load_wav(path: str | Path) -> np.ndarray:
    """Load a 16kHz 16-bit PCM wav as mono float32 in [-1, 1].

    This is whisper's exact input contract — files at any other sample rate are
    rejected rather than resampled.
    """
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != config.SAMPLE_RATE:
            raise ValueError(
                f"{path}: sample rate is {wf.getframerate()}Hz but whisper needs "
                f"{config.SAMPLE_RATE}Hz — re-record or resample the file first"
            )
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got {wf.getsampwidth() * 8}-bit")
        frames = wf.readframes(wf.getnframes())
        channels = wf.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


class Transcriber:
    """whisper.cpp transcription with the model loaded once and kept resident.

    Per-utterance model loading costs 1-2s and destroys the latency budget, so
    construction is expensive (load + Metal warmup) and ``transcribe`` is cheap.
    """

    def __init__(
        self,
        model_name: str = config.MODEL_NAME,
        remove_fillers_enabled: bool = config.REMOVE_FILLERS,
    ) -> None:
        self._remove_fillers = remove_fillers_enabled
        # Lazy import: module import must succeed without pywhispercpp.
        from pywhispercpp.model import Model

        kwargs: dict[str, Any] = {
            "language": config.LANGUAGE,
            "n_threads": config.performance_core_count(),
            "print_realtime": False,
            "print_progress": False,
        }
        # Silence whisper.cpp's C++ log chatter where the binding supports it;
        # older pywhispercpp versions lack the parameter.
        if "redirect_whispercpp_logs_to" in inspect.signature(Model.__init__).parameters:
            kwargs["redirect_whispercpp_logs_to"] = None

        start = time.perf_counter()
        try:
            self._model = Model(model_name, **kwargs)
        except TypeError:
            # Defensive fallback if the signature check was fooled (e.g. the
            # binding routes unknown kwargs into whisper params).
            kwargs.pop("redirect_whispercpp_logs_to", None)
            self._model = Model(model_name, **kwargs)
        loaded = time.perf_counter()

        # First Metal inference pays ~1s of kernel compilation — eat that cost
        # now, on 1s of silence, not on the user's first real utterance.
        self._model.transcribe(np.zeros(config.SAMPLE_RATE, dtype=np.float32))
        warmed = time.perf_counter()

        logger.info(
            "model %s loaded in %.0f ms, warmup inference %.0f ms",
            model_name,
            (loaded - start) * 1000,
            (warmed - loaded) * 1000,
        )

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe 16kHz mono float32 audio in [-1, 1].

        Returns "" for utterances that are too short or transcribe to junk.
        """
        if too_short(audio):
            logger.debug(
                "dropped %.2fs utterance (below %.2fs threshold)",
                audio.size / config.SAMPLE_RATE,
                config.MIN_UTTERANCE_SECONDS,
            )
            return ""

        min_samples = int(_MIN_INFERENCE_SECONDS * config.SAMPLE_RATE)
        if audio.size < min_samples:
            audio = np.pad(audio, (0, min_samples - audio.size))

        start = time.perf_counter()
        segments = self._model.transcribe(audio)
        elapsed_ms = (time.perf_counter() - start) * 1000
        text = " ".join(segment.text.strip() for segment in segments)
        if self._remove_fillers:
            text = remove_fillers(text)
        text = clean_text(text)
        logger.debug(
            "transcribed %.2fs of audio in %.0f ms: %r",
            audio.size / config.SAMPLE_RATE,
            elapsed_ms,
            text,
        )
        return text


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) != 2:
        print("usage: python -m voice2text.transcriber <path/to/16khz-mono.wav>", file=sys.stderr)
        raise SystemExit(2)

    try:
        wav_audio = load_wav(sys.argv[1])
    except (OSError, ValueError, wave.Error, EOFError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"loading model {config.MODEL_NAME!r} (load + warmup, see log line)...")
    transcriber = Transcriber()

    t0 = time.perf_counter()
    transcript = transcriber.transcribe(wav_audio)
    inference_ms = (time.perf_counter() - t0) * 1000

    duration = wav_audio.size / config.SAMPLE_RATE
    print(f"transcript: {transcript!r}")
    print(f"inference: {inference_ms:.0f} ms for {duration:.2f}s of audio")
