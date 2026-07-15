from __future__ import annotations

import json
from pathlib import Path

import pytest

import voice2text.main as main_module
from voice2text.config import AppConfig, ConfigError
from voice2text.main import main
from voice2text.trigger_settings import (
    TriggerSettingsError,
    load_trigger_settings,
    save_trigger_settings,
    trigger_choice,
    trigger_choices,
)


def test_reviewed_choices_include_right_alt_and_distinct_physical_identities() -> None:
    choices = trigger_choices()
    identities = {(choice.scan_code, choice.extended) for choice in choices}

    assert trigger_choice("right-alt").display_name == "Right Alt"
    assert trigger_choice("right-alt").scan_code == 0x38
    assert trigger_choice("right-alt").extended is True
    assert trigger_choice("right-ctrl").scan_code == 0x1D
    assert len(identities) == len(choices)
    assert all(choice.choice_id != "fn" for choice in choices)


def test_settings_round_trip_is_small_atomic_and_non_secret(tmp_path: Path) -> None:
    path = tmp_path / "voice2text" / "settings.json"

    saved = save_trigger_settings("right-alt", path=path)
    loaded = load_trigger_settings(path)

    assert saved == loaded
    assert loaded is not None and loaded.choice.display_name == "Right Alt"
    assert path.stat().st_size < 4 * 1024
    assert list(path.parent.glob("*.tmp")) == []
    document = json.loads(path.read_text())
    assert document == {
        "trigger": {"choice": "right-alt", "suppressChords": True},
        "version": 1,
    }


def test_saved_choice_loads_into_app_config_and_environment_can_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    save_trigger_settings("right-alt", path=path)

    saved_config = AppConfig.from_environment(trigger_settings_path=path)
    assert saved_config.trigger.display_name == "Right Alt"
    assert saved_config.trigger.scan_code == 0x38

    monkeypatch.setenv("VOICE2TEXT_TRIGGER_CHOICE", "f8")
    overridden = AppConfig.from_environment(trigger_settings_path=path)
    assert overridden.trigger.display_name == "F8"
    assert overridden.trigger.scan_code == 0x42


def test_invalid_or_unknown_settings_fail_with_actionable_errors(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"version": 1, "trigger": {"choice": "fn", "suppressChords": true}}')

    with pytest.raises(TriggerSettingsError, match="unsupported trigger choice"):
        load_trigger_settings(path)
    with pytest.raises(ConfigError, match="unsupported trigger choice"):
        AppConfig.from_environment(trigger_settings_path=path)

    path.write_text("not-json")
    with pytest.raises(TriggerSettingsError, match="not valid JSON"):
        load_trigger_settings(path)


def test_cli_lists_fn_limitation_and_saves_installer_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert main(["--list-triggers"]) == 0
    listing = capsys.readouterr().out
    assert "right-alt" in listing
    assert "Fn is hardware-dependent" in listing

    assert main(["--configure-trigger", "right-alt"]) == 0
    confirmation = capsys.readouterr().out
    assert "Trigger saved: Right Alt" in confirmation
    loaded = load_trigger_settings(tmp_path / "voice2text" / "settings.json")
    assert loaded is not None and loaded.choice_id == "right-alt"


def test_cli_rejects_unsupported_fn_choice_without_writing_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert main(["--configure-trigger", "fn"]) == 2
    assert not (tmp_path / "voice2text" / "settings.json").exists()


def test_cli_picker_saves_returned_choice_without_real_ui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(main_module, "choose_trigger", lambda _initial: "f9")

    assert main(["--configure-trigger"]) == 0

    loaded = load_trigger_settings(tmp_path / "voice2text" / "settings.json")
    assert loaded is not None and loaded.choice_id == "f9"
