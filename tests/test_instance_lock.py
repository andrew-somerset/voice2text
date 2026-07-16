from __future__ import annotations

import pytest

from voice2text.instance_lock import InstanceLockError, SingleInstanceLock


class FakeMutexBindings:
    def __init__(self, *, handle: int = 99, error: int = 0) -> None:
        self.handle = handle
        self.error = error
        self.created: list[str] = []
        self.closed: list[int] = []

    def create(self, name: str) -> tuple[int, int]:
        self.created.append(name)
        return self.handle, self.error

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_lock_holds_and_releases_one_mutex_handle() -> None:
    bindings = FakeMutexBindings()
    lock = SingleInstanceLock(bindings=bindings)

    assert lock.acquire() is True
    assert lock.acquire() is True
    assert len(bindings.created) == 1
    lock.close()
    assert bindings.closed == [99]


def test_existing_instance_returns_false_and_closes_probe_handle() -> None:
    bindings = FakeMutexBindings(error=183)
    lock = SingleInstanceLock(bindings=bindings)

    assert lock.acquire() is False
    assert bindings.closed == [99]


def test_creation_failure_is_reported_without_handle_content() -> None:
    lock = SingleInstanceLock(bindings=FakeMutexBindings(handle=0, error=5))

    with pytest.raises(InstanceLockError, match="Windows error 5"):
        lock.acquire()


@pytest.mark.parametrize("name", ["Global\\voice2text", "bad\0name", "x" * 241])
def test_mutex_name_is_restricted_to_local_session(name: str) -> None:
    with pytest.raises(ValueError, match="mutex name"):
        SingleInstanceLock(name=name, bindings=FakeMutexBindings())
