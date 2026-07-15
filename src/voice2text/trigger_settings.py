"""Persist a privacy-safe trigger choice selected during setup."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

_SETTINGS_VERSION = 1
_MAX_SETTINGS_BYTES = 4 * 1024
_CHOICE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TriggerSettingsError(ValueError):
    """A saved trigger setting is unavailable, invalid, or could not be written."""


@dataclass(frozen=True, slots=True)
class TriggerChoice:
    """One physical key identity that Windows Raw Input can distinguish."""

    choice_id: str
    display_name: str
    scan_code: int
    extended: bool
    description: str

    def __post_init__(self) -> None:
        if not _CHOICE_ID_PATTERN.fullmatch(self.choice_id):
            raise ValueError("trigger choice ID is invalid")
        if not self.display_name or len(self.display_name) > 64:
            raise ValueError("trigger display name is invalid")
        if not 0 <= self.scan_code <= 0xFFFF:
            raise ValueError("trigger scan code must fit in 16 bits")
        if not isinstance(self.extended, bool):
            raise TypeError("trigger extended flag must be boolean")
        if not self.description:
            raise ValueError("trigger description cannot be empty")


_TRIGGER_CHOICES = (
    TriggerChoice(
        choice_id="right-alt",
        display_name="Right Alt",
        scan_code=0x38,
        extended=True,
        description=(
            "Convenient on compact keyboards; combinations such as AltGr are chord-suppressed, "
            "but Windows and applications may still perform their normal Alt behavior."
        ),
    ),
    TriggerChoice(
        choice_id="right-ctrl",
        display_name="Right Ctrl",
        scan_code=0x1D,
        extended=True,
        description="Safest modifier baseline because it usually has no standalone action.",
    ),
    TriggerChoice(
        choice_id="right-shift",
        display_name="Right Shift",
        scan_code=0x36,
        extended=False,
        description="Available on most full-size keyboards; normal Shift combinations are ignored.",
    ),
    TriggerChoice(
        choice_id="f8",
        display_name="F8",
        scan_code=0x42,
        extended=False,
        description="A non-modifier option; verify that required applications do not use F8.",
    ),
    TriggerChoice(
        choice_id="f9",
        display_name="F9",
        scan_code=0x43,
        extended=False,
        description="A non-modifier option; verify that required applications do not use F9.",
    ),
)
_TRIGGER_CHOICES_BY_ID = {choice.choice_id: choice for choice in _TRIGGER_CHOICES}


@dataclass(frozen=True, slots=True)
class TriggerSettings:
    """Non-secret per-user trigger settings loaded before application startup."""

    choice_id: str
    suppress_chords: bool = True

    def __post_init__(self) -> None:
        trigger_choice(self.choice_id)
        if not isinstance(self.suppress_chords, bool):
            raise TriggerSettingsError("trigger chord suppression must be boolean")

    @property
    def choice(self) -> TriggerChoice:
        return trigger_choice(self.choice_id)


def trigger_choices() -> tuple[TriggerChoice, ...]:
    """Return the reviewed installer choices in display order."""

    return _TRIGGER_CHOICES


def trigger_choice(choice_id: str) -> TriggerChoice:
    """Resolve a stable installer choice ID to its physical Raw Input identity."""

    try:
        return _TRIGGER_CHOICES_BY_ID[choice_id]
    except (KeyError, TypeError):
        raise TriggerSettingsError(f"unsupported trigger choice: {choice_id}") from None


def describe_trigger(scan_code: int, extended: bool) -> str:
    """Return a safe display name without inspecting typed characters."""

    for choice in _TRIGGER_CHOICES:
        if choice.scan_code == scan_code and choice.extended == extended:
            return choice.display_name
    suffix = " extended" if extended else ""
    return f"Scan code 0x{scan_code:02X}{suffix}"


def default_trigger_settings_path() -> Path:
    """Return the per-user, non-roaming settings path used by setup."""

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise TriggerSettingsError("LOCALAPPDATA is unavailable for trigger settings")
    return Path(local_app_data).expanduser().resolve() / "voice2text" / "settings.json"


def load_trigger_settings(path: Path | None = None) -> TriggerSettings | None:
    """Load a small, strictly validated settings document, or None before first-run setup."""

    if path is None:
        try:
            settings_path = default_trigger_settings_path()
        except TriggerSettingsError:
            return None
    else:
        settings_path = path.expanduser().resolve()

    try:
        if settings_path.stat().st_size > _MAX_SETTINGS_BYTES:
            raise TriggerSettingsError("trigger settings exceed the size limit")
        raw = settings_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TriggerSettingsError("could not read trigger settings") from exc

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise TriggerSettingsError("trigger settings are not valid JSON") from None
    if not isinstance(document, dict) or set(document) != {"version", "trigger"}:
        raise TriggerSettingsError("trigger settings have an invalid structure")
    if document["version"] != _SETTINGS_VERSION:
        raise TriggerSettingsError("trigger settings use an unsupported version")

    trigger = document["trigger"]
    if not isinstance(trigger, dict) or set(trigger) != {"choice", "suppressChords"}:
        raise TriggerSettingsError("trigger settings have an invalid trigger entry")
    choice_id = trigger["choice"]
    suppress_chords = trigger["suppressChords"]
    if not isinstance(choice_id, str) or not isinstance(suppress_chords, bool):
        raise TriggerSettingsError("trigger settings contain invalid values")
    return TriggerSettings(choice_id=choice_id, suppress_chords=suppress_chords)


def save_trigger_settings(
    choice_id: str,
    *,
    suppress_chords: bool = True,
    path: Path | None = None,
) -> TriggerSettings:
    """Atomically save a reviewed trigger choice for the current Windows user."""

    settings = TriggerSettings(choice_id=choice_id, suppress_chords=suppress_chords)
    settings_path = (path or default_trigger_settings_path()).expanduser().resolve()
    payload = json.dumps(
        {
            "version": _SETTINGS_VERSION,
            "trigger": {
                "choice": settings.choice_id,
                "suppressChords": settings.suppress_chords,
            },
        },
        indent=2,
        sort_keys=True,
    ).encode()
    if len(payload) > _MAX_SETTINGS_BYTES:
        raise TriggerSettingsError("trigger settings exceed the size limit")

    temporary = settings_path.with_name(f".{settings_path.name}.{secrets.token_hex(8)}.tmp")
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as settings_file:
            settings_file.write(payload)
            settings_file.write(b"\n")
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.replace(temporary, settings_path)
    except OSError as exc:
        raise TriggerSettingsError("could not save trigger settings") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return settings
