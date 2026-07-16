from __future__ import annotations

import voice2text.local_runtime as local_runtime
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
