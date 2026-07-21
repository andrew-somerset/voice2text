"""Validated application configuration with no secrets in source control."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from voice2text.model_settings import (
    ModelSettingsError,
    bundled_model_settings,
    load_model_settings,
)
from voice2text.trigger_settings import (
    TriggerSettingsError,
    describe_trigger,
    load_trigger_settings,
    trigger_choice,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ConfigError(ValueError):
    """Raised when application configuration is unsafe or inconsistent."""


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _optional_path(value: str | None) -> Path | None:
    return Path(value).expanduser().resolve() if value else None


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    """Windows Raw Input identity and gesture timing for the trigger key."""

    scan_code: int = 0x1D
    extended: bool = True
    display_name: str = ""
    suppress_chords: bool = True
    chord_grace_seconds: float = 0.08
    tap_max_seconds: float = 0.25
    double_tap_window_seconds: float = 0.35
    glean_max_recording_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not 0 <= self.scan_code <= 0xFFFF:
            raise ConfigError("trigger scan_code must fit in an unsigned 16-bit value")
        if not isinstance(self.extended, bool):
            raise ConfigError("trigger extended flag must be boolean")
        if not self.display_name:
            object.__setattr__(
                self,
                "display_name",
                describe_trigger(self.scan_code, self.extended),
            )
        if len(self.display_name) > 64 or any(
            ord(character) < 0x20 for character in self.display_name
        ):
            raise ConfigError("trigger display_name is invalid")
        if not isinstance(self.suppress_chords, bool):
            raise ConfigError("trigger suppress_chords flag must be boolean")
        if not 0.02 <= self.chord_grace_seconds <= 0.15:
            raise ConfigError("chord_grace_seconds must be between 0.02 and 0.15")
        if not 0.05 <= self.tap_max_seconds <= 1.0:
            raise ConfigError("tap_max_seconds must be between 0.05 and 1.0")
        if self.chord_grace_seconds >= self.tap_max_seconds:
            raise ConfigError("chord_grace_seconds must be shorter than tap_max_seconds")
        if not self.tap_max_seconds <= self.double_tap_window_seconds <= 1.5:
            raise ConfigError(
                "double_tap_window_seconds must be at least tap_max_seconds and at most 1.5"
            )
        if not 1.0 <= self.glean_max_recording_seconds <= 600.0:
            raise ConfigError("glean_max_recording_seconds must be between 1 and 600")


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Audio contract required by whisper.cpp."""

    sample_rate: int = 16_000
    channels: int = 1
    dtype: str = "float32"
    block_duration_ms: int = 20

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ConfigError("the initial Whisper pipeline requires a 16 kHz microphone stream")
        if self.channels != 1:
            raise ConfigError("the initial Whisper pipeline requires mono audio")
        if self.dtype != "float32":
            raise ConfigError("the initial Whisper pipeline requires float32 audio")
        if not 5 <= self.block_duration_ms <= 100:
            raise ConfigError("block_duration_ms must be between 5 and 100")

    @property
    def block_size(self) -> int:
        """Number of frames requested from PortAudio per callback."""

        return self.sample_rate * self.block_duration_ms // 1_000


@dataclass(frozen=True, slots=True)
class TranscriberConfig:
    """Local model configuration; production requires a managed model path and hash."""

    model_path: Path | None = None
    model_sha256: str | None = None
    n_threads: int = field(default_factory=lambda: max(1, min(8, os.cpu_count() or 4)))
    min_utterance_seconds: float = 0.30
    language: str = "en"

    def __post_init__(self) -> None:
        if self.model_sha256 and not _SHA256_PATTERN.fullmatch(self.model_sha256):
            raise ConfigError("model_sha256 must contain exactly 64 hexadecimal characters")
        if self.model_sha256 and self.model_path is None:
            raise ConfigError("model_path is required when model_sha256 is configured")
        if not 1 <= self.n_threads <= 64:
            raise ConfigError("n_threads must be between 1 and 64")
        if not 0.1 <= self.min_utterance_seconds <= 2.0:
            raise ConfigError("min_utterance_seconds must be between 0.1 and 2.0")
        if not self.language:
            raise ConfigError("language cannot be empty")


@dataclass(frozen=True, slots=True)
class GleanConfig:
    """Non-secret Glean client settings."""

    mode: Literal["mock", "live"] = "mock"
    server_url: str | None = None
    client_id: str | None = None
    scopes: tuple[str, ...] = ("CHAT",)
    application_id: str | None = None
    save_chat: bool = False
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.mode not in {"mock", "live"}:
            raise ConfigError("Glean mode must be 'mock' or 'live'")
        if self.mode == "live":
            if not self.server_url or not self.client_id:
                raise ConfigError("live Glean mode requires server_url and client_id")
            if not self.server_url.startswith("https://"):
                raise ConfigError("live Glean server_url must use HTTPS")
        if not self.scopes or any(not scope for scope in self.scopes):
            raise ConfigError("at least one non-empty Glean scope is required")
        if not 1.0 <= self.request_timeout_seconds <= 120.0:
            raise ConfigError("request_timeout_seconds must be between 1 and 120")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Top-level immutable application configuration."""

    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcriber: TranscriberConfig = field(default_factory=TranscriberConfig)
    glean: GleanConfig = field(default_factory=GleanConfig)
    verbose: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        trigger_settings_path: Path | None = None,
        model_settings_path: Path | None = None,
    ) -> AppConfig:
        """Build configuration from non-secret environment values."""

        mode = os.getenv("VOICE2TEXT_GLEAN_MODE", "mock").strip().lower()
        scopes = tuple(
            scope.strip()
            for scope in os.getenv("VOICE2TEXT_GLEAN_SCOPES", "CHAT").split(",")
            if scope.strip()
        )
        try:
            saved_trigger = load_trigger_settings(trigger_settings_path)
            selected_choice = trigger_choice(
                os.getenv(
                    "VOICE2TEXT_TRIGGER_CHOICE",
                    saved_trigger.choice_id if saved_trigger is not None else "right-ctrl",
                ).strip()
            )
        except TriggerSettingsError as exc:
            raise ConfigError(str(exc)) from None

        env_model_path = os.getenv("VOICE2TEXT_MODEL_PATH")
        env_model_sha256 = os.getenv("VOICE2TEXT_MODEL_SHA256")
        if env_model_path or env_model_sha256:
            model_path = _optional_path(env_model_path)
            model_sha256 = env_model_sha256 or None
        else:
            try:
                saved_model = load_model_settings(model_settings_path)
            except ModelSettingsError as exc:
                raise ConfigError(str(exc)) from None
            resolved_model = saved_model or bundled_model_settings()
            model_path = resolved_model.path if resolved_model is not None else None
            model_sha256 = resolved_model.sha256 if resolved_model is not None else None

        trigger_scan_code_value = os.getenv("VOICE2TEXT_TRIGGER_SCAN_CODE")
        trigger_extended_value = os.getenv("VOICE2TEXT_TRIGGER_EXTENDED")
        trigger_suppress_chords_value = os.getenv("VOICE2TEXT_TRIGGER_SUPPRESS_CHORDS")
        verbose_value = os.getenv("VOICE2TEXT_VERBOSE")
        trigger_scan_code = (
            selected_choice.scan_code
            if trigger_scan_code_value is None
            else int(trigger_scan_code_value, 0)
        )
        trigger_extended = (
            selected_choice.extended
            if trigger_extended_value is None
            else _parse_bool(trigger_extended_value, name="VOICE2TEXT_TRIGGER_EXTENDED")
        )
        trigger_identity_overridden = (
            trigger_scan_code_value is not None or trigger_extended_value is not None
        )
        trigger_suppress_chords = (
            saved_trigger.suppress_chords if saved_trigger is not None else True
        )
        if trigger_suppress_chords_value is not None:
            trigger_suppress_chords = _parse_bool(
                trigger_suppress_chords_value,
                name="VOICE2TEXT_TRIGGER_SUPPRESS_CHORDS",
            )

        return cls(
            trigger=TriggerConfig(
                scan_code=trigger_scan_code,
                extended=trigger_extended,
                display_name=(
                    describe_trigger(trigger_scan_code, trigger_extended)
                    if trigger_identity_overridden
                    else selected_choice.display_name
                ),
                suppress_chords=trigger_suppress_chords,
                chord_grace_seconds=float(
                    os.getenv("VOICE2TEXT_TRIGGER_CHORD_GRACE_SECONDS", "0.08")
                ),
                tap_max_seconds=float(os.getenv("VOICE2TEXT_TAP_MAX_SECONDS", "0.25")),
                double_tap_window_seconds=float(
                    os.getenv("VOICE2TEXT_DOUBLE_TAP_WINDOW_SECONDS", "0.35")
                ),
                glean_max_recording_seconds=float(
                    os.getenv("VOICE2TEXT_GLEAN_MAX_RECORDING_SECONDS", "120")
                ),
            ),
            transcriber=TranscriberConfig(
                model_path=model_path,
                model_sha256=model_sha256,
                n_threads=int(
                    os.getenv(
                        "VOICE2TEXT_WHISPER_THREADS",
                        str(max(1, min(8, os.cpu_count() or 4))),
                    )
                ),
            ),
            glean=GleanConfig(
                mode=mode,  # type: ignore[arg-type]
                server_url=os.getenv("VOICE2TEXT_GLEAN_SERVER_URL") or None,
                client_id=os.getenv("VOICE2TEXT_GLEAN_CLIENT_ID") or None,
                scopes=scopes,
                application_id=os.getenv("VOICE2TEXT_GLEAN_APPLICATION_ID") or None,
                save_chat=_parse_bool(
                    os.getenv("VOICE2TEXT_GLEAN_SAVE_CHAT", "false"),
                    name="VOICE2TEXT_GLEAN_SAVE_CHAT",
                ),
            ),
            verbose=(
                False
                if verbose_value is None
                else _parse_bool(verbose_value, name="VOICE2TEXT_VERBOSE")
            ),
        )
