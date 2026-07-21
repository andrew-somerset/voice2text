"""Resolve and persist a locally managed whisper.cpp model for reproducible setup.

This module lets a teammate run one explicit setup command instead of hand-sourcing a
model file and pasting a 64-character checksum into two environment variables. It stores
only a non-secret path and the reviewed SHA-256 for the current Windows user. It never
downloads anything itself; ``model_setup`` performs the explicit, checksum-verified fetch.
The resident runtime still verifies the checksum before loading the model.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

_SETTINGS_VERSION = 1
_MAX_SETTINGS_BYTES = 4 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MODEL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[.\-][a-z0-9]+)*$")


class ModelSettingsError(ValueError):
    """A saved model setting is unavailable, invalid, or could not be written."""


@dataclass(frozen=True, slots=True)
class ManagedModel:
    """A reviewed whisper.cpp model that setup can fetch and verify by checksum.

    Only models with a reviewed, pinned SHA-256 belong here. The checksum is the integrity
    gate: ``model_setup`` refuses to install a file whose hash does not match ``sha256``.
    """

    model_id: str
    display_name: str
    file_name: str
    sha256: str
    url: str
    approx_bytes: int
    description: str

    def __post_init__(self) -> None:
        if not _MODEL_ID_PATTERN.fullmatch(self.model_id):
            raise ValueError("managed model ID is invalid")
        if not self.display_name or len(self.display_name) > 64:
            raise ValueError("managed model display name is invalid")
        if not self.file_name or "/" in self.file_name or "\\" in self.file_name:
            raise ValueError("managed model file name is invalid")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("managed model sha256 must be 64 hexadecimal characters")
        if not self.url.startswith("https://"):
            raise ValueError("managed model URL must use HTTPS")
        if self.approx_bytes <= 0:
            raise ValueError("managed model approx_bytes must be positive")
        if not self.description:
            raise ValueError("managed model description cannot be empty")


# The base.en artifact is the benchmarked, checksum-verified default on gm_dev (see README).
# Adding a model here is a reviewed action: pin its exact SHA-256 first. For any model that is
# not in this catalog, teammates can still register an already-downloaded file with --model-file.
_MANAGED_MODELS = (
    ManagedModel(
        model_id="base.en",
        display_name="Whisper base.en",
        file_name="ggml-base.en.bin",
        sha256="a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
        approx_bytes=147_951_465,
        description="Benchmarked English CPU default (~148 MB); balances accuracy and latency.",
    ),
)
_MANAGED_MODELS_BY_ID = {model.model_id: model for model in _MANAGED_MODELS}
DEFAULT_MODEL_ID = "base.en"


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Non-secret per-user model settings loaded before application startup."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ModelSettingsError("model sha256 must contain exactly 64 hexadecimal characters")


def managed_models() -> tuple[ManagedModel, ...]:
    """Return the reviewed, checksum-pinned models setup can fetch."""

    return _MANAGED_MODELS


def managed_model(model_id: str) -> ManagedModel:
    """Resolve a reviewed model ID to its pinned download and checksum details."""

    try:
        return _MANAGED_MODELS_BY_ID[model_id]
    except (KeyError, TypeError):
        raise ModelSettingsError(f"unsupported model choice: {model_id}") from None


def _local_app_data() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise ModelSettingsError("LOCALAPPDATA is unavailable for model settings")
    return Path(local_app_data).expanduser().resolve() / "voice2text"


def default_models_dir() -> Path:
    """Return the per-user directory that holds locally managed model files."""

    return _local_app_data() / "models"


def default_model_settings_path() -> Path:
    """Return the per-user, non-roaming model settings path used by setup."""

    return _local_app_data() / "model.json"


def bundled_model_settings(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> ModelSettings | None:
    """Resolve the reviewed model shipped beside a frozen one-folder executable.

    Source checkouts never gain an implicit model path. A packaged build may place the pinned
    default under ``models`` next to the executable; the normal ``Transcriber`` checksum gate
    still verifies the complete file before loading it.
    """

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return None
    model = managed_model(DEFAULT_MODEL_ID)
    current = (executable or Path(sys.executable)).expanduser().resolve()
    model_path = current.parent / "models" / model.file_name
    if not model_path.is_file():
        return None
    return ModelSettings(path=model_path, sha256=model.sha256)


def load_model_settings(path: Path | None = None) -> ModelSettings | None:
    """Load a small, strictly validated model document, or None before model setup."""

    if path is None:
        try:
            settings_path = default_model_settings_path()
        except ModelSettingsError:
            return None
    else:
        settings_path = path.expanduser().resolve()

    try:
        if settings_path.stat().st_size > _MAX_SETTINGS_BYTES:
            raise ModelSettingsError("model settings exceed the size limit")
        raw = settings_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ModelSettingsError("could not read model settings") from exc

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ModelSettingsError("model settings are not valid JSON") from None
    if not isinstance(document, dict) or set(document) != {"version", "model"}:
        raise ModelSettingsError("model settings have an invalid structure")
    if document["version"] != _SETTINGS_VERSION:
        raise ModelSettingsError("model settings use an unsupported version")

    model = document["model"]
    if not isinstance(model, dict) or set(model) != {"path", "sha256"}:
        raise ModelSettingsError("model settings have an invalid model entry")
    model_path = model["path"]
    sha256 = model["sha256"]
    if not isinstance(model_path, str) or not model_path or not isinstance(sha256, str):
        raise ModelSettingsError("model settings contain invalid values")
    return ModelSettings(path=Path(model_path), sha256=sha256)


def save_model_settings(
    model_path: Path,
    sha256: str,
    *,
    path: Path | None = None,
) -> ModelSettings:
    """Atomically save the resolved model path and checksum for the current Windows user."""

    settings = ModelSettings(path=model_path.expanduser().resolve(), sha256=sha256)
    settings_path = (path or default_model_settings_path()).expanduser().resolve()
    payload = json.dumps(
        {
            "version": _SETTINGS_VERSION,
            "model": {
                "path": str(settings.path),
                "sha256": settings.sha256,
            },
        },
        indent=2,
        sort_keys=True,
    ).encode()
    if len(payload) > _MAX_SETTINGS_BYTES:
        raise ModelSettingsError("model settings exceed the size limit")

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
        raise ModelSettingsError("could not save model settings") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return settings
