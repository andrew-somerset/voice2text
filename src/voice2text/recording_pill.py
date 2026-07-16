"""Compact thread-owned recording indicator with a privacy-safe volume meter."""

from __future__ import annotations

import ctypes
import gc
import math
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any

from voice2text.paster import WindowsClipboard

_PILL_WIDTH = 168
_PILL_HEIGHT = 42
_BAR_COUNT = 9
_BAR_WIDTH = 5
_BAR_GAP = 7
_BAR_IDLE = 0.10
_TICK_MILLISECONDS = 33
_TRANSPARENT_COLOR = "#010203"
_BACKGROUND = "#111827"


class RecordingPillStatus(Enum):
    """User-visible states rendered by the compact pill."""

    HIDDEN = auto()
    READY = auto()
    LOCAL_RECORDING = auto()
    GLEAN_RECORDING = auto()
    TRANSCRIBING = auto()
    COMPLETE = auto()
    ERROR = auto()
    PASTE_BLOCKED = auto()


class RecordingPillCommandKind(Enum):
    """Immutable commands accepted from non-UI threads."""

    SHOW_READY = auto()
    SHOW_LOCAL = auto()
    SHOW_GLEAN = auto()
    SHOW_TRANSCRIBING = auto()
    SHOW_PASTED = auto()
    SHOW_NO_SPEECH = auto()
    SHOW_COMPLETE = auto()
    SHOW_ERROR = auto()
    SHOW_PASTE_BLOCKED = auto()
    SET_LEVEL = auto()
    HIDE = auto()
    SHUTDOWN = auto()


@dataclass(frozen=True, slots=True)
class RecordingPillCommand:
    """One cross-thread pill update containing no audio or transcript content."""

    kind: RecordingPillCommandKind
    trigger_name: str = ""
    message: str = ""
    content: str = field(default="", repr=False)
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
        if len(self.content) > 64 * 1024 or "\0" in self.content:
            raise ValueError("recording pill fallback content is invalid")


@dataclass(frozen=True, slots=True)
class RecordingPillState:
    """Complete toolkit-independent render state."""

    status: RecordingPillStatus = RecordingPillStatus.HIDDEN
    title: str = ""
    hint: str = ""
    level: float = 0.0
    content: str = field(default="", repr=False)


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
        elif command.kind is RecordingPillCommandKind.SHOW_READY:
            self._state = RecordingPillState(
                status=RecordingPillStatus.READY,
                title="Voice dictation ready",
                hint=f"Hold {command.trigger_name} to record",
            )
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
        elif command.kind is RecordingPillCommandKind.SHOW_TRANSCRIBING:
            self._state = RecordingPillState(
                status=RecordingPillStatus.TRANSCRIBING,
                title="Transcribing locally",
                hint="Audio stays on this PC",
            )
        elif command.kind is RecordingPillCommandKind.SHOW_PASTED:
            self._state = RecordingPillState(
                status=RecordingPillStatus.COMPLETE,
                title="Text inserted",
                hint="Previous plain-text clipboard restored",
            )
        elif command.kind is RecordingPillCommandKind.SHOW_NO_SPEECH:
            self._state = RecordingPillState(
                status=RecordingPillStatus.COMPLETE,
                title="No speech detected",
                hint="Nothing was pasted",
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
                title="Voice action failed",
                hint=command.message or "Check Windows microphone permissions",
            )
        elif command.kind is RecordingPillCommandKind.SHOW_PASTE_BLOCKED:
            self._state = RecordingPillState(
                status=RecordingPillStatus.PASTE_BLOCKED,
                title="Automatic paste was blocked",
                hint=command.message or "Copy the local transcript manually",
                content=command.content,
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


def _bar_targets(
    status: RecordingPillStatus,
    level: float,
    phase: float,
) -> tuple[float, ...]:
    """Return nine normalized Mac-style bar heights for one animation frame."""

    if not math.isfinite(level) or not 0.0 <= level <= 1.0:
        raise ValueError("visual meter level must be between 0 and 1")
    targets: list[float] = []
    for index in range(_BAR_COUNT):
        if status is RecordingPillStatus.TRANSCRIBING:
            target = 0.22 + 0.18 * (0.5 + 0.5 * math.sin(phase * 1.4 + index * 0.9))
        else:
            oscillation = 0.5 + 0.5 * math.sin(phase + index * 0.7)
            target = _BAR_IDLE + (0.15 + 0.85 * oscillation) * level
        targets.append(max(0.05, min(1.0, target)))
    return tuple(targets)


class RecordingPill:
    """Own a small always-on-top Tk pill on a dedicated UI thread."""

    def __init__(
        self,
        *,
        on_cancel: Callable[[], None] | None = None,
        clipboard_factory: Callable[[], WindowsClipboard] = WindowsClipboard,
    ) -> None:
        self._on_cancel = on_cancel or (lambda: None)
        self._clipboard_factory = clipboard_factory
        self._commands: queue.Queue[RecordingPillCommand] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._model = RecordingPillModel()
        self._widgets: dict[str, Any] = {}
        self._phase = 0.0
        self._visual_level = 0.0
        self._bar_levels = [_BAR_IDLE] * _BAR_COUNT

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

    def show_ready(self, trigger_name: str) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_READY, trigger_name=trigger_name)

    def show_glean(self, trigger_name: str) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_GLEAN, trigger_name=trigger_name)

    def show_transcribing(self) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_TRANSCRIBING)

    def show_pasted(self) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_PASTED)

    def show_no_speech(self) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_NO_SPEECH)

    def show_complete(self, message: str = "Test audio discarded") -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_COMPLETE, message=message)

    def show_error(self, message: str) -> None:
        self._enqueue(RecordingPillCommandKind.SHOW_ERROR, message=message)

    def show_paste_blocked(self, text: str, reason: str) -> None:
        self._enqueue(
            RecordingPillCommandKind.SHOW_PASTE_BLOCKED,
            message=reason,
            content=text,
        )

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
        content: str = "",
        level: float = 0.0,
    ) -> None:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("recording pill is not running")
        self._commands.put(
            RecordingPillCommand(
                kind=kind,
                trigger_name=trigger_name,
                message=message,
                content=content,
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
            root.update_idletasks()
            _make_window_no_activate(root)
            self._ready.set()
            root.after(_TICK_MILLISECONDS, self._poll_commands)
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
        _rounded_rectangle(
            canvas,
            1,
            1,
            _PILL_WIDTH - 1,
            _PILL_HEIGHT - 1,
            _PILL_HEIGHT // 2,
            _BACKGROUND,
        )

        total_width = _BAR_COUNT * _BAR_WIDTH + (_BAR_COUNT - 1) * _BAR_GAP
        x_start = (_PILL_WIDTH - total_width) / 2 - 5
        bars: list[int] = []
        for index in range(_BAR_COUNT):
            x = x_start + index * (_BAR_WIDTH + _BAR_GAP)
            bars.append(
                canvas.create_line(
                    x,
                    _PILL_HEIGHT / 2 - 2,
                    x,
                    _PILL_HEIGHT / 2 + 2,
                    fill="#ffffff",
                    width=_BAR_WIDTH,
                    capstyle="round",
                )
            )
        close = canvas.create_text(
            _PILL_WIDTH - 13,
            _PILL_HEIGHT / 2,
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
                "bars": tuple(bars),
            }
        )
        self._build_fallback_widgets(tk, root)

    def _build_fallback_widgets(self, tk: Any, root: Any) -> None:
        fallback = tk.Toplevel(root)
        fallback.title("voice2text - paste blocked")
        fallback.configure(bg="#111827")
        fallback.attributes("-topmost", True)
        fallback.resizable(False, False)
        fallback.withdraw()
        fallback.protocol("WM_DELETE_WINDOW", self._hide_fallback)

        shell = tk.Frame(fallback, bg="#111827", padx=20, pady=18)
        shell.pack(fill="both", expand=True)
        title = tk.Label(
            shell,
            text="Automatic paste was blocked",
            fg="#f9fafb",
            bg="#111827",
            font=("Segoe UI Semibold", 13),
            anchor="w",
        )
        title.pack(fill="x")
        reason = tk.Label(
            shell,
            text="",
            fg="#fca5a5",
            bg="#111827",
            font=("Segoe UI", 9),
            anchor="w",
            pady=6,
        )
        reason.pack(fill="x")
        text = tk.Text(
            shell,
            width=52,
            height=7,
            wrap="word",
            fg="#e5e7eb",
            bg="#1f2937",
            relief="flat",
            padx=10,
            pady=9,
            font=("Segoe UI", 10),
            state="disabled",
        )
        text.pack(fill="both", expand=True, pady=(4, 12))
        actions = tk.Frame(shell, bg="#111827")
        actions.pack(fill="x")
        close = tk.Button(
            actions,
            text="Close",
            command=self._hide_fallback,
            fg="#ffffff",
            bg="#374151",
            activebackground="#4b5563",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=6,
        )
        close.pack(side="right", padx=(8, 0))
        copy = tk.Button(
            actions,
            text="Copy",
            command=self._copy_fallback,
            fg="#ffffff",
            bg="#2563eb",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=6,
        )
        copy.pack(side="right")
        self._widgets.update(
            {
                "fallback": fallback,
                "fallback_reason": reason,
                "fallback_text": text,
                "fallback_copy": copy,
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
        if changed or self._model.state.status is not RecordingPillStatus.HIDDEN:
            self._tick_visual()
            self._render()
        root.after(_TICK_MILLISECONDS, self._poll_commands)

    def _tick_visual(self) -> None:
        state = self._model.state
        self._phase += 0.22
        if state.status in {
            RecordingPillStatus.LOCAL_RECORDING,
            RecordingPillStatus.GLEAN_RECORDING,
        }:
            target_level = min(1.0, (state.level**0.55) * 1.35)
        elif state.status is RecordingPillStatus.TRANSCRIBING:
            target_level = 0.38
        elif state.status is RecordingPillStatus.READY:
            target_level = 0.18
        elif state.status is RecordingPillStatus.COMPLETE:
            target_level = 0.28
        elif state.status is RecordingPillStatus.ERROR:
            target_level = 0.20
        else:
            target_level = 0.0
        self._visual_level += (target_level - self._visual_level) * 0.55

        targets = _bar_targets(state.status, self._visual_level, self._phase)
        for index, target in enumerate(targets):
            self._bar_levels[index] += (target - self._bar_levels[index]) * 0.60

    def _render(self) -> None:
        state = self._model.state
        root = self._widgets["root"]
        fallback = self._widgets["fallback"]
        if state.status is RecordingPillStatus.PASTE_BLOCKED:
            root.withdraw()
            text = self._widgets["fallback_text"]
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", state.content)
            text.configure(state="disabled")
            self._widgets["fallback_reason"].configure(text=state.hint)
            self._widgets["fallback_copy"].configure(text="Copy")
            fallback.deiconify()
            fallback.lift()
            fallback.update_idletasks()
            x = max(20, (fallback.winfo_screenwidth() - fallback.winfo_width()) // 2)
            y = max(20, (fallback.winfo_screenheight() - fallback.winfo_height()) // 2)
            fallback.geometry(f"+{x}+{y}")
            return
        fallback.withdraw()
        if state.status is RecordingPillStatus.HIDDEN:
            root.withdraw()
            return

        accents = {
            RecordingPillStatus.READY: "#60a5fa",
            RecordingPillStatus.LOCAL_RECORDING: "#ffffff",
            RecordingPillStatus.GLEAN_RECORDING: "#f97316",
            RecordingPillStatus.TRANSCRIBING: "#a78bfa",
            RecordingPillStatus.COMPLETE: "#22c55e",
            RecordingPillStatus.ERROR: "#ef4444",
        }
        accent = accents[state.status]
        canvas = self._widgets["canvas"]
        center_y = _PILL_HEIGHT / 2
        max_height = _PILL_HEIGHT - 14
        for index, bar in enumerate(self._widgets["bars"]):
            height = max(3.0, self._bar_levels[index] * max_height)
            coordinates = canvas.coords(bar)
            x = coordinates[0]
            canvas.coords(bar, x, center_y - height / 2, x, center_y + height / 2)
            canvas.itemconfigure(bar, fill=accent)

        root.deiconify()
        root.update_idletasks()
        x = max(12, (root.winfo_screenwidth() - _PILL_WIDTH) // 2)
        y = max(12, root.winfo_screenheight() - _PILL_HEIGHT - 72)
        root.geometry(f"{_PILL_WIDTH}x{_PILL_HEIGHT}+{x}+{y}")

    def _dismiss(self) -> None:
        self._on_cancel()
        self._model.apply(RecordingPillCommand(RecordingPillCommandKind.HIDE))
        self._render()

    def _copy_fallback(self) -> None:
        text = self._model.state.content
        if not text:
            return
        try:
            self._clipboard_factory().write_text(text)
            self._widgets["fallback_copy"].configure(text="Copied")
            self._widgets["fallback_reason"].configure(text="Copied - paste only where intended")
        except Exception:
            self._widgets["fallback_reason"].configure(text="Clipboard access was denied")

    def _hide_fallback(self) -> None:
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


def _make_window_no_activate(root: Any) -> None:
    """Prevent the Windows pill from becoming the keyboard/paste target when shown."""

    if sys.platform != "win32":
        return
    from ctypes import wintypes

    gwl_exstyle = -20
    ws_ex_noactivate = 0x08000000
    ws_ex_toolwindow = 0x00000080
    swp_flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    child_window = int(root.winfo_id())
    root_window = user32.GetAncestor(child_window, 2)
    window = int(root_window) if root_window else child_window
    style = int(user32.GetWindowLongW(window, gwl_exstyle))
    user32.SetWindowLongW(
        window,
        gwl_exstyle,
        style | ws_ex_noactivate | ws_ex_toolwindow,
    )
    user32.SetWindowPos(window, None, 0, 0, 0, 0, swp_flags)
