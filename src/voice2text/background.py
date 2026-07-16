"""Current-user background launch, readiness signaling, and sign-in registration."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

from voice2text.instance_lock import is_instance_running

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "voice2text"
_READY_EVENT_NAME = r"Local\voice2text-gm-ready"
_STOP_EVENT_NAME = r"Local\voice2text-gm-stop"
_ERROR_FILE_NOT_FOUND = 2
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_ALLOWED_USER_ENVIRONMENT = (
    "VOICE2TEXT_MODEL_PATH",
    "VOICE2TEXT_MODEL_SHA256",
    "VOICE2TEXT_WHISPER_THREADS",
    "VOICE2TEXT_TRIGGER_CHOICE",
    "VOICE2TEXT_TRIGGER_SCAN_CODE",
    "VOICE2TEXT_TRIGGER_EXTENDED",
    "VOICE2TEXT_TRIGGER_SUPPRESS_CHORDS",
    "VOICE2TEXT_TRIGGER_CHORD_GRACE_SECONDS",
    "VOICE2TEXT_TAP_MAX_SECONDS",
    "VOICE2TEXT_DOUBLE_TAP_WINDOW_SECONDS",
    "VOICE2TEXT_GLEAN_MAX_RECORDING_SECONDS",
)


class BackgroundError(RuntimeError):
    """Background lifecycle operation failed without exposing content."""


class LaunchResult(Enum):
    """Content-free result of requesting a detached listener."""

    STARTED = auto()
    ALREADY_RUNNING = auto()
    FAILED = auto()


class StartupRegistry(Protocol):
    def read(self) -> str | None: ...

    def write(self, command_line: str) -> None: ...

    def delete(self) -> None: ...


class EnvironmentReader(Protocol):
    def read(self, name: str) -> str | None: ...


class ChildProcess(Protocol):
    def poll(self) -> int | None: ...


class ProcessSpawner(Protocol):
    def spawn(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> ChildProcess: ...


class EventHandle(Protocol):
    def set(self) -> None: ...

    def reset(self) -> None: ...

    def wait(self, timeout_seconds: float = 0.0) -> bool: ...

    def close(self) -> None: ...


class EventFactory(Protocol):
    def create(self, name: str) -> EventHandle: ...

    def open(self, name: str) -> EventHandle | None: ...


@dataclass(frozen=True, slots=True)
class BackgroundStatus:
    """Background and startup state without command-line or process details."""

    running: bool
    startup_installed: bool
    startup_current: bool


def runtime_command(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> tuple[str, ...]:
    """Return the console-free command used by detached and sign-in launches."""

    current = (executable or Path(sys.executable)).resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return (str(current),)
    pythonw = current.with_name("pythonw.exe")
    if not pythonw.is_file():
        raise BackgroundError("The selected Python environment has no pythonw.exe")
    return (str(pythonw), "-m", "voice2text")


def startup_command_line(command: tuple[str, ...] | None = None) -> str:
    """Quote one command for the current-user Windows Run registry value."""

    return subprocess.list2cmdline(command or runtime_command())


def install_startup(*, registry: StartupRegistry | None = None) -> None:
    """Register the verified local runtime for the signed-in user only."""

    (registry or _WindowsRunRegistry()).write(startup_command_line())


def uninstall_startup(*, registry: StartupRegistry | None = None) -> None:
    """Remove the signed-in user's startup registration if present."""

    (registry or _WindowsRunRegistry()).delete()


def background_status(
    *,
    registry: StartupRegistry | None = None,
    running_probe: Callable[[], bool] = is_instance_running,
) -> BackgroundStatus:
    """Return whether the runtime is active and whether startup matches this build."""

    installed_command = (registry or _WindowsRunRegistry()).read()
    expected = startup_command_line()
    return BackgroundStatus(
        running=running_probe(),
        startup_installed=installed_command is not None,
        startup_current=installed_command == expected,
    )


def launch_background(
    *,
    timeout_seconds: float = 30.0,
    running_probe: Callable[[], bool] = is_instance_running,
    event_factory: EventFactory | None = None,
    spawner: ProcessSpawner | None = None,
    environment_reader: EnvironmentReader | None = None,
    command: tuple[str, ...] | None = None,
    cwd: Path | None = None,
) -> LaunchResult:
    """Launch through pythonw and wait until the real listener reports readiness."""

    if timeout_seconds <= 0:
        raise ValueError("background launch timeout must be positive")
    if running_probe():
        return LaunchResult.ALREADY_RUNNING

    events = event_factory or _WindowsEventFactory()
    ready = events.create(_READY_EVENT_NAME)
    ready.reset()
    environment = _background_environment(environment_reader or _WindowsUserEnvironment())
    child = (spawner or _WindowsProcessSpawner()).spawn(
        command or runtime_command(),
        cwd=(cwd or Path(__file__).resolve().parents[2]),
        environment=environment,
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return LaunchResult.FAILED
            if ready.wait(min(0.25, remaining)):
                return LaunchResult.STARTED
            if child.poll() is not None:
                return LaunchResult.FAILED
    finally:
        ready.close()


def request_background_stop(*, event_factory: EventFactory | None = None) -> bool:
    """Signal the current user's runtime to shut down cleanly."""

    event = (event_factory or _WindowsEventFactory()).open(_STOP_EVENT_NAME)
    if event is None:
        return False
    try:
        event.set()
        return True
    finally:
        event.close()


class RuntimeSignals:
    """Named readiness and stop events owned by the persistent runtime."""

    def __init__(self, *, event_factory: EventFactory | None = None) -> None:
        events = event_factory or _WindowsEventFactory()
        self._ready = events.create(_READY_EVENT_NAME)
        self._stop = events.create(_STOP_EVENT_NAME)
        self._ready.reset()
        self._stop.reset()

    def mark_ready(self) -> None:
        self._ready.set()

    def stop_requested(self) -> bool:
        return self._stop.wait(0.0)

    def close(self) -> None:
        self._ready.reset()
        self._ready.close()
        self._stop.close()


def _background_environment(reader: EnvironmentReader) -> dict[str, str]:
    environment = os.environ.copy()
    for name in _ALLOWED_USER_ENVIRONMENT:
        value = reader.read(name)
        if value:
            environment[name] = value
    return environment


class _WindowsRunRegistry:
    def read(self) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                value, value_type = winreg.QueryValueEx(key, _RUN_VALUE)
        except FileNotFoundError:
            return None
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
            raise BackgroundError("The existing startup registration has an invalid value")
        return value

    def write(self, command_line: str) -> None:
        import winreg

        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                access=winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command_line)
        except OSError as exc:
            raise BackgroundError("Could not register voice2text for user sign-in") from exc

    def delete(self) -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                access=winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, _RUN_VALUE)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackgroundError("Could not remove the user startup registration") from exc


class _WindowsUserEnvironment:
    def read(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, value_type = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
            return None
        return os.path.expandvars(value) if value_type == winreg.REG_EXPAND_SZ else value


class _WindowsProcessSpawner:
    def spawn(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> ChildProcess:
        creation_flags = 0x00000008 | 0x00000200 | 0x08000000
        try:
            return subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise BackgroundError("Could not start the background listener") from exc


class _WindowsEventFactory:
    def create(self, name: str) -> EventHandle:
        return _WindowsNamedEvent.create(name)

    def open(self, name: str) -> EventHandle | None:
        return _WindowsNamedEvent.open(name)


class _WindowsNamedEvent:
    def __init__(self, handle: int, kernel32: ctypes.WinDLL) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def create(cls, name: str) -> _WindowsNamedEvent:
        kernel32 = cls._bindings()
        handle = kernel32.CreateEventW(None, True, False, name)
        if not handle:
            error = ctypes.get_last_error()
            raise BackgroundError(f"Could not create lifecycle event (Windows error {error})")
        return cls(int(handle), kernel32)

    @classmethod
    def open(cls, name: str) -> _WindowsNamedEvent | None:
        kernel32 = cls._bindings()
        handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE | _SYNCHRONIZE, False, name)
        if not handle:
            error = ctypes.get_last_error()
            if error == _ERROR_FILE_NOT_FOUND:
                return None
            raise BackgroundError(f"Could not open lifecycle event (Windows error {error})")
        return cls(int(handle), kernel32)

    def set(self) -> None:
        if not self._kernel32.SetEvent(self._handle):
            error = ctypes.get_last_error()
            raise BackgroundError(f"Could not signal lifecycle event (Windows error {error})")

    def reset(self) -> None:
        if not self._kernel32.ResetEvent(self._handle):
            error = ctypes.get_last_error()
            raise BackgroundError(f"Could not reset lifecycle event (Windows error {error})")

    def wait(self, timeout_seconds: float = 0.0) -> bool:
        if timeout_seconds < 0:
            milliseconds = _INFINITE
        else:
            milliseconds = min(round(timeout_seconds * 1_000), _INFINITE - 1)
        result = int(self._kernel32.WaitForSingleObject(self._handle, milliseconds))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        error = ctypes.get_last_error()
        raise BackgroundError(f"Could not wait for lifecycle event (Windows error {error})")

    def close(self) -> None:
        handle, self._handle = self._handle, 0
        if handle and not self._kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise BackgroundError(f"Could not close lifecycle event (Windows error {error})")

    @staticmethod
    def _bindings() -> ctypes.WinDLL:
        if sys.platform != "win32":
            raise OSError("Windows lifecycle events are available only on Windows")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
        kernel32.ResetEvent.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32
