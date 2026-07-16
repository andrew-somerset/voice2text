from __future__ import annotations

from pathlib import Path

from voice2text.background import (
    BackgroundStatus,
    LaunchResult,
    RuntimeSignals,
    background_status,
    install_startup,
    launch_background,
    request_background_stop,
    runtime_command,
    startup_command_line,
    uninstall_startup,
)


class FakeRegistry:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def read(self) -> str | None:
        return self.value

    def write(self, command_line: str) -> None:
        self.value = command_line

    def delete(self) -> None:
        self.value = None


class FakeEnvironment:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def read(self, name: str) -> str | None:
        return self.values.get(name)


class FakeChild:
    def __init__(self, polls: list[int | None] | None = None) -> None:
        self.polls = polls or [None]

    def poll(self) -> int | None:
        if len(self.polls) > 1:
            return self.polls.pop(0)
        return self.polls[0]


class FakeSpawner:
    def __init__(self, child: FakeChild | None = None) -> None:
        self.child = child or FakeChild()
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def spawn(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> FakeChild:
        self.calls.append((command, cwd, environment))
        return self.child


class FakeEvent:
    def __init__(self, waits: list[bool] | None = None) -> None:
        self.waits = waits or [False]
        self.set_count = 0
        self.reset_count = 0
        self.closed = False

    def set(self) -> None:
        self.set_count += 1

    def reset(self) -> None:
        self.reset_count += 1

    def wait(self, _timeout_seconds: float = 0.0) -> bool:
        if len(self.waits) > 1:
            return self.waits.pop(0)
        return self.waits[0]

    def close(self) -> None:
        self.closed = True


class FakeEvents:
    def __init__(
        self,
        *,
        ready: FakeEvent | None = None,
        stop: FakeEvent | None = None,
        open_stop: bool = True,
    ) -> None:
        self.ready = ready or FakeEvent()
        self.stop = stop or FakeEvent()
        self.open_stop = open_stop

    def create(self, name: str) -> FakeEvent:
        return self.ready if "ready" in name else self.stop

    def open(self, _name: str) -> FakeEvent | None:
        return self.stop if self.open_stop else None


def test_runtime_command_uses_pythonw_without_console(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    pythonw.write_bytes(b"")

    command = runtime_command(executable=python, frozen=False)

    assert command == (str(pythonw.resolve()), "-m", "voice2text")
    assert "pythonw.exe" in startup_command_line(command)


def test_frozen_runtime_command_uses_current_executable(tmp_path: Path) -> None:
    executable = tmp_path / "voice2text.exe"

    assert runtime_command(executable=executable, frozen=True) == (str(executable.resolve()),)


def test_startup_registry_install_and_uninstall() -> None:
    registry = FakeRegistry()

    install_startup(registry=registry)
    assert registry.read() == startup_command_line()
    uninstall_startup(registry=registry)
    assert registry.read() is None


def test_background_status_reports_running_and_current_registration() -> None:
    expected = startup_command_line()
    status = background_status(
        registry=FakeRegistry(expected),
        running_probe=lambda: True,
    )

    assert status == BackgroundStatus(True, True, True)


def test_launch_returns_already_running_without_spawning() -> None:
    spawner = FakeSpawner()

    result = launch_background(running_probe=lambda: True, spawner=spawner)

    assert result is LaunchResult.ALREADY_RUNNING
    assert spawner.calls == []


def test_launch_waits_for_real_runtime_readiness(tmp_path: Path) -> None:
    ready = FakeEvent([False, True])
    spawner = FakeSpawner()

    result = launch_background(
        timeout_seconds=1,
        running_probe=lambda: False,
        event_factory=FakeEvents(ready=ready),
        spawner=spawner,
        environment_reader=FakeEnvironment({"VOICE2TEXT_MODEL_PATH": "managed-model"}),
        command=("pythonw.exe", "-m", "voice2text"),
        cwd=tmp_path,
    )

    assert result is LaunchResult.STARTED
    assert ready.reset_count == 1
    assert ready.closed is True
    assert spawner.calls[0][2]["VOICE2TEXT_MODEL_PATH"] == "managed-model"


def test_launch_fails_when_child_exits_before_ready(tmp_path: Path) -> None:
    result = launch_background(
        timeout_seconds=1,
        running_probe=lambda: False,
        event_factory=FakeEvents(ready=FakeEvent([False])),
        spawner=FakeSpawner(FakeChild([1])),
        environment_reader=FakeEnvironment(),
        command=("pythonw.exe", "-m", "voice2text"),
        cwd=tmp_path,
    )

    assert result is LaunchResult.FAILED


def test_stop_request_signals_existing_event_only() -> None:
    stop = FakeEvent()

    assert request_background_stop(event_factory=FakeEvents(stop=stop)) is True
    assert stop.set_count == 1
    assert stop.closed is True
    assert request_background_stop(event_factory=FakeEvents(open_stop=False)) is False


def test_runtime_signals_reset_ready_and_stop_then_publish_ready() -> None:
    ready = FakeEvent()
    stop = FakeEvent([False, True])
    signals = RuntimeSignals(event_factory=FakeEvents(ready=ready, stop=stop))

    assert ready.reset_count == 1
    assert stop.reset_count == 1
    signals.mark_ready()
    assert ready.set_count == 1
    assert signals.stop_requested() is False
    assert signals.stop_requested() is True
    signals.close()
    assert ready.reset_count == 2
    assert ready.closed is True
    assert stop.closed is True
