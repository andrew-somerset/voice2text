"""Non-persistent console-stream fallback for pythonw and frozen GUI launches."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def attach_missing_gui_streams() -> tuple[TextIO, ...]:
    """Attach missing stdout/stderr to the OS null device so diagnostics cannot crash a GUI."""

    attached: list[TextIO] = []
    if sys.stdout is None:
        # This process-lifetime stream is retained by ``sys`` and closed during interpreter exit.
        stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        sys.stdout = stdout
        attached.append(stdout)
    if sys.stderr is None:
        stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        sys.stderr = stderr
        attached.append(stderr)
    return tuple(attached)
