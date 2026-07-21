"""Build the signed-ready Windows one-folder application with its reviewed model payload."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cx_Freeze import Executable, setup

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from voice2text import __version__  # noqa: E402
from voice2text.model_settings import DEFAULT_MODEL_ID, managed_model  # noqa: E402
from voice2text.transcriber import sha256_file  # noqa: E402

MODEL = managed_model(DEFAULT_MODEL_ID)
DEFAULT_MODEL_PATH = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "voice2text" / "models" / MODEL.file_name
)
MODEL_PATH = Path(os.environ.get("VOICE2TEXT_BUILD_MODEL_PATH", DEFAULT_MODEL_PATH)).resolve()
if not MODEL_PATH.is_file():
    raise RuntimeError(
        "The reviewed Whisper model is unavailable. Set VOICE2TEXT_BUILD_MODEL_PATH or run "
        "`uv run voice2text --setup-model` before building."
    )
if sha256_file(MODEL_PATH).lower() != MODEL.sha256.lower():
    raise RuntimeError("The installer model does not match the reviewed SHA-256")

BUILD_DIR = Path(os.environ.get("VOICE2TEXT_BUILD_DIR", ROOT / "build" / "voice2text"))
BUILD_BASE = os.environ.get("VOICE2TEXT_BUILD_BASE", "gui")
EXECUTABLE_BASE = None if BUILD_BASE == "console" else BUILD_BASE
TARGET_NAME = os.environ.get("VOICE2TEXT_TARGET_NAME", "Voice2Text.exe")

BUILD_OPTIONS = {
    "build_exe": str(BUILD_DIR),
    "include_files": [
        (str(MODEL_PATH), f"models/{MODEL.file_name}"),
    ],
    "include_msvcr": True,
    "packages": [
        "PIL",
        "certifi",
        "comtypes",
        "httpx",
        "numpy",
        "pystray",
        "pywhispercpp",
        "pywinauto",
        "sounddevice",
        "soxr",
        "tkinter",
        "voice2text",
    ],
    "excludes": [
        "pytest",
        "pytest_cov",
        "ruff",
        "setuptools",
    ],
    "optimize": 1,
    "silent_level": 1,
    "zip_include_packages": ["encodings"],
}

setup(
    name="Voice2Text",
    version=__version__,
    description="Private, local Windows push-to-talk dictation",
    options={"build_exe": BUILD_OPTIONS},
    executables=[
        Executable(
            script=str(SRC / "voice2text" / "__main__.py"),
            base=EXECUTABLE_BASE,
            target_name=TARGET_NAME,
            icon=str(ROOT / "assets" / "voice2text.ico"),
            copyright="Copyright (c) 2026 Andrew Somerset",
        )
    ],
)
