from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from voice2text.config import AppConfig
from voice2text.model_settings import (
    DEFAULT_MODEL_ID,
    ModelSettingsError,
    load_model_settings,
    managed_model,
    managed_models,
    save_model_settings,
)


def test_catalog_has_pinned_verified_default() -> None:
    ids = {model.model_id for model in managed_models()}
    assert DEFAULT_MODEL_ID in ids

    default = managed_model(DEFAULT_MODEL_ID)
    assert default.file_name == "ggml-base.en.bin"
    assert default.url.startswith("https://")
    assert len(default.sha256) == 64


def test_unknown_model_choice_is_rejected() -> None:
    with pytest.raises(ModelSettingsError, match="unsupported model choice"):
        managed_model("does-not-exist")


def test_settings_round_trip_is_small_atomic_and_non_secret(tmp_path: Path) -> None:
    path = tmp_path / "voice2text" / "model.json"
    model_path = tmp_path / "models" / "ggml-base.en.bin"
    sha256 = hashlib.sha256(b"managed-model-fixture").hexdigest()

    saved = save_model_settings(model_path, sha256, path=path)
    loaded = load_model_settings(path)

    assert loaded is not None
    assert loaded == saved
    assert loaded.path == model_path.resolve()
    assert loaded.sha256 == sha256
    assert path.stat().st_size < 4 * 1024
    assert list(path.parent.glob("*.tmp")) == []
    document = json.loads(path.read_text())
    assert document == {
        "model": {"path": str(model_path.resolve()), "sha256": sha256},
        "version": 1,
    }


def test_missing_settings_returns_none(tmp_path: Path) -> None:
    assert load_model_settings(tmp_path / "model.json") is None


def test_invalid_settings_fail_with_actionable_errors(tmp_path: Path) -> None:
    path = tmp_path / "model.json"

    path.write_text("not-json")
    with pytest.raises(ModelSettingsError, match="not valid JSON"):
        load_model_settings(path)

    path.write_text('{"version": 1, "model": {"path": "", "sha256": "abc"}}')
    with pytest.raises(ModelSettingsError, match="invalid values"):
        load_model_settings(path)

    path.write_text('{"version": 9, "model": {"path": "m.bin", "sha256": "abc"}}')
    with pytest.raises(ModelSettingsError, match="unsupported version"):
        load_model_settings(path)


def test_saved_model_loads_into_app_config_and_env_can_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VOICE2TEXT_MODEL_PATH", raising=False)
    monkeypatch.delenv("VOICE2TEXT_MODEL_SHA256", raising=False)
    model_path = tmp_path / "models" / "ggml-base.en.bin"
    saved_sha = "a" * 64
    save_model_settings(model_path, saved_sha, path=tmp_path / "model.json")

    config = AppConfig.from_environment(
        trigger_settings_path=tmp_path / "missing.json",
        model_settings_path=tmp_path / "model.json",
    )
    assert config.transcriber.model_path == model_path.resolve()
    assert config.transcriber.model_sha256 == saved_sha

    env_model = tmp_path / "managed" / "ggml-base.en.bin"
    env_sha = "b" * 64
    monkeypatch.setenv("VOICE2TEXT_MODEL_PATH", str(env_model))
    monkeypatch.setenv("VOICE2TEXT_MODEL_SHA256", env_sha)
    overridden = AppConfig.from_environment(
        trigger_settings_path=tmp_path / "missing.json",
        model_settings_path=tmp_path / "model.json",
    )
    assert overridden.transcriber.model_path == env_model.resolve()
    assert overridden.transcriber.model_sha256 == env_sha


def test_no_model_configured_leaves_transcriber_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VOICE2TEXT_MODEL_PATH", raising=False)
    monkeypatch.delenv("VOICE2TEXT_MODEL_SHA256", raising=False)

    config = AppConfig.from_environment(
        trigger_settings_path=tmp_path / "missing.json",
        model_settings_path=tmp_path / "missing-model.json",
    )
    assert config.transcriber.model_path is None
    assert config.transcriber.model_sha256 is None
