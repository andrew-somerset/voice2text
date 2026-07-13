"""Tests for voice2text.transcriber.

The pure helpers (too_short, clean_text) run anywhere, including CI without
pywhispercpp installed — transcriber.py imports pywhispercpp lazily inside
Transcriber.__init__. The integration tests additionally need a downloaded
whisper model and skip cleanly when it (or pywhispercpp) is missing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from voice2text import config
from voice2text.transcriber import (
    Transcriber,
    clean_text,
    load_wav,
    remove_fillers,
    too_short,
)

FIXTURE_WAV = Path(__file__).parent / "fixtures" / "hello.wav"


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * config.SAMPLE_RATE), dtype=np.float32)


class TestTooShort:
    def test_rejects_below_threshold(self) -> None:
        assert too_short(_silence(0.2))

    def test_accepts_above_threshold(self) -> None:
        assert not too_short(_silence(0.5))

    def test_rejects_empty(self) -> None:
        assert too_short(np.zeros(0, dtype=np.float32))

    def test_uses_samplerate(self) -> None:
        # 0.3s worth of 16kHz samples is only 0.1s of audio at 48kHz.
        assert too_short(_silence(0.3), samplerate=48_000)


class TestCleanText:
    def test_strips_whitespace(self) -> None:
        assert clean_text("  hello  ") == "hello"

    def test_dots_only_is_junk(self) -> None:
        assert clean_text("...") == ""

    def test_unicode_ellipsis_is_junk(self) -> None:
        assert clean_text("…") == ""

    def test_punctuation_only_is_junk(self) -> None:
        assert clean_text("!?") == ""

    def test_empty_string(self) -> None:
        assert clean_text("") == ""

    def test_whitespace_only(self) -> None:
        assert clean_text(" \n\t ") == ""

    def test_sentence_preserved(self) -> None:
        assert clean_text(" Hello world, this is a test. ") == "Hello world, this is a test."


class TestRemoveFillers:
    def test_removes_leading_filler_with_comma(self) -> None:
        assert remove_fillers("Um, I think so.") == "I think so."

    def test_removes_midsentence_filler(self) -> None:
        assert remove_fillers("I was, uh, going home.") == "I was, going home."

    def test_removes_multiple_and_variants(self) -> None:
        assert remove_fillers("Uh um so erm yeah") == "So yeah"

    def test_case_insensitive(self) -> None:
        assert remove_fillers("UH what") == "What"

    def test_filler_only_becomes_empty(self) -> None:
        assert remove_fillers("Um, uh...") == ""

    def test_does_not_touch_real_words(self) -> None:
        # "summer" contains "um" but is not a whole-word filler.
        assert remove_fillers("Summer is here") == "Summer is here"

    def test_preserves_non_filler_sentence(self) -> None:
        assert remove_fillers("Hello world, this is a test.") == "Hello world, this is a test."

    def test_empty_string(self) -> None:
        assert remove_fillers("") == ""


@pytest.fixture(scope="module")
def transcriber() -> Transcriber:
    """A loaded Transcriber; skips if pywhispercpp or the model is unavailable."""
    pytest.importorskip("pywhispercpp")
    try:
        from pywhispercpp.constants import MODELS_DIR
    except ImportError:
        pytest.skip("pywhispercpp.constants.MODELS_DIR unavailable")
    model_file = Path(MODELS_DIR).expanduser() / f"ggml-{config.MODEL_NAME}.bin"
    if not model_file.exists():
        pytest.skip("whisper model not downloaded")
    return Transcriber()


def test_transcribes_fixture_wav(transcriber: Transcriber) -> None:
    if not FIXTURE_WAV.exists():
        pytest.skip("tests/fixtures/hello.wav not present")
    audio = load_wav(FIXTURE_WAV)
    text = transcriber.transcribe(audio)
    assert "hello" in text.lower()


def test_short_utterance_returns_empty(transcriber: Transcriber) -> None:
    assert transcriber.transcribe(_silence(0.2)) == ""
