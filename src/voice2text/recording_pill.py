"""Compact thread-owned recording indicator with a privacy-safe volume meter."""

from __future__ import annotations

import gc
import math
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any

_PILL_WIDTH = 390
_PILL_HEIGHT = 64
_TRANSPARENT_COLOR = "#010203"
_BACKGROUND = "#111827"
_MUTED = "#9ca3af"


class RecordingPillStatus(Enum):
    """User-visible states rendered by the compact pill."""

    HIDDEN = auto()
    LOCAL_RECORDING = auto()
    GLEAN_RECORDING = auto()
    COMPLETE = auto()
    ERROR = auto()


class RecordingPillCommandKind(Enum):
    """Immutable commands accepted from non-UI threads."""

    SHOW_LOCAL = auto()
    SHOW_GLEAN = auto()
    SHOW_COMPLETE = auto()
    SHOW_ERROR = auto()
    SET_LEVEL = auto()
    HIDE = auto()
    SHUTDOWN = auto()


@dataclass(frozen=True, slots=True)
class RecordingPillCommand:
    """One cross-thread pill update containing no audio or transcript content."""

    kind: RecordingPillCommandKind
    trigger_name: str = ""
    message: str = ""
    level: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.level) or not 0.0 <= self.level <= 1.0:
            raise ValueError("recording pill level must be between 0 and 1")
        if len(self.trigger_name) > 64 or any(
            ord(character) < 0x20 for character in self.trigger_name
        ):
            raise ValueError("recording pill trigger name is invalid")
        if len(self.message) > 160 or any(character in "\r\n\0" for character in self.message):
            raise ValueError("recording pill message is invalid")


@dataclass(frozen=True, slots=True)
class RecordingPillState:
    """Complete toolkit-independent render state."""

    status: RecordingPillStatus = RecordingPillStatus.HIDDEN
    title: str = ""
    hint: str = ""
    level: float = 0.0


class RecordingPillModel:
    """Pure reducer for deterministic pill behavior and tests."""

    def __init__(self) -> None:
        self._state = RecordingPillState()

    @property
    def state(self) -> RecordingPillState:
        return self._state

    def apply(self, command: RecordingPillCommand) -> RecordingPillState:
        """Apply one command and return the complete resulting state."""

        if command.kind in {
            RecordingPillCommandKind.HIDE,
            RecordingPillCommandKind.SHUTDOWN,
        }:
            self._state = RecordingPillState()
        elif command.kind is RecordingPillCommandKind.SHOW_LOCAL:
            self._state = RecordingPillState(
                status=RecordingPillStatus.LOCAL_RECORDING,
                title="Recording locally",
                hint=f"Release {command.trigger_name} to finish",
            )
        elif command.kind is RecordingPillCommandKind.SHOW_GLEAN:
            self._state = RecordingPillState(
                status=RecordingPillStatus.GLEAN_RECORDING,
                title="Ask Glean recording",
                hint=f"Tap {command.trigger_name} to stop",
            )
        elif command.kind is RecordingPillCommandKind.SHOW_COMPLETE:
            self._state = RecordingPillState(
                status=RecordingPillStatus.COMPLETE,
                title="Audio captured",
                hint=command.message or "Test audio discarded",
            )
        elif command.kind is RecordingPillCommandKind.SHOW_ERROR:
            self._state = RecordingPillState(
                status=RecordingPillStatus.ERROR,
                title="Microphone unavailable",
                hint=command.message or "Check Windows microphone permissions",
            )
        elif command.kind is RecordingPillCommandKind.SET_LEVEL:
            if self._state.status in {
                RecordingPillStatus.LOCAL_RECORDING,
                RecordingPillStatus.GLEAN_RECORDING,
            }:
                self._state = replace(self._state, level=command.level)
        else:  # pragma: no cover - protects future enum additions
            raise RuntimeError(f"unhandled recording pill command: {command.kind}")
        return self._state


class RecordingPill:
    """Own a small always-on-top Tk pill on a dedicated UI thread."""

    def __init__(self, *, on_cancel: Callable[[], None] | None = None) -> None:
        self._on_cancel = on_cancel or (lambda: None)
        self._commands: queue.Queue[RecordingPillCommand] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._model = RecordingPillModel()
        self._widgets: dict[str, Any] = {}

    def start(self, timeout_seconds: float = 5.0) -> None:
        """Start the UI thread and wait for its hidden window."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="voice2text-recording-pill",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            raise TimeoutError("recording pill did not start in time")
        if self._startup_error is not None:
            raise RuntimeError("recording pill failed to start") from self._startup_error

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Destroy Tk on its UI thread and clear in-memory state."""

        thread = self._thread
        if thread is None:
            return
        self._commands.put(RecordingPillCommand(RecordingPillCommandKind.SHUTDOWN))
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise TimeoutError("recording pill did not stop in time")
        self._thread = None

    def show_local(self, trigger_name: str) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_LOCAL, trigger_name=trigger_name)

    def show_glean(self, trigger_name: str) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_GLEAN, trigger_name=trigger_name)

    def show_complete(self, message: str = "Test audio discarded") -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_COMPLETE, message=message)

    def show_error(self, message: str) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_ERROR, message=message)

    def set_level(self, level: float) -> None:
        normalized = max(0.0, min(1.0, float(level)))
        self._enqueue(RecordingPillCommandKind.SET_LEVEL, level=normalized)

    def hide(self) -> None:
        self._enqueue(RecordingPillCommandKind.HIDE)

    def __enter__(self) -> RecordingPill:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _enqueue(
        self,
        kind: RecordingPillCommandKind,
        *,
        trigger_name: str = "",
        message: str = "",
        level: float = 0.0,
    ) -> None:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("recording pill is not running")
        self._commands.put(
            RecordingPillCommand(
                kind=kind,
                trigger_name=trigger_name,
                message=message,
                level=level,
            )
        )

    def _run(self) -> None:
        root: Any | None = None
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("voice2text recording")
            root.overrideredirect(True)
            root.configure(bg=_TRANSPARENT_COLOR)
            root.attributes("-topmost", True)
            try:
                root.attributes("-alpha", 0.97)
                root.wm_attributes("-transparentcolor", _TRANSPARENT_COLOR)
                root.attributes("-toolwindow", True)
            except tk.TclError:
                pass
            root.withdraw()
            root.bind("<Escape>", lambda _event: self._dismiss())
            self._build_widgets(tk, root)
            self._widgets["root"] = root
            self._ready.set()
            root.after(25, self._poll_commands)
            root.mainloop()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._widgets.clear()
            root = None
            gc.collect()

    def _build_widgets(self, tk: Any, root: Any) -> None:
        root.geometry(f"{_PILL_WIDTH}x{_PILL_HEIGHT}")
        root.resizable(False, False)
        canvas = tk.Canvas(
            root,
            width=_PILL_WIDTH,
            height=_PILL_HEIGHT,
            bg=_TRANSPARENT_COLOR,
            highlightthickness=0,
            borderwidth=0,
        )
        canvas.pack(fill="both", expand=True)
        _rounded_rectangle(canvas, 1, 1, _PILL_WIDTH - 1, _PILL_HEIGHT - 1, 31, _BACKGROUND)
        dot = canvas.create_oval(18, 24, 32, 38, fill="#22c55e", outline="")
        title = canvas.create_text(
            45,
            22,
            text="Recording locally",
            fill="#f9fafb",
            font=("Segoe UI Semibold", 11),
            anchor="w",
        )
        hint = canvas.create_text(
            45,
            43,
            text="Release the trigger to finish",
            fill=_MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        )

        heights = (10, 16, 24, 32, 22, 14, 28, 18, 11)
        bars: list[int] = []
        for index, height in enumerate(heights):
            x = 267 + index * 10
            bars.append(
                canvas.create_line(
                    x,
                    (_PILL_HEIGHT - height) / 2,
                    x,
                    (_PILL_HEIGHT + height) / 2,
                    fill="#374151",
                    width=5,
                    capstyle="round",
                )
            )
        close = canvas.create_text(
            372,
            32,
            text="X",
            fill="#6b7280",
            font=("Segoe UI Semibold", 8),
            tags=("close",),
        )
        canvas.tag_bind("close", "<Button-1>", lambda _event: self._dismiss())
        canvas.tag_bind(
            "close",
            "<Enter>",
            lambda _event: canvas.itemconfigure(close, fill="#f9fafb"),
        )
        canvas.tag_bind(
            "close",
            "<Leave>",
            lambda _event: canvas.itemconfigure(close, fill="#6b7280"),
        )
        self._widgets.update(
            {
                "canvas": canvas,
                "dot": dot,
                "title": title,
                "hint": hint,
                "bars": tuple(bars),
            }
        )

    def _poll_commands(self) -> None:
        root = self._widgets["root"]
        changed = False
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            if command.kind is RecordingPillCommandKind.SHUTDOWN:
                self._model.apply(command)
                root.destroy()
                return
            self._model.apply(command)
            changed = True
        if changed:
            self._render()
        root.after(25, self._poll_commands)

    def _render(self) -> None:
        state = self._model.state
        root = self._widgets["root"]
        if state.status is RecordingPillStatus.HIDDEN:
            root.withdraw()
            return

        accents = {
            RecordingPillStatus.LOCAL_RECORDING: "#22c55e",
            RecordingPillStatus.GLEAN_RECORDING: "#f97316",
            RecordingPillStatus.COMPLETE: "#60a5fa",
            RecordingPillStatus.ERROR: "#ef4444",
        }
        accent = accents[state.status]
        canvas = self._widgets["canvas"]
        canvas.itemconfigure(self._widgets["dot"], fill=accent)
        canvas.itemconfigure(self._widgets["title"], text=state.title)
        canvas.itemconfigure(self._widgets["hint"], text=state.hint)

        active_bars = round(state.level * len(self._widgets["bars"]))
        if state.level > 0 and active_bars == 0:
            active_bars = 1
        for index, bar in enumerate(self._widgets["bars"]):
            canvas.itemconfigure(bar, fill=accent if index < active_bars else "#374151")

        root.deiconify()
        root.lift()
        root.update_idletasks()
        x = max(12, (root.winfo_screenwidth() - _PILL_WIDTH) // 2)
        y = max(12, root.winfo_screenheight() - _PILL_HEIGHT - 72)
        root.geometry(f"{_PILL_WIDTH}x{_PILL_HEIGHT}+{x}+{y}")

    def _dismiss(self) -> None:
        self._on_cancel()
        self._model.apply(RecordingPillCommand(RecordingPillCommandKind.HIDE))
        self._render()


def _rounded_rectangle(
    canvas: Any,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    color: str,
) -> None:
    """Draw a filled rounded rectangle using primitives available in Tk 8.6."""

    canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=color, outline="")
    canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=color, outline="")
    canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, fill=color, outline="")
    canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, fill=color, outline="")
    canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, fill=color, outline="")
    canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, fill=color, outline="")
