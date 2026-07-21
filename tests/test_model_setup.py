from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import voice2text.model_setup as model_setup_module
from voice2text.main import main
from voice2text.model_settings import ManagedModel, ModelSettings, load_model_settings
from voice2text.model_setup import (
    ModelSetupError,
    ModelSetupResult,
    _stream_to_file,
    _UrllibDownloader,
    setup_managed_model,
)

_CONTENT = b"fake-whisper-model-bytes"
_CONTENT_SHA256 = hashlib.sha256(_CONTENT).hexdigest()


def _fake_managed_model() -> ManagedModel:
    return ManagedModel(
        model_id="test.en",
        display_name="Test model",
        file_name="ggml-test.en.bin",
        sha256=_CONTENT_SHA256,
        url="https://example.test/ggml-test.en.bin",
        approx_bytes=len(_CONTENT),
        description="A tiny fixture model used only in tests.",
    )


class _FakeDownloader:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.calls: list[str] = []

    def download(self, url: str, destination: Path, *, max_bytes: int) -> None:
        self.calls.append(url)
        destination.write_bytes(self._content)


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_setup_module, "managed_model", lambda _id: _fake_managed_model())


def test_download_verifies_checksum_records_settings_and_is_idempotent(
    fake_catalog: None,
    tmp_path: Path,
) -> None:
    models_dir = tmp_path / "models"
    settings_path = tmp_path / "model.json"
    downloader = _FakeDownloader(_CONTENT)

    result = setup_managed_model(
        "test.en",
        models_dir=models_dir,
        downloader=downloader,
        settings_path=settings_path,
    )

    installed = models_dir / "ggml-test.en.bin"
    assert result.action == "downloaded"
    assert result.settings.path == installed.resolve()
    assert result.settings.sha256 == _CONTENT_SHA256
    assert installed.read_bytes() == _CONTENT
    assert downloader.calls == ["https://example.test/ggml-test.en.bin"]

    loaded = load_model_settings(settings_path)
    assert loaded is not None and loaded.sha256 == _CONTENT_SHA256

    again = setup_managed_model(
        "test.en",
        models_dir=models_dir,
        downloader=downloader,
        settings_path=settings_path,
    )
    assert again.action == "reused"
    assert downloader.calls == ["https://example.test/ggml-test.en.bin"]


def test_download_checksum_mismatch_is_rejected_and_leaves_no_file(
    fake_catalog: None,
    tmp_path: Path,
) -> None:
    models_dir = tmp_path / "models"
    settings_path = tmp_path / "model.json"

    with pytest.raises(ModelSetupError, match="checksum"):
        setup_managed_model(
            "test.en",
            models_dir=models_dir,
            downloader=_FakeDownloader(b"corrupted-bytes"),
            settings_path=settings_path,
        )

    assert not (models_dir / "ggml-test.en.bin").exists()
    assert list(models_dir.glob("*")) == []
    assert load_model_settings(settings_path) is None


def test_register_source_file_verifies_checksum(fake_catalog: None, tmp_path: Path) -> None:
    source = tmp_path / "downloads" / "model.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(_CONTENT)
    models_dir = tmp_path / "models"

    result = setup_managed_model(
        "test.en",
        models_dir=models_dir,
        source_file=source,
        settings_path=tmp_path / "model.json",
    )

    assert result.action == "registered"
    assert (models_dir / "ggml-test.en.bin").read_bytes() == _CONTENT


def test_register_source_file_rejects_wrong_checksum(fake_catalog: None, tmp_path: Path) -> None:
    source = tmp_path / "downloads" / "model.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not-the-reviewed-model")
    models_dir = tmp_path / "models"

    with pytest.raises(ModelSetupError, match="does not match the reviewed checksum"):
        setup_managed_model(
            "test.en",
            models_dir=models_dir,
            source_file=source,
            settings_path=tmp_path / "model.json",
        )

    assert not (models_dir / "ggml-test.en.bin").exists()


def test_stream_enforces_the_size_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ModelSetupError, match="maximum allowed size"):
        _stream_to_file(_FakeResponse([b"x" * 10]), tmp_path / "out.bin", max_bytes=5)


def test_default_downloader_requires_https(tmp_path: Path) -> None:
    with pytest.raises(ModelSetupError, match="HTTPS"):
        _UrllibDownloader().download(
            "http://example.test/model.bin", tmp_path / "out.bin", max_bytes=10
        )


def test_cli_setup_model_defaults_to_base_en_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_setup(model_id: str, *, source_file: Path | None = None) -> ModelSetupResult:
        captured["model_id"] = model_id
        captured["source_file"] = source_file
        return ModelSetupResult(
            model=_fake_managed_model(),
            settings=ModelSettings(path=tmp_path / "ggml-test.en.bin", sha256=_CONTENT_SHA256),
            action="downloaded",
        )

    monkeypatch.setattr(model_setup_module, "setup_managed_model", fake_setup)

    assert main(["--setup-model"]) == 0
    out = capsys.readouterr().out
    assert "Downloaded and verified" in out
    assert captured["model_id"] == "base.en"
    assert captured["source_file"] is None


def test_cli_setup_model_passes_choice_and_source_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_setup(model_id: str, *, source_file: Path | None = None) -> ModelSetupResult:
        captured["model_id"] = model_id
        captured["source_file"] = source_file
        return ModelSetupResult(
            model=_fake_managed_model(),
            settings=ModelSettings(path=tmp_path / "ggml-test.en.bin", sha256=_CONTENT_SHA256),
            action="registered",
        )

    monkeypatch.setattr(model_setup_module, "setup_managed_model", fake_setup)
    source = tmp_path / "byo.bin"
    source.write_bytes(_CONTENT)

    assert main(["--setup-model", "test.en", "--model-file", str(source)]) == 0
    assert captured["model_id"] == "test.en"
    assert captured["source_file"] == source


def test_cli_setup_model_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_setup(_model_id: str, *, source_file: Path | None = None) -> ModelSetupResult:
        raise ModelSetupError("model download failed")

    monkeypatch.setattr(model_setup_module, "setup_managed_model", fake_setup)

    assert main(["--setup-model"]) == 2


def test_cli_lists_reviewed_models(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list-models"]) == 0
    listing = capsys.readouterr().out
    assert "base.en" in listing
    assert "(default)" in listing
