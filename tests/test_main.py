from __future__ import annotations

import voice2text.background as background
import voice2text.local_runtime as local_runtime
import voice2text.onboarding as onboarding
from voice2text.main import main


def test_default_command_runs_persistent_local_dictation(
    monkeypatch,
) -> None:
    durations: list[float | None] = []
    monkeypatch.setattr(
        local_runtime,
        "run_local_dictation",
        lambda _config, *, duration_seconds=None: durations.append(duration_seconds),
    )

    assert main([]) == 0
    assert durations == [None]


def test_explicit_local_test_can_be_bounded(monkeypatch) -> None:
    durations: list[float | None] = []
    monkeypatch.setattr(
        local_runtime,
        "run_local_dictation",
        lambda _config, *, duration_seconds=None: durations.append(duration_seconds),
    )

    assert main(["--test-local-dictation", "--test-seconds", "5"]) == 0
    assert durations == [5.0]


def test_first_run_and_settings_open_the_guided_wizard(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        onboarding,
        "run_first_run_wizard",
        lambda *, reconfigure=False: calls.append(reconfigure) or True,
    )

    assert main(["--first-run"]) == 0
    assert main(["--settings"]) == 0
    assert calls == [False, True]


def test_start_background_reports_readiness(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        background,
        "launch_background",
        lambda: background.LaunchResult.STARTED,
    )

    assert main(["--start-background"]) == 0
    assert "ready" in capsys.readouterr().out


def test_install_startup_registers_and_starts(monkeypatch, capsys) -> None:
    calls: list[str] = []
    monkeypatch.setattr(background, "install_startup", lambda: calls.append("install"))
    monkeypatch.setattr(
        background,
        "launch_background",
        lambda: background.LaunchResult.STARTED,
    )

    assert main(["--install-startup"]) == 0
    assert calls == ["install"]
    assert "user sign-in" in capsys.readouterr().out


def test_background_status_is_content_free(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        background,
        "background_status",
        lambda: background.BackgroundStatus(True, True, True),
    )

    assert main(["--background-status"]) == 0
    output = capsys.readouterr().out
    assert "Listener running: yes" in output
    assert "Start at sign-in: yes" in output
