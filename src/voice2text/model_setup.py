"""Explicit, checksum-verified model bootstrap for reproducible teammate setup.

This is a deliberate developer/setup action, not a runtime download. The resident app in
``main.py`` never fetches a model; it only loads a locally managed file and verifies its
SHA-256. ``setup_managed_model`` fetches (or registers) a reviewed model, verifies the pinned
checksum before installing it, and records the path so the runtime finds it automatically.

Enterprise GM deployment replaces this convenience with managed software distribution.
"""

from __future__ import annotations

import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from voice2text.model_settings import (
    DEFAULT_MODEL_ID,
    ManagedModel,
    ModelSettings,
    managed_model,
    save_model_settings,
)
from voice2text.transcriber import sha256_file

# Integrity is enforced by the pinned SHA-256; this cap only stops a runaway/hostile response
# from filling the disk before the checksum is ever checked.
_MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class ModelSetupError(RuntimeError):
    """Raised when a model cannot be fetched, verified, or installed safely."""


class Downloader(Protocol):
    """Fetch bytes from a trusted HTTPS URL into a destination file."""

    def download(self, url: str, destination: Path, *, max_bytes: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ModelSetupResult:
    """Content-free description of what setup did."""

    model: ManagedModel
    settings: ModelSettings
    action: str  # "downloaded", "reused", or "registered"


class _UrllibDownloader:
    """Stream a managed model over HTTPS with a hard size ceiling."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self._timeout_seconds = timeout_seconds

    def download(self, url: str, destination: Path, *, max_bytes: int) -> None:
        if not url.startswith("https://"):
            raise ModelSetupError("model downloads must use HTTPS")
        request = urllib.request.Request(url, headers={"User-Agent": "voice2text-setup"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                _stream_to_file(response, destination, max_bytes=max_bytes)
        except OSError as exc:
            raise ModelSetupError("model download failed") from exc


def _stream_to_file(source: object, destination: Path, *, max_bytes: int) -> None:
    read = getattr(source, "read", None)
    if read is None:  # pragma: no cover - defensive guard for a bad downloader
        raise ModelSetupError("download source is not readable")
    total = 0
    with destination.open("wb") as target:
        while chunk := read(_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise ModelSetupError("model download exceeded the maximum allowed size")
            target.write(chunk)


def setup_managed_model(
    model_id: str = DEFAULT_MODEL_ID,
    *,
    models_dir: Path | None = None,
    source_file: Path | None = None,
    downloader: Downloader | None = None,
    settings_path: Path | None = None,
) -> ModelSetupResult:
    """Fetch or register a reviewed model, verify its checksum, and record it for the runtime.

    ``source_file`` registers an already-downloaded file (useful where direct downloads are
    blocked). Otherwise an existing valid file is reused, or the pinned URL is downloaded. The
    file is verified against the reviewed SHA-256 before it is installed or recorded.
    """

    from voice2text.model_settings import default_models_dir

    model = managed_model(model_id)
    destination_dir = (models_dir or default_models_dir()).expanduser().resolve()
    destination = destination_dir / model.file_name
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelSetupError("could not create the local models directory") from exc

    if source_file is not None:
        action = _register_source_file(source_file, destination, model)
    elif _file_matches(destination, model.sha256):
        action = "reused"
    else:
        _download_and_verify(model, destination, downloader or _UrllibDownloader())
        action = "downloaded"

    actual = sha256_file(destination)
    if actual.lower() != model.sha256.lower():  # pragma: no cover - defense in depth
        raise ModelSetupError("installed model failed final checksum verification")

    settings = save_model_settings(destination, model.sha256, path=settings_path)
    return ModelSetupResult(model=model, settings=settings, action=action)


def _file_matches(path: Path, expected_sha256: str) -> bool:
    if not path.is_file():
        return False
    return sha256_file(path).lower() == expected_sha256.lower()


def _register_source_file(source_file: Path, destination: Path, model: ManagedModel) -> str:
    source = source_file.expanduser().resolve()
    if not source.is_file():
        raise ModelSetupError(f"source model file does not exist: {source}")
    if sha256_file(source).lower() != model.sha256.lower():
        raise ModelSetupError(
            f"source model file does not match the reviewed checksum for {model.model_id}"
        )
    if source == destination:
        return "registered"
    _atomic_place(destination, lambda temporary: shutil.copyfile(source, temporary))
    return "registered"


def _download_and_verify(model: ManagedModel, destination: Path, downloader: Downloader) -> None:
    def produce(temporary: Path) -> None:
        downloader.download(model.url, temporary, max_bytes=_MAX_DOWNLOAD_BYTES)
        if sha256_file(temporary).lower() != model.sha256.lower():
            raise ModelSetupError(
                f"downloaded model does not match the reviewed checksum for {model.model_id}"
            )

    _atomic_place(destination, produce)


def _atomic_place(destination: Path, produce: object) -> None:
    """Build the file at a temporary path, then atomically move it into place on success."""

    temporary = destination.with_name(f".{destination.name}.download")
    temporary.unlink(missing_ok=True)
    try:
        produce(temporary)  # type: ignore[operator]
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
