"""Configuration constants for voice2text."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Transcription. Override the model with VOICE2TEXT_MODEL or the --model CLI flag.
MODEL_NAME: str = os.environ.get("VOICE2TEXT_MODEL", "small.en")
LANGUAGE: str = "en"

# Audio capture. Whisper requires 16kHz mono float32 — record in that format
# from the start, never resample.
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
DTYPE: str = "float32"

# Utterances shorter than this are accidental taps; whisper hallucinates on them.
MIN_UTTERANCE_SECONDS: float = 0.3

# Transcription post-processing: strip spoken filler words from the result.
# Disable with VOICE2TEXT_REMOVE_FILLERS=0 or the --keep-fillers CLI flag.
REMOVE_FILLERS: bool = os.environ.get("VOICE2TEXT_REMOVE_FILLERS", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Whole-word tokens removed when REMOVE_FILLERS is on. Kept conservative so real
# words are never dropped (e.g. "hmm"/"like"/"so" are intentionally excluded).
FILLER_WORDS: tuple[str, ...] = ("um", "umm", "uh", "uhh", "uhm", "erm", "er")

# Custom vocabulary + learned corrections store (JSON). Override the location
# with VOICE2TEXT_VOCAB. Terms bias transcription; substitutions are applied
# literally to the result.
_APP_SUPPORT: Path = Path.home() / "Library" / "Application Support" / "voice2text"
VOCAB_PATH: Path = Path(os.environ.get("VOICE2TEXT_VOCAB", str(_APP_SUPPORT / "vocabulary.json")))

# Automatically learn from in-place corrections (best-effort, via Accessibility).
# Disable with VOICE2TEXT_LEARN=0 or the --no-learn CLI flag.
LEARN_CORRECTIONS: bool = os.environ.get("VOICE2TEXT_LEARN", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)

# kCGEventFlagMaskSecondaryFn — the Fn key bit in CGEvent flags.
FN_FLAG_MASK: int = 0x800000

# Pasting: clipboard write -> synthetic Cmd+V, and Cmd+V -> restore old clipboard.
PASTE_DELAY_SECONDS: float = 0.05
CLIPBOARD_RESTORE_DELAY_SECONDS: float = 0.3

# UI: the "copy the text" fallback window.
#   "on-failure" (default): show it only when a paste is blocked (e.g. macOS
#                           Secure Keyboard Entry is on), so it feels like a
#                           silent-paste dictation tool the rest of the time.
#   "always":               show it after every dictation.
#   "never":                never show it.
# Override with VOICE2TEXT_RESULT_WINDOW or the --show-result-window CLI flag.
RESULT_WINDOW_MODE: str = os.environ.get("VOICE2TEXT_RESULT_WINDOW", "on-failure")
RESULT_WINDOW_MODES: tuple[str, ...] = ("on-failure", "always", "never")
# Seconds the fallback window lingers before auto-closing (0 = until dismissed).
RESULT_WINDOW_LINGER_SECONDS: float = 12.0

# Listening indicator (Wispr-Flow-style pill) geometry, in points.
INDICATOR_WIDTH: int = 144
INDICATOR_HEIGHT: int = 38
INDICATOR_BOTTOM_MARGIN: int = 56  # distance from the screen's bottom edge


def performance_core_count() -> int:
    """Number of performance cores (Apple Silicon) — used for whisper n_threads."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return max(1, int(out))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return max(1, (os.cpu_count() or 4) // 2)
