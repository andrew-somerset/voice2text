"""Narrow Windows Raw Input adapter for one configured trigger key."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, ClassVar

from voice2text.config import TriggerConfig

LOGGER = logging.getLogger(__name__)

RIM_TYPEKEYBOARD = 1
RID_INPUT = 0x10000003
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
RI_KEY_BREAK = 0x0001
RI_KEY_E0 = 0x0002
RI_KEY_E1 = 0x0004
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_KEYBOARD = 0x06

_LRESULT = ctypes.c_ssize_t
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDPROC = _WINFUNCTYPE(
    _LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSW(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RAWKEYBOARD(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class _RAWINPUT_KEYBOARD(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("header", _RAWINPUTHEADER),
        ("keyboard", _RAWKEYBOARD),
    ]


@dataclass(frozen=True, slots=True)
class TriggerTransition:
    """A de-duplicated transition for the configured trigger only."""

    is_down: bool
    timestamp_ns: int


class TriggerFilter:
    """Discard unrelated Raw Input data immediately and de-duplicate repeats."""

    def __init__(self, config: TriggerConfig | None = None) -> None:
        self._config = config or TriggerConfig()
        self._is_down = False

    @property
    def is_down(self) -> bool:
        return self._is_down

    def process(
        self,
        *,
        make_code: int,
        flags: int,
        timestamp_ns: int,
    ) -> TriggerTransition | None:
        """Return a transition only when the configured physical key changes state."""

        if timestamp_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")
        is_extended = bool(flags & (RI_KEY_E0 | RI_KEY_E1))
        if make_code != self._config.scan_code or is_extended != self._config.extended:
            return None

        is_down = not bool(flags & RI_KEY_BREAK)
        if is_down == self._is_down:
            return None

        self._is_down = is_down
        return TriggerTransition(is_down=is_down, timestamp_ns=timestamp_ns)


class WindowsTriggerListener:
    """Receive background Raw Input on a dedicated message thread."""

    def __init__(
        self,
        on_transition: Callable[[TriggerTransition], None],
        config: TriggerConfig | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("Windows Raw Input is available only on Windows")
        self._on_transition = on_transition
        self._filter = TriggerFilter(config)
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._startup_error: BaseException | None = None
        self._window: int | None = None
        self._class_name = f"Voice2TextRawInput_{os.getpid()}_{id(self):x}"
        self._wnd_proc_ref = _WNDPROC(self._window_proc)
        self._win32 = _Win32Bindings()

    def start(self, timeout_seconds: float = 5.0) -> None:
        """Create the message-only window and register for keyboard Raw Input."""

        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._message_loop,
            name="voice2text-raw-input",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            raise TimeoutError("Windows Raw Input listener did not start in time")
        if self._startup_error is not None:
            raise RuntimeError(
                "Windows Raw Input listener failed to start"
            ) from self._startup_error

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Stop the message loop and release the Raw Input registration."""

        thread = self._thread
        if thread is None:
            return
        if self._thread_id is not None and not self._win32.user32.PostThreadMessageW(
            self._thread_id, WM_QUIT, 0, 0
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "PostThreadMessageW failed")
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise TimeoutError("Windows Raw Input listener did not stop in time")
        self._thread = None

    def __enter__(self) -> WindowsTriggerListener:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _message_loop(self) -> None:
        class_registered = False
        try:
            self._thread_id = int(self._win32.kernel32.GetCurrentThreadId())
            instance = self._win32.kernel32.GetModuleHandleW(None)
            window_class = _WNDCLASSW(
                lpfnWndProc=self._wnd_proc_ref,
                hInstance=instance,
                lpszClassName=self._class_name,
            )
            if not self._win32.user32.RegisterClassW(ctypes.byref(window_class)):
                error = ctypes.get_last_error()
                raise OSError(error, "RegisterClassW failed")
            class_registered = True

            hwnd_message = ctypes.c_void_p(-3)
            window = self._win32.user32.CreateWindowExW(
                0,
                self._class_name,
                self._class_name,
                0,
                0,
                0,
                0,
                0,
                hwnd_message,
                None,
                instance,
                None,
            )
            if not window:
                error = ctypes.get_last_error()
                raise OSError(error, "CreateWindowExW failed")
            self._window = int(window)

            device = _RAWINPUTDEVICE(
                HID_USAGE_PAGE_GENERIC,
                HID_USAGE_GENERIC_KEYBOARD,
                RIDEV_INPUTSINK,
                window,
            )
            if not self._win32.user32.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(_RAWINPUTDEVICE)
            ):
                error = ctypes.get_last_error()
                raise OSError(error, "RegisterRawInputDevices failed")

            self._ready.set()
            message = wintypes.MSG()
            while True:
                result = self._win32.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    error = ctypes.get_last_error()
                    raise OSError(error, "GetMessageW failed")
                if result == 0:
                    break
                self._win32.user32.TranslateMessage(ctypes.byref(message))
                self._win32.user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
                self._ready.set()
            else:
                LOGGER.exception("Windows Raw Input message loop failed")
        finally:
            self._unregister_raw_input()
            if self._window:
                self._win32.user32.DestroyWindow(self._window)
                self._window = None
            if class_registered:
                instance = self._win32.kernel32.GetModuleHandleW(None)
                self._win32.user32.UnregisterClassW(self._class_name, instance)
            self._thread_id = None

    def _unregister_raw_input(self) -> None:
        if not self._window:
            return
        device = _RAWINPUTDEVICE(
            HID_USAGE_PAGE_GENERIC,
            HID_USAGE_GENERIC_KEYBOARD,
            RIDEV_REMOVE,
            None,
        )
        if not self._win32.user32.RegisterRawInputDevices(
            ctypes.byref(device), 1, ctypes.sizeof(_RAWINPUTDEVICE)
        ):
            LOGGER.warning("Could not remove Windows Raw Input registration")

    def _window_proc(
        self,
        window: int,
        message: int,
        w_param: int,
        l_param: int,
    ) -> int:
        if message == WM_INPUT:
            try:
                self._process_raw_input(l_param)
            except Exception:
                LOGGER.exception("Failed to process configured trigger input")
        return int(self._win32.user32.DefWindowProcW(window, message, w_param, l_param))

    def _process_raw_input(self, raw_input_handle: int) -> None:
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        result = self._win32.user32.GetRawInputData(
            raw_input_handle,
            RID_INPUT,
            None,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            raise OSError(error, "GetRawInputData size query failed")
        if size.value < ctypes.sizeof(_RAWINPUT_KEYBOARD):
            return

        buffer = ctypes.create_string_buffer(size.value)
        result = self._win32.user32.GetRawInputData(
            raw_input_handle,
            RID_INPUT,
            buffer,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            raise OSError(error, "GetRawInputData failed")

        raw_input = ctypes.cast(buffer, ctypes.POINTER(_RAWINPUT_KEYBOARD)).contents
        if raw_input.header.dwType != RIM_TYPEKEYBOARD:
            return
        transition = self._filter.process(
            make_code=int(raw_input.keyboard.MakeCode),
            flags=int(raw_input.keyboard.Flags),
            timestamp_ns=time.monotonic_ns(),
        )
        if transition is not None:
            self._on_transition(transition)


class _Win32Bindings:
    """Typed Win32 function bindings kept private to this module."""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.DefWindowProcW.restype = _LRESULT
        self.user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(_RAWINPUTDEVICE),
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.RegisterRawInputDevices.restype = wintypes.BOOL
        self.user32.GetRawInputData.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.UINT),
            wintypes.UINT,
        ]
        self.user32.GetRawInputData.restype = wintypes.UINT
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.GetMessageW.restype = ctypes.c_int
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = _LRESULT
        self.user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def main(argv: list[str] | None = None) -> int:
    """Manually verify that only Right Ctrl transitions are reported."""

    parser = argparse.ArgumentParser(description="Test the Windows trigger listener")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")

    print("Listening for Right Ctrl only; unrelated keys are discarded.")
    with WindowsTriggerListener(lambda transition: print("DOWN" if transition.is_down else "UP")):
        time.sleep(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
