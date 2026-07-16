"""Current-session single-instance protection for Windows trigger listeners."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Protocol

_ERROR_ALREADY_EXISTS = 183
_DEFAULT_MUTEX_NAME = "Local\\voice2text-gm-windows-client"


class InstanceLockError(RuntimeError):
    """Windows could not create or release the single-instance lock."""


class MutexBindings(Protocol):
    def create(self, name: str) -> tuple[int, int]: ...

    def close(self, handle: int) -> None: ...


class SingleInstanceLock:
    """Hold one named mutex so duplicate listeners cannot duplicate a paste."""

    def __init__(
        self,
        *,
        name: str = _DEFAULT_MUTEX_NAME,
        bindings: MutexBindings | None = None,
    ) -> None:
        if not name.startswith("Local\\") or len(name) > 240 or "\0" in name:
            raise ValueError("single-instance mutex name is invalid")
        self._name = name
        self._bindings = bindings or _Win32MutexBindings()
        self._handle: int | None = None

    def acquire(self) -> bool:
        """Return False when another process in this user session already owns the mutex."""

        if self._handle is not None:
            return True
        handle, error = self._bindings.create(self._name)
        if not handle:
            raise InstanceLockError(f"Could not create the instance lock (Windows error {error})")
        if error == _ERROR_ALREADY_EXISTS:
            self._bindings.close(handle)
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        """Release this process's mutex handle."""

        handle, self._handle = self._handle, None
        if handle is not None:
            self._bindings.close(handle)

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise InstanceLockError("voice2text is already running")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def is_instance_running(*, bindings: MutexBindings | None = None) -> bool:
    """Probe the current session without retaining the mutex when no app is running."""

    lock = SingleInstanceLock(bindings=bindings)
    if not lock.acquire():
        return True
    lock.close()
    return False


class _Win32MutexBindings:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("named Windows mutexes are available only on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def create(self, name: str) -> tuple[int, int]:
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, True, name)
        return (int(handle) if handle else 0, ctypes.get_last_error())

    def close(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise InstanceLockError(f"Could not close the instance lock (Windows error {error})")
