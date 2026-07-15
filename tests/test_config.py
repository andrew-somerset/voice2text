from __future__ import annotations

from pathlib import Path

import pytest

from voice2text.config import AppConfig, AudioConfig, ConfigError, GleanConfig, TriggerConfig


def test_defaults_are_safe_for_mock_mode() -> None:
    config = AppConfig()

    assert config.trigger.scan_code == 0x1D
    assert config.trigger.extended is True
    assert config.trigger.display_name == "Right Ctrl"
    assert config.trigger.suppress_chords is True
    assert config.trigger.chord_grace_seconds == 0.08
    assert config.audio.block_size == 320
    assert config.glean.mode == "mock"
    assert config.glean.scopes == ("CHAT",)


def test_live_glean_requires_https_server_and_client() -> None:
    with pytest.raises(ConfigError, match="requires server_url and client_id"):
        GleanConfig(mode="live")

    with pytest.raises(ConfigError, match="must use HTTPS"):
        GleanConfig(mode="live", server_url="http://example.test", client_id="client")


def test_trigger_timing_is_validated() -> None:
    with pytest.raises(ConfigError, match="at least tap_max_seconds"):
        TriggerConfig(tap_max_seconds=0.4, double_tap_window_seconds=0.3)

    with pytest.raises(ConfigError, match="shorter than tap_max_seconds"):
        TriggerConfig(chord_grace_seconds=0.1, tap_max_seconds=0.1)


def test_audio_contract_rejects_wrong_rate() -> None:
    with pytest.raises(ConfigError, match="16 kHz"):
        AudioConfig(sample_rate=48_000)


def test_environment_loader_parses_non_secret_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VOICE2TEXT_TRIGGER_CHOICE", "right-alt")
    monkeypatch.setenv("VOICE2TEXT_GLEAN_SCOPES", "CHAT,offline_access")

    config = AppConfig.from_environment(trigger_settings_path=tmp_path / "missing.json")

    assert config.trigger.scan_code == 0x38
    assert config.trigger.extended is True
    assert config.trigger.display_name == "Right Alt"
    assert config.glean.scopes == ("CHAT", "offline_access")
