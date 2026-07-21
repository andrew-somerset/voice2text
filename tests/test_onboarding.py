from __future__ import annotations

import hashlib
from pathlib import Path

from voice2text.background import LaunchResult
from voice2text.config import AppConfig, TranscriberConfig
from voice2text.onboarding import (
    MicrophoneCheckResult,
    OnboardingError,
    complete_setup,
    config_for_trigger,
    open_microphone_settings,
    probe_microphone,
    stop_listener_for_setup,
    verify_local_model,
    wait_for_listener_stop,
)


class FakeRecorder:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def start(self) -> None:
        self.calls.append("start")
        if self.failure is not None:
            raise self.failure

    def cancel(self) -> None:
        self.calls.append("cancel")

    def close(self) -> None:
        self.calls.append("close")


def test_microphone_probe_activates_cancels_and_closes_without_retaining_audio() -> None:
    recorder = FakeRecorder()

    result = probe_microphone(AppConfig(), recorder_factory=lambda: recorder)

    assert result == MicrophoneCheckResult(True, "Microphone access is enabled and working.")
    assert recorder.calls == ["open", "start", "cancel", "close"]


def test_microphone_probe_returns_safe_failure_and_still_closes() -> None:
    recorder = FakeRecorder(failure=RuntimeError("private-driver-detail"))

    result = probe_microphone(AppConfig(), recorder_factory=lambda: recorder)

    assert result.ready is False
    assert "private-driver-detail" not in result.message
    assert recorder.calls[-1] == "close"


def test_microphone_settings_opens_the_exact_windows_privacy_page() -> None:
    opened: list[str] = []

    open_microphone_settings(opener=lambda uri: opened.append(uri))

    assert opened == ["ms-settings:privacy-microphone"]


def test_model_verification_accepts_only_the_configured_checksum(tmp_path: Path) -> None:
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"verified-model-fixture")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    config = AppConfig(transcriber=TranscriberConfig(model_path=model, model_sha256=digest))

    assert verify_local_model(config_loader=lambda: config) is config

    mismatched = AppConfig(transcriber=TranscriberConfig(model_path=model, model_sha256="0" * 64))
    try:
        verify_local_model(config_loader=lambda: mismatched)
    except OnboardingError as exc:
        assert "integrity check" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("mismatched model was accepted")


def test_trigger_test_config_uses_reviewed_physical_identity() -> None:
    configured = config_for_trigger(AppConfig(), "right-alt")

    assert configured.trigger.display_name == "Right Alt"
    assert configured.trigger.scan_code == 0x38
    assert configured.trigger.extended is True


def test_listener_stop_is_noop_when_runtime_is_not_running() -> None:
    stop_calls: list[str] = []

    assert (
        stop_listener_for_setup(
            running_probe=lambda: False,
            stop_request=lambda: stop_calls.append("stop") or True,
        )
        is False
    )
    assert stop_calls == []
    assert wait_for_listener_stop(running_probe=lambda: False) is True


def test_listener_stop_requests_and_waits_for_clean_shutdown() -> None:
    calls: list[str] = []

    assert (
        stop_listener_for_setup(
            running_probe=lambda: True,
            stop_request=lambda: calls.append("stop") or True,
            waiter=lambda: calls.append("wait") or True,
        )
        is True
    )
    assert calls == ["stop", "wait"]


def test_completion_saves_trigger_registers_startup_and_launches() -> None:
    calls: list[object] = []

    result = complete_setup(
        "f9",
        running_probe=lambda: False,
        save_trigger=lambda choice, **options: calls.append((choice, options)),
        startup_installer=lambda: calls.append("install"),
        startup_uninstaller=lambda: calls.append("uninstall"),
        launcher=lambda: calls.append("launch") or LaunchResult.STARTED,
    )

    assert result.trigger.display_name == "F9"
    assert result.launch_result is LaunchResult.STARTED
    assert calls == [
        ("f9", {"suppress_chords": True}),
        "install",
        "launch",
    ]


def test_completion_rolls_back_startup_when_listener_cannot_become_ready() -> None:
    calls: list[str] = []

    try:
        complete_setup(
            "right-ctrl",
            running_probe=lambda: False,
            save_trigger=lambda _choice, **_options: None,
            startup_installer=lambda: calls.append("install"),
            startup_uninstaller=lambda: calls.append("uninstall"),
            launcher=lambda: LaunchResult.FAILED,
        )
    except OnboardingError as exc:
        assert "did not become ready" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("failed listener launch was accepted")

    assert calls == ["install", "uninstall"]
