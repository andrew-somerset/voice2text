from __future__ import annotations

import pytest

from voice2text.config import AppConfig, AudioConfig, ConfigError, GleanConfig, TriggerConfig


def test_defaults_are_safe_for_mock_mode() -> None:
    config = AppConfig()

    assert config.trigger.scan_code == 0x1D
    assert config.trigger.extended is True
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


def test_audio_contract_rejects_wrong_rate() -> None:
    with pytest.raises(ConfigError, match="16 kHz"):
        AudioConfig(sample_rate=48_000)


def test_environment_loader_parses_non_secret_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE2TEXT_TRIGGER_SCAN_CODE", "0x2a")
    monkeypatch.setenv("VOICE2TEXT_TRIGGER_EXTENDED", "false")
    monkeypatch.setenv("VOICE2TEXT_GLEAN_SCOPES", "CHAT,offline_access")

    config = AppConfig.from_environment()

    assert config.trigger.scan_code == 0x2A
    assert config.trigger.extended is False
    assert config.glean.scopes == ("CHAT", "offline_access")
