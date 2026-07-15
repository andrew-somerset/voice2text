"""Windows clipboard and balanced `SendInput` Ctrl+V synthesis."""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56


class PasteError(RuntimeError):
    """Raised when Windows cannot safely complete clipboard or input operations."""


class Clipboard(Protocol):
    def read_text(self) -> str | None: ...

    def write_text(self, text: str) -> None: ...

    def clear(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One virtual-key transition used to construct a balanced paste chord."""

    virtual_key: int
    key_up: bool


def paste_key_events() -> tuple[KeyEvent, ...]:
    """Return Ctrl down, V down/up, Ctrl up in a testable order."""

    return (
        KeyEvent(VK_CONTROL, False),
        KeyEvent(VK_V, False),
        KeyEvent(VK_V, True),
        KeyEvent(VK_CONTROL, True),
    )


class WindowsPaster:
    """Paste text without leaving it in the clipboard after the configured delay."""

    def __init__(
        self,
        *,
        clipboard: Clipboard | None = None,
        send_paste: Callable[[], None] | None = None,
        clipboard_delay_seconds: float = 0.05,
        restore_delay_seconds: float = 0.30,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if clipboard_delay_seconds < 0 or restore_delay_seconds < 0:
            raise ValueError("clipboard delays cannot be negative")
        self._clipboard = clipboard or WindowsClipboard()
        self._send_paste = send_paste or send_ctrl_v
        self._clipboard_delay_seconds = clipboard_delay_seconds
        self._restore_delay_seconds = restore_delay_seconds
        self._sleep = sleep
        self._lock = threading.Lock()

    def paste(self, text: str) -> None:
        """Paste once, then restore prior plain text unless another app changed it."""

        if not text:
            raise ValueError("paste text cannot be empty")
        if "\0" in text:
            raise ValueError("paste text cannot contain a NUL character")

        with self._lock:
            previous_text = self._clipboard.read_text()
            self._clipboard.write_text(text)
            try:
                self._sleep(self._clipboard_delay_seconds)
                self._send_paste()
                self._sleep(self._restore_delay_seconds)
            finally:
                if self._clipboard.read_text() == text:
                    if previous_text is None:
                        self._clipboard.clear()
                    else:
                        self._clipboard.write_text(previous_text)


class _KEYBDINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_: ClassVar[tuple[str, ...]] = ("value",)
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("type", wintypes.DWORD),
        ("value", _INPUTUNION),
    ]


def send_ctrl_v() -> None:
    """Inject a balanced Ctrl+V chord through the supported Windows API."""

    if sys.platform != "win32":
        raise OSError("SendInput is available only on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    events = paste_key_events()
    inputs = (_INPUT * len(events))(
        *[
            _INPUT(
                type=INPUT_KEYBOARD,
                ki=_KEYBDINPUT(
                    wVk=event.virtual_key,
                    dwFlags=KEYEVENTF_KEYUP if event.key_up else 0,
                ),
            )
            for event in events
        ]
    )
    sent = int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT)))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise PasteError(
            f"SendInput inserted {sent} of {len(inputs)} events (Windows error {error}); "
            "endpoint policy or integrity-level isolation may be blocking synthetic input"
        )


class WindowsClipboard:
    """Bounded-retry UTF-16 Windows clipboard access for plain text only."""

    def __init__(self, *, attempts: int = 8, retry_delay_seconds: float = 0.025) -> None:
        if sys.platform != "win32":
            raise OSError("the Windows clipboard is available only on Windows")
        if attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("clipboard retry settings are invalid")
        self._attempts = attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def read_text(self) -> str | None:
        with self._opened():
            if not self._user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = self._user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                error = ctypes.get_last_error()
                raise PasteError(f"GetClipboardData failed with Windows error {error}")
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                error = ctypes.get_last_error()
                raise PasteError(f"GlobalLock failed with Windows error {error}")
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)

    def write_text(self, text: str) -> None:
        if "\0" in text:
            raise ValueError("clipboard text cannot contain a NUL character")
        encoded = (text + "\0").encode("utf-16-le")
        handle = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            error = ctypes.get_last_error()
            raise PasteError(f"GlobalAlloc failed with Windows error {error}")
        clipboard_owns_handle = False
        try:
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                error = ctypes.get_last_error()
                raise PasteError(f"GlobalLock failed with Windows error {error}")
            try:
                ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                self._kernel32.GlobalUnlock(handle)

            with self._opened():
                if not self._user32.EmptyClipboard():
                    error = ctypes.get_last_error()
                    raise PasteError(f"EmptyClipboard failed with Windows error {error}")
                if not self._user32.SetClipboardData(CF_UNICODETEXT, handle):
                    error = ctypes.get_last_error()
                    raise PasteError(f"SetClipboardData failed with Windows error {error}")
                clipboard_owns_handle = True
        finally:
            if not clipboard_owns_handle:
                self._kernel32.GlobalFree(handle)

    def clear(self) -> None:
        with self._opened():
            if not self._user32.EmptyClipboard():
                error = ctypes.get_last_error()
                raise PasteError(f"EmptyClipboard failed with Windows error {error}")

    @contextmanager
    def _opened(self) -> Iterator[None]:
        for attempt in range(self._attempts):
            if self._user32.OpenClipboard(None):
                try:
                    yield
                finally:
                    self._user32.CloseClipboard()
                return
            if attempt + 1 < self._attempts:
                time.sleep(self._retry_delay_seconds)
        error = ctypes.get_last_error()
        reason = (
            "access was denied by the desktop session or endpoint policy"
            if error == 5
            else "another process may be holding the clipboard"
        )
        raise PasteError(
            f"OpenClipboard failed after {self._attempts} attempts "
            f"(Windows error {error}); {reason}"
        )

    def _configure_signatures(self) -> None:
        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL
        self._user32.CloseClipboard.argtypes = []
        self._user32.CloseClipboard.restype = wintypes.BOOL
        self._user32.EmptyClipboard.argtypes = []
        self._user32.EmptyClipboard.restype = wintypes.BOOL
        self._user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        self._user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = wintypes.HANDLE
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE
        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self._kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalLock.restype = wintypes.LPVOID
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL
        self._kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalFree.restype = wintypes.HGLOBAL


def main(argv: list[str] | None = None) -> int:
    """Paste explicit text into the currently focused field for manual testing."""

    parser = argparse.ArgumentParser(description="Test Windows clipboard and Ctrl+V")
    parser.add_argument("text")
    args = parser.parse_args(argv)
    print("Pasting into the focused application in 2 seconds...")
    time.sleep(2)
    WindowsPaster().paste(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
