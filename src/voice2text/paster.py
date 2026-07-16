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
from enum import Enum, auto
from typing import Any, ClassVar, Protocol

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
WM_CANCELMODE = 0x001F
WM_PASTE = 0x0302
SMTO_ABORTIFHUNG = 0x0002
_DIRECT_PASTE_CLASSES = ("edit", "richedit", "scintilla")


class PasteError(RuntimeError):
    """Raised when Windows cannot safely complete clipboard or input operations."""


class Clipboard(Protocol):
    def read_text(self) -> str | None: ...

    def write_text(self, text: str) -> None: ...

    def clear(self) -> None: ...


class FocusManager(Protocol):
    """Minimal foreground-window surface used to target one explicit paste."""

    def capture(self) -> FocusTarget | None: ...

    def validate(self, target: FocusTarget) -> bool: ...

    def activate(self, target: FocusTarget) -> None: ...

    def paste_clipboard(self, target: FocusTarget) -> bool: ...


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One virtual-key transition used to construct a balanced paste chord."""

    virtual_key: int
    key_up: bool


@dataclass(frozen=True, slots=True)
class FocusTarget:
    """Opaque top-level and focused-child handles captured when dictation begins."""

    foreground_window: int
    focused_control: int

    def __post_init__(self) -> None:
        if self.foreground_window <= 0 or self.focused_control <= 0:
            raise ValueError("focus target handles must be positive")


class PasteMethod(Enum):
    """Content-free delivery path used for one paste attempt."""

    NONE = auto()
    DIRECT_CONTROL = auto()
    SEND_INPUT = auto()


@dataclass(frozen=True, slots=True)
class PasteOutcome:
    """Result of a paste attempt without transcript or target metadata."""

    pasted: bool
    method: PasteMethod = PasteMethod.NONE
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _PendingClipboardRestore:
    generation: int
    original: str | None
    inserted: str


class RestoreTimer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[..., None], tuple[Any, ...]], RestoreTimer]


class _GUITHREADINFO(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


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
        focus_manager: FocusManager | None = None,
        send_paste: Callable[[], None] | None = None,
        clipboard_delay_seconds: float = 0.05,
        restore_delay_seconds: float = 0.30,
        sleep: Callable[[float], None] = time.sleep,
        timer_factory: TimerFactory = threading.Timer,
    ) -> None:
        if clipboard_delay_seconds < 0 or restore_delay_seconds < 0:
            raise ValueError("clipboard delays cannot be negative")
        self._clipboard = clipboard or WindowsClipboard()
        self._focus_manager = focus_manager
        self._send_paste = send_paste or send_ctrl_v
        self._clipboard_delay_seconds = clipboard_delay_seconds
        self._restore_delay_seconds = restore_delay_seconds
        self._sleep = sleep
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._generation = 0
        self._pending_restore: _PendingClipboardRestore | None = None
        self._restore_timer: RestoreTimer | None = None

    def paste(self, text: str, *, target: FocusTarget | None = None) -> PasteOutcome:
        """Paste once and schedule safe restoration of the prior plain-text clipboard."""

        if not text:
            raise ValueError("paste text cannot be empty")
        if "\0" in text:
            raise ValueError("paste text cannot contain a NUL character")

        focus_manager = self._focus_manager
        if target is not None:
            focus_manager = focus_manager or WindowsFocusManager()
            try:
                target_valid = focus_manager.validate(target)
            except Exception:
                target_valid = False
            if not target_valid:
                return PasteOutcome(False, reason="Original text control is no longer available")

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._cancel_restore_timer_locked()
            current_text = self._clipboard.read_text()
            if self._pending_restore is not None and current_text == self._pending_restore.inserted:
                previous_text = self._pending_restore.original
            else:
                previous_text = current_text
            self._pending_restore = _PendingClipboardRestore(
                generation=generation,
                original=previous_text,
                inserted=text,
            )
            self._clipboard.write_text(text)

        try:
            self._sleep(self._clipboard_delay_seconds)
            method = PasteMethod.SEND_INPUT
            if target is not None and focus_manager is not None:
                if focus_manager.paste_clipboard(target):
                    method = PasteMethod.DIRECT_CONTROL
                else:
                    focus_manager.activate(target)
                    self._send_paste()
            else:
                self._send_paste()
        except Exception:
            with self._lock:
                self._restore_clipboard_locked(generation)
            return PasteOutcome(False, reason="Windows blocked focused text insertion")

        with self._lock:
            self._schedule_restore_locked(generation)
        return PasteOutcome(True, method=method)

    def close(self) -> None:
        """Cancel any timer and immediately restore a still-owned clipboard value."""

        with self._lock:
            pending = self._pending_restore
            self._cancel_restore_timer_locked()
            if pending is not None:
                self._restore_clipboard_locked(pending.generation)

    def _schedule_restore_locked(self, generation: int) -> None:
        if self._restore_delay_seconds == 0:
            self._restore_clipboard_locked(generation)
            return
        timer = self._timer_factory(
            self._restore_delay_seconds,
            self._restore_clipboard,
            (generation,),
        )
        timer.daemon = True
        self._restore_timer = timer
        timer.start()

    def _cancel_restore_timer_locked(self) -> None:
        if self._restore_timer is not None:
            self._restore_timer.cancel()
            self._restore_timer = None

    def _restore_clipboard(self, generation: int) -> None:
        with self._lock:
            self._restore_clipboard_locked(generation)

    def _restore_clipboard_locked(self, generation: int) -> None:
        pending = self._pending_restore
        if pending is None or pending.generation != generation:
            return
        self._restore_timer = None
        if self._clipboard.read_text() == pending.inserted:
            if pending.original is None:
                self._clipboard.clear()
            else:
                self._clipboard.write_text(pending.original)
        self._pending_restore = None


class WindowsFocusManager:
    """Capture and safely restore one existing top-level Windows foreground window."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("foreground-window targeting is available only on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetGUIThreadInfo.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(_GUITHREADINFO),
        ]
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        self._user32.IsChild.restype = wintypes.BOOL
        self._user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self._user32.AttachThreadInput.restype = wintypes.BOOL
        self._user32.SetActiveWindow.argtypes = [wintypes.HWND]
        self._user32.SetActiveWindow.restype = wintypes.HWND
        self._user32.SetFocus.argtypes = [wintypes.HWND]
        self._user32.SetFocus.restype = wintypes.HWND
        self._user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        self._user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def capture(self) -> FocusTarget | None:
        """Capture opaque top-level and focused-child handles without titles or content."""

        window = self._user32.GetForegroundWindow()
        if not window:
            return None
        thread_id = int(self._user32.GetWindowThreadProcessId(window, None))
        if not thread_id:
            return None
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        if not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        focused = info.hwndFocus
        if not focused or not self._user32.IsWindow(focused):
            return None
        if focused != window and not self._user32.IsChild(window, focused):
            return None
        return FocusTarget(foreground_window=int(window), focused_control=int(focused))

    def validate(self, target: FocusTarget) -> bool:
        """Check that both opaque handles remain valid and related."""

        window = target.foreground_window
        focused = target.focused_control
        return bool(
            self._user32.IsWindow(window)
            and self._user32.IsWindow(focused)
            and (focused == window or self._user32.IsChild(window, focused))
        )

    def activate(self, target: FocusTarget) -> None:
        """Restore both the original top-level window and its exact focused child control."""

        window = target.foreground_window
        focused = target.focused_control
        if not self.validate(target):
            raise PasteError("The original dictation target is no longer available")
        target_thread = int(self._user32.GetWindowThreadProcessId(window, None))
        current_thread = int(self._kernel32.GetCurrentThreadId())
        if not target_thread:
            raise PasteError("The original dictation target thread is unavailable")

        result = ctypes.c_size_t()
        self._user32.SendMessageTimeoutW(
            window,
            WM_CANCELMODE,
            0,
            0,
            SMTO_ABORTIFHUNG,
            250,
            ctypes.byref(result),
        )

        message = wintypes.MSG()
        self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        attached = False
        try:
            if target_thread != current_thread:
                if not self._user32.AttachThreadInput(current_thread, target_thread, True):
                    raise PasteError("Windows would not attach to the dictation target")
                attached = True
            if not self._user32.SetForegroundWindow(window):
                raise PasteError("Windows would not restore the original dictation target")
            self._user32.SetActiveWindow(window)
            self._user32.SetFocus(focused)
        finally:
            if attached:
                self._user32.AttachThreadInput(current_thread, target_thread, False)

        restored = self._user32.GetForegroundWindow()
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        focused_restored = self._user32.GetGUIThreadInfo(target_thread, ctypes.byref(info))
        if (
            not restored
            or int(restored) != window
            or not focused_restored
            or not info.hwndFocus
            or int(info.hwndFocus) != focused
        ):
            raise PasteError("The original dictation target did not regain focus")

    def paste_clipboard(self, target: FocusTarget) -> bool:
        """Use WM_PASTE for standard edit controls; return False for custom UI toolkits."""

        class_buffer = ctypes.create_unicode_buffer(256)
        copied = int(
            self._user32.GetClassNameW(
                target.focused_control,
                class_buffer,
                len(class_buffer),
            )
        )
        if copied <= 0:
            return False
        class_name = class_buffer.value.lower()
        if not any(name in class_name for name in _DIRECT_PASTE_CLASSES):
            return False
        result = ctypes.c_size_t()
        delivered = self._user32.SendMessageTimeoutW(
            target.focused_control,
            WM_PASTE,
            0,
            0,
            SMTO_ABORTIFHUNG,
            1_000,
            ctypes.byref(result),
        )
        return bool(delivered)


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
