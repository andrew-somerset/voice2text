from __future__ import annotations

from pathlib import Path

import voice2text.tray as tray_module
from voice2text.tray import launch_settings_window, settings_command


def test_settings_command_uses_frozen_executable(tmp_path: Path) -> None:
    executable = tmp_path / "Voice2Text.exe"

    assert settings_command(executable=executable, frozen=True) == (
        str(executable.resolve()),
        "--settings",
    )


def test_settings_command_uses_source_pythonw(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    pythonw.write_bytes(b"")

    assert settings_command(executable=python, frozen=False) == (
        str(pythonw.resolve()),
        "-m",
        "voice2text",
        "--settings",
    )


def test_settings_launcher_is_console_free_and_uses_requested_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_popen(command: tuple[str, ...], **options: object) -> object:
        calls.append((command, options))
        return object()

    monkeypatch.setattr(tray_module.subprocess, "Popen", fake_popen)
    command = ("Voice2Text.exe", "--settings")

    launch_settings_window(command=command, cwd=tmp_path)

    assert calls[0][0] == command
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["creationflags"] == 0x00000008 | 0x00000200 | 0x08000000
