"""Small Windows notification-area control for settings and clean shutdown."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from voice2text.background import runtime_command

LOGGER = logging.getLogger(__name__)


class TrayError(RuntimeError):
    """The optional notification-area control could not be started."""


def settings_command(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> tuple[str, ...]:
    """Return the console-free command that opens the guided settings wizard."""

    return (*runtime_command(executable=executable, frozen=frozen), "--settings")


def launch_settings_window(
    *,
    command: tuple[str, ...] | None = None,
    cwd: Path | None = None,
) -> None:
    """Open settings in a separate process so the resident listener remains responsive."""

    selected_command = command or settings_command()
    is_frozen = bool(getattr(sys, "frozen", False))
    working_directory = cwd or (
        Path(sys.executable).resolve().parent if is_frozen else Path(__file__).resolve().parents[2]
    )
    creation_flags = 0x00000008 | 0x00000200 | 0x08000000
    try:
        subprocess.Popen(
            selected_command,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise TrayError("Could not open Voice2Text settings") from exc


class TrayIcon:
    """Run a content-free notification-area icon while local dictation is resident."""

    def __init__(
        self,
        *,
        trigger_name: str,
        on_exit: Callable[[], None],
        settings_launcher: Callable[[], None] = launch_settings_window,
    ) -> None:
        self._trigger_name = trigger_name
        self._on_exit = on_exit
        self._settings_launcher = settings_launcher
        self._icon: Any | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Create the tray icon on pystray's Windows event thread."""

        with self._lock:
            if self._icon is not None:
                return
            try:
                import pystray
                from PIL import Image, ImageDraw
            except ImportError as exc:
                raise TrayError("Notification-area dependencies are unavailable") from exc

            image = Image.new("RGBA", (64, 64), (17, 24, 39, 255))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((5, 5, 59, 59), radius=14, fill=(37, 99, 235, 255))
            heights = (18, 30, 42, 52, 40, 28, 16)
            for index, height in enumerate(heights):
                x = 14 + index * 6
                top = 32 - height // 2
                draw.rounded_rectangle(
                    (x, top, x + 3, top + height),
                    radius=2,
                    fill=(255, 255, 255, 255),
                )

            def open_settings(_icon: Any, _item: Any) -> None:
                try:
                    self._settings_launcher()
                except TrayError:
                    LOGGER.error("Voice2Text settings could not be opened")

            def exit_runtime(icon: Any, _item: Any) -> None:
                self._on_exit()
                icon.stop()

            menu = pystray.Menu(
                pystray.MenuItem(
                    f"Voice2Text — hold {self._trigger_name}",
                    lambda _icon, _item: None,
                    enabled=False,
                ),
                pystray.MenuItem("Settings...", open_settings, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit until next sign-in", exit_runtime),
            )
            self._icon = pystray.Icon(
                "voice2text",
                image,
                "Voice2Text — local dictation ready",
                menu,
            )
            try:
                self._icon.run_detached()
            except Exception as exc:
                self._icon = None
                raise TrayError("Notification-area icon could not start") from exc

    def stop(self) -> None:
        """Remove the notification-area icon without changing sign-in startup."""

        with self._lock:
            icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                LOGGER.warning("Notification-area icon did not stop cleanly")
