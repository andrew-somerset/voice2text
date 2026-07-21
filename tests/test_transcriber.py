from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from voice2text.config import TranscriberConfig
from voice2text.transcriber import Transcriber, TranscriberError, sha256_file


@dataclass
class FakeSegment:
    text: str


class FakeModel:
    def __init__(self, responses: list[list[FakeSegment]] | None = None) -> None:
        self.responses = responses or [[]]
        self.calls: list[np.ndarray] = []

    def transcribe(self, media: np.ndarray, **_params: object) -> list[FakeSegment]:
        self.calls.append(media.copy())
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def model_config(tmp_path: Path) -> TranscriberConfig:
    path = tmp_path / "model.bin"
    path.write_bytes(b"managed-model-fixture")
    return TranscriberConfig(
        model_path=path,
        model_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        min_utterance_seconds=0.3,
    )


def test_checksum_is_streamed_and_verified(tmp_path: Path) -> None:
    config = model_config(tmp_path)

    assert config.model_path is not None
    assert sha256_file(config.model_path) == config.model_sha256


def test_model_is_warmed_once_and_segments_are_normalized(tmp_path: Path) -> None:
    fake = FakeModel(
        responses=[
            [],
            [FakeSegment("  hello "), FakeSegment("  GM team.  ")],
        ]
    )
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: fake,
    )

    result = transcriber.transcribe(np.zeros(8_000, dtype=np.float32))

    assert result == "hello GM team."
    assert len(fake.calls) == 2
    assert fake.calls[0].size == 16_000


def test_short_audio_is_rejected_without_inference(tmp_path: Path) -> None:
    fake = FakeModel()
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: fake,
        warm_up=False,
    )

    assert transcriber.transcribe(np.zeros(4_799, dtype=np.float32)) == ""
    assert fake.calls == []


def test_punctuation_only_result_is_discarded(tmp_path: Path) -> None:
    fake = FakeModel(responses=[[FakeSegment(" ... !!! ")]])
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: fake,
        warm_up=False,
    )

    assert transcriber.transcribe(np.zeros(4_800, dtype=np.float32)) == ""


@pytest.mark.parametrize(
    "annotation",
    [
        "[BLANK_AUDIO]",
        "  [BLANK_AUDIO]  ",
        "[ Silence ]",
        "[Music]",
        "*coughing*",
        "♪♪♪",
        "[BLANK_AUDIO].",
    ],
)
def test_non_speech_annotation_only_result_is_discarded(tmp_path: Path, annotation: str) -> None:
    fake = FakeModel(responses=[[FakeSegment(annotation)]])
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: fake,
        warm_up=False,
    )

    assert transcriber.transcribe(np.zeros(4_800, dtype=np.float32)) == ""


def test_non_speech_annotation_is_stripped_from_surrounding_speech(tmp_path: Path) -> None:
    fake = FakeModel(responses=[[FakeSegment("hello [BLANK_AUDIO] team")]])
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: fake,
        warm_up=False,
    )

    assert transcriber.transcribe(np.zeros(4_800, dtype=np.float32)) == "hello team"


def test_dictated_parentheses_are_preserved(tmp_path: Path) -> None:
    fake = FakeModel(responses=[[FakeSegment("call me (later) today")]])
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: fake,
        warm_up=False,
    )

    assert transcriber.transcribe(np.zeros(4_800, dtype=np.float32)) == "call me (later) today"


def test_invalid_audio_contract_is_rejected(tmp_path: Path) -> None:
    transcriber = Transcriber(
        model_config(tmp_path),
        model_factory=lambda *_args: FakeModel(),
        warm_up=False,
    )

    with pytest.raises(TranscriberError, match="float32"):
        transcriber.transcribe(np.zeros(5_000, dtype=np.float64))  # type: ignore[arg-type]
    with pytest.raises(TranscriberError, match="range"):
        transcriber.transcribe(np.full(5_000, 1.1, dtype=np.float32))
    with pytest.raises(TranscriberError, match="one-dimensional"):
        transcriber.transcribe(np.zeros((5_000, 1), dtype=np.float32))  # type: ignore[arg-type]


def test_missing_or_mismatched_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TranscriberError, match="MODEL_PATH"):
        Transcriber(
            TranscriberConfig(),
            model_factory=lambda *_args: FakeModel(),
            warm_up=False,
        )

    path = tmp_path / "model.bin"
    path.write_bytes(b"unexpected")
    with pytest.raises(TranscriberError, match="does not match"):
        Transcriber(
            TranscriberConfig(model_path=path, model_sha256="0" * 64),
            model_factory=lambda *_args: FakeModel(),
            warm_up=False,
        )
