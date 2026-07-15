"""Pure trigger gesture state machine with no operating-system dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from voice2text.config import TriggerConfig

_NANOSECONDS_PER_SECOND = 1_000_000_000


class InputKind(Enum):
    """Physical trigger transitions and monotonic timer ticks."""

    DOWN = auto()
    UP = auto()
    TIMER = auto()


class GestureEventKind(Enum):
    """Commands emitted for queue consumers outside the input callback."""

    LOCAL_START = auto()
    LOCAL_CANCEL = auto()
    LOCAL_STOP = auto()
    GLEAN_START = auto()
    GLEAN_STOP = auto()
    GLEAN_LIMIT_REACHED = auto()


class GestureState(Enum):
    """Observable state, exposed primarily for diagnostics and tests."""

    IDLE = auto()
    FIRST_PRESS = auto()
    WAITING_SECOND_TAP = auto()
    SECOND_PRESS = auto()
    GLEAN_RECORDING = auto()
    GLEAN_STOP_PRESS = auto()


@dataclass(frozen=True, slots=True)
class GestureInput:
    """One timestamped input to the state machine."""

    kind: InputKind
    timestamp_ns: int

    def __post_init__(self) -> None:
        if self.timestamp_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")


@dataclass(frozen=True, slots=True)
class GestureEvent:
    """One immutable command emitted by the state machine."""

    kind: GestureEventKind
    timestamp_ns: int
    duration_ns: int | None = None


class GestureStateMachine:
    """Distinguish hold-to-dictate from double-tap/third-tap Ask Glean."""

    def __init__(self, config: TriggerConfig | None = None) -> None:
        self._config = config or TriggerConfig()
        self._tap_max_ns = _seconds_to_ns(self._config.tap_max_seconds)
        self._double_tap_window_ns = _seconds_to_ns(self._config.double_tap_window_seconds)
        self._glean_max_recording_ns = _seconds_to_ns(self._config.glean_max_recording_seconds)
        self._state = GestureState.IDLE
        self._press_started_ns: int | None = None
        self._second_tap_deadline_ns: int | None = None
        self._glean_started_ns: int | None = None
        self._last_timestamp_ns: int | None = None

    @property
    def state(self) -> GestureState:
        return self._state

    @property
    def next_deadline_ns(self) -> int | None:
        """Next timer deadline the platform adapter should arrange to deliver."""

        if self._state is GestureState.WAITING_SECOND_TAP:
            return self._second_tap_deadline_ns
        if self._state is GestureState.GLEAN_RECORDING and self._glean_started_ns is not None:
            return self._glean_started_ns + self._glean_max_recording_ns
        return None

    def handle(self, input_event: GestureInput) -> tuple[GestureEvent, ...]:
        """Process one ordered input and return zero or more commands."""

        timestamp_ns = input_event.timestamp_ns
        if self._last_timestamp_ns is not None and timestamp_ns < self._last_timestamp_ns:
            raise ValueError("gesture inputs must use non-decreasing monotonic timestamps")
        self._last_timestamp_ns = timestamp_ns

        expiration = self._expire_deadline(timestamp_ns)
        if expiration:
            if input_event.kind is InputKind.TIMER:
                return expiration
            return expiration + self._handle_current(input_event)
        return self._handle_current(input_event)

    def _expire_deadline(self, timestamp_ns: int) -> tuple[GestureEvent, ...]:
        if (
            self._state is GestureState.WAITING_SECOND_TAP
            and self._second_tap_deadline_ns is not None
            and timestamp_ns >= self._second_tap_deadline_ns
        ):
            self._enter_idle()
            return ()

        if (
            self._state is GestureState.GLEAN_RECORDING
            and self._glean_started_ns is not None
            and timestamp_ns >= self._glean_started_ns + self._glean_max_recording_ns
        ):
            duration_ns = timestamp_ns - self._glean_started_ns
            self._enter_idle()
            return (
                GestureEvent(
                    GestureEventKind.GLEAN_LIMIT_REACHED,
                    timestamp_ns,
                    duration_ns,
                ),
            )
        return ()

    def _handle_current(self, input_event: GestureInput) -> tuple[GestureEvent, ...]:
        kind = input_event.kind
        timestamp_ns = input_event.timestamp_ns

        if kind is InputKind.TIMER:
            return ()

        if self._state is GestureState.IDLE:
            if kind is InputKind.DOWN:
                self._state = GestureState.FIRST_PRESS
                self._press_started_ns = timestamp_ns
                return (GestureEvent(GestureEventKind.LOCAL_START, timestamp_ns),)
            return ()

        if self._state is GestureState.FIRST_PRESS:
            if kind is InputKind.DOWN:  # Key auto-repeat or a duplicated make event.
                return ()
            return self._finish_first_press(timestamp_ns)

        if self._state is GestureState.WAITING_SECOND_TAP:
            if kind is InputKind.DOWN:
                self._state = GestureState.SECOND_PRESS
                self._press_started_ns = timestamp_ns
                return (GestureEvent(GestureEventKind.LOCAL_START, timestamp_ns),)
            return ()

        if self._state is GestureState.SECOND_PRESS:
            if kind is InputKind.DOWN:
                return ()
            return self._finish_second_press(timestamp_ns)

        if self._state is GestureState.GLEAN_RECORDING:
            if kind is InputKind.UP:
                return ()
            glean_started_ns = self._require_timestamp(
                self._glean_started_ns, "Glean recording start"
            )
            duration_ns = timestamp_ns - glean_started_ns
            self._state = GestureState.GLEAN_STOP_PRESS
            self._glean_started_ns = None
            return (GestureEvent(GestureEventKind.GLEAN_STOP, timestamp_ns, duration_ns),)

        if self._state is GestureState.GLEAN_STOP_PRESS:
            if kind is InputKind.UP:
                self._enter_idle()
            return ()

        raise RuntimeError(f"unhandled gesture state: {self._state}")

    def _finish_first_press(self, timestamp_ns: int) -> tuple[GestureEvent, ...]:
        press_started_ns = self._require_timestamp(self._press_started_ns, "first press")
        duration_ns = timestamp_ns - press_started_ns
        self._press_started_ns = None

        if duration_ns <= self._tap_max_ns:
            self._state = GestureState.WAITING_SECOND_TAP
            self._second_tap_deadline_ns = timestamp_ns + self._double_tap_window_ns
            return (GestureEvent(GestureEventKind.LOCAL_CANCEL, timestamp_ns, duration_ns),)

        self._enter_idle()
        return (GestureEvent(GestureEventKind.LOCAL_STOP, timestamp_ns, duration_ns),)

    def _finish_second_press(self, timestamp_ns: int) -> tuple[GestureEvent, ...]:
        press_started_ns = self._require_timestamp(self._press_started_ns, "second press")
        duration_ns = timestamp_ns - press_started_ns
        self._press_started_ns = None
        self._second_tap_deadline_ns = None

        if duration_ns <= self._tap_max_ns:
            self._state = GestureState.GLEAN_RECORDING
            self._glean_started_ns = timestamp_ns
            return (
                GestureEvent(GestureEventKind.LOCAL_CANCEL, timestamp_ns, duration_ns),
                GestureEvent(GestureEventKind.GLEAN_START, timestamp_ns),
            )

        self._enter_idle()
        return (GestureEvent(GestureEventKind.LOCAL_STOP, timestamp_ns, duration_ns),)

    def _enter_idle(self) -> None:
        self._state = GestureState.IDLE
        self._press_started_ns = None
        self._second_tap_deadline_ns = None
        self._glean_started_ns = None

    @staticmethod
    def _require_timestamp(value: int | None, label: str) -> int:
        if value is None:
            raise RuntimeError(f"missing {label} timestamp")
        return value


def _seconds_to_ns(seconds: float) -> int:
    return round(seconds * _NANOSECONDS_PER_SECOND)


def main() -> int:
    """Small deterministic demonstration for manual inspection."""

    machine = GestureStateMachine()
    sample = (
        GestureInput(InputKind.DOWN, 0),
        GestureInput(InputKind.UP, 100_000_000),
        GestureInput(InputKind.DOWN, 200_000_000),
        GestureInput(InputKind.UP, 280_000_000),
        GestureInput(InputKind.DOWN, 2_000_000_000),
        GestureInput(InputKind.UP, 2_050_000_000),
    )
    for item in sample:
        for event in machine.handle(item):
            print(event.kind.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
