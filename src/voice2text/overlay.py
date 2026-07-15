"""Thread-owned Windows overlay for recording, thinking, answers, and errors."""

from __future__ import annotations

import argparse
import gc
import queue
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol

from voice2text.glean_client import Citation, MockGleanClient
from voice2text.paster import WindowsClipboard


class OverlayStatus(Enum):
    """User-visible overlay states independent of the GUI toolkit."""

    HIDDEN = auto()
    LOCAL_RECORDING = auto()
    GLEAN_RECORDING = auto()
    THINKING = auto()
    ANSWER = auto()
    ERROR = auto()
    LIMIT_CONFIRMATION = auto()


class OverlayCommandKind(Enum):
    """Cross-thread commands consumed exclusively by the UI thread."""

    SHOW_LOCAL_RECORDING = auto()
    SHOW_GLEAN_RECORDING = auto()
    SHOW_THINKING = auto()
    APPEND_ANSWER = auto()
    COMPLETE_ANSWER = auto()
    SHOW_ERROR = auto()
    SHOW_LIMIT_CONFIRMATION = auto()
    HIDE = auto()
    SHUTDOWN = auto()


@dataclass(frozen=True, slots=True)
class OverlayCommand:
    """One immutable update placed on the overlay queue."""

    kind: OverlayCommandKind
    text: str = ""
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class OverlayState:
    """Complete render state, kept in memory only."""

    status: OverlayStatus = OverlayStatus.HIDDEN
    title: str = ""
    message: str = ""
    answer: str = ""
    citations: tuple[Citation, ...] = ()


class OverlayModel:
    """Pure state reducer so UI behavior can be tested without a desktop."""

    def __init__(self) -> None:
        self._state = OverlayState()

    @property
    def state(self) -> OverlayState:
        return self._state

    def apply(self, command: OverlayCommand) -> OverlayState:
        """Apply one command and return the resulting complete view state."""

        if command.kind in {OverlayCommandKind.HIDE, OverlayCommandKind.SHUTDOWN}:
            self._state = OverlayState()
        elif command.kind is OverlayCommandKind.SHOW_LOCAL_RECORDING:
            self._state = OverlayState(
                status=OverlayStatus.LOCAL_RECORDING,
                title="Local dictation",
                message="Listening — release Right Ctrl to transcribe locally",
            )
        elif command.kind is OverlayCommandKind.SHOW_GLEAN_RECORDING:
            self._state = OverlayState(
                status=OverlayStatus.GLEAN_RECORDING,
                title="Ask Glean — recording",
                message="Press Right Ctrl once to stop",
            )
        elif command.kind is OverlayCommandKind.SHOW_THINKING:
            self._state = OverlayState(
                status=OverlayStatus.THINKING,
                title="Ask Glean",
                message="Thinking…",
            )
        elif command.kind is OverlayCommandKind.APPEND_ANSWER:
            self._state = OverlayState(
                status=OverlayStatus.ANSWER,
                title="Ask Glean",
                message="Mock response" if not self._state.answer else self._state.message,
                answer=self._state.answer + command.text,
                citations=self._state.citations,
            )
        elif command.kind is OverlayCommandKind.COMPLETE_ANSWER:
            self._state = OverlayState(
                status=OverlayStatus.ANSWER,
                title="Ask Glean",
                message=self._state.message or "Response complete",
                answer=self._state.answer,
                citations=command.citations,
            )
        elif command.kind is OverlayCommandKind.SHOW_ERROR:
            self._state = OverlayState(
                status=OverlayStatus.ERROR,
                title="Voice request could not be completed",
                message=command.text or "An unexpected error occurred.",
            )
        elif command.kind is OverlayCommandKind.SHOW_LIMIT_CONFIRMATION:
            self._state = OverlayState(
                status=OverlayStatus.LIMIT_CONFIRMATION,
                title="Ask Glean recording limit reached",
                message="Submit the locally recorded question, or discard it?",
            )
        else:  # pragma: no cover - protects future enum additions
            raise RuntimeError(f"unhandled overlay command: {command.kind}")
        return self._state


class ClipboardWriter(Protocol):
    def write_text(self, text: str) -> None: ...


class Overlay:
    """Own a compact Tk overlay on a dedicated UI thread."""

    def __init__(
        self,
        *,
        on_cancel: Callable[[], None] | None = None,
        on_limit_decision: Callable[[bool], None] | None = None,
        clipboard_factory: Callable[[], ClipboardWriter] = WindowsClipboard,
        play_start_sound: Callable[[], None] | None = None,
    ) -> None:
        self._on_cancel = on_cancel or (lambda: None)
        self._on_limit_decision = on_limit_decision or (lambda _submit: None)
        self._clipboard_factory = clipboard_factory
        self._play_start_sound = play_start_sound or _play_default_start_sound
        self._commands: queue.Queue[OverlayCommand] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._model = OverlayModel()
        self._widgets: dict[str, Any] = {}

    def start(self, timeout_seconds: float = 5.0) -> None:
        """Start the UI thread and wait until its hidden window is ready."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="voice2text-overlay",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            raise TimeoutError("overlay did not start in time")
        if self._startup_error is not None:
            raise RuntimeError("overlay failed to start") from self._startup_error

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Stop the UI loop and discard all in-memory answer state."""

        thread = self._thread
        if thread is None:
            return
        self._commands.put(OverlayCommand(OverlayCommandKind.SHUTDOWN))
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise TimeoutError("overlay did not stop in time")
        self._thread = None

    def show_local_recording(self) -> None:
        self._enqueue(OverlayCommandKind.SHOW_LOCAL_RECORDING)

    def show_glean_recording(self) -> None:
        self._enqueue(OverlayCommandKind.SHOW_GLEAN_RECORDING)

    def show_thinking(self) -> None:
        self._enqueue(OverlayCommandKind.SHOW_THINKING)

    def append_answer(self, text_delta: str) -> None:
        if text_delta:
            self._enqueue(OverlayCommandKind.APPEND_ANSWER, text=text_delta)

    def complete_answer(self, citations: tuple[Citation, ...]) -> None:
        self._enqueue(OverlayCommandKind.COMPLETE_ANSWER, citations=citations)

    def show_error(self, message: str) -> None:
        self._enqueue(OverlayCommandKind.SHOW_ERROR, text=message)

    def show_recording_limit(self) -> None:
        self._enqueue(OverlayCommandKind.SHOW_LIMIT_CONFIRMATION)

    def hide(self) -> None:
        self._enqueue(OverlayCommandKind.HIDE)

    def __enter__(self) -> Overlay:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _enqueue(
        self,
        kind: OverlayCommandKind,
        *,
        text: str = "",
        citations: tuple[Citation, ...] = (),
    ) -> None:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("overlay is not running")
        self._commands.put(OverlayCommand(kind=kind, text=text, citations=citations))

    def _run(self) -> None:
        root: Any | None = None
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("voice2text")
            root.configure(bg="#111827")
            root.attributes("-topmost", True)
            root.withdraw()
            root.protocol("WM_DELETE_WINDOW", self._dismiss)
            root.bind("<Escape>", lambda _event: self._dismiss())
            self._build_widgets(tk, root)
            self._widgets["root"] = root
            self._ready.set()
            root.after(40, self._poll_commands)
            root.mainloop()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._widgets.clear()
            root = None
            gc.collect()

    def _build_widgets(self, tk: Any, root: Any) -> None:
        root.geometry("540x390")
        root.resizable(False, False)

        shell = tk.Frame(root, bg="#111827", padx=24, pady=20)
        shell.pack(fill="both", expand=True)
        header = tk.Frame(shell, bg="#111827")
        header.pack(fill="x")
        dot = tk.Label(header, text="●", fg="#22c55e", bg="#111827", font=("Segoe UI", 15))
        dot.pack(side="left", padx=(0, 10))
        title = tk.Label(
            header,
            text="voice2text",
            fg="#f9fafb",
            bg="#111827",
            font=("Segoe UI Semibold", 16),
            anchor="w",
        )
        title.pack(side="left", fill="x", expand=True)
        dismiss = tk.Button(
            header,
            text="Close",
            command=self._dismiss,
            fg="#d1d5db",
            bg="#111827",
            activebackground="#1f2937",
            activeforeground="#ffffff",
            borderwidth=0,
            font=("Segoe UI", 16),
            cursor="hand2",
        )
        dismiss.pack(side="right")

        message = tk.Label(
            shell,
            text="",
            fg="#93c5fd",
            bg="#111827",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            pady=8,
        )
        message.pack(fill="x")
        answer = tk.Text(
            shell,
            height=9,
            wrap="word",
            fg="#e5e7eb",
            bg="#1f2937",
            insertbackground="#ffffff",
            relief="flat",
            padx=14,
            pady=12,
            font=("Segoe UI", 11),
            state="disabled",
        )
        answer.pack(fill="both", expand=True, pady=(4, 10))
        citations = tk.Frame(shell, bg="#111827")
        citations.pack(fill="x")
        actions = tk.Frame(shell, bg="#111827")
        actions.pack(fill="x", pady=(12, 0))

        copy_button = _button(tk, actions, "Copy answer", self._copy_answer, primary=True)
        submit_button = _button(tk, actions, "Submit", self._submit_limit, primary=True)
        discard_button = _button(tk, actions, "Discard", self._discard_limit)

        self._widgets.update(
            {
                "dot": dot,
                "title": title,
                "message": message,
                "answer": answer,
                "citations": citations,
                "actions": actions,
                "copy": copy_button,
                "submit": submit_button,
                "discard": discard_button,
            }
        )

    def _poll_commands(self) -> None:
        root = self._widgets["root"]
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            if command.kind is OverlayCommandKind.SHUTDOWN:
                self._model.apply(command)
                root.destroy()
                return
            self._model.apply(command)
            if command.kind is OverlayCommandKind.SHOW_GLEAN_RECORDING:
                self._play_start_sound()
            self._render()
        root.after(40, self._poll_commands)

    def _render(self) -> None:
        state = self._model.state
        root = self._widgets["root"]
        if state.status is OverlayStatus.HIDDEN:
            root.withdraw()
            return

        colors = {
            OverlayStatus.LOCAL_RECORDING: "#22c55e",
            OverlayStatus.GLEAN_RECORDING: "#f97316",
            OverlayStatus.THINKING: "#60a5fa",
            OverlayStatus.ANSWER: "#a78bfa",
            OverlayStatus.ERROR: "#ef4444",
            OverlayStatus.LIMIT_CONFIRMATION: "#f59e0b",
        }
        self._widgets["dot"].configure(fg=colors[state.status])
        self._widgets["title"].configure(text=state.title)
        self._widgets["message"].configure(text=state.message)

        answer = self._widgets["answer"]
        answer.configure(state="normal")
        answer.delete("1.0", "end")
        answer.insert("1.0", state.answer)
        answer.configure(state="disabled")

        citations_frame = self._widgets["citations"]
        for child in citations_frame.winfo_children():
            child.destroy()
        if state.citations:
            import tkinter as tk

            tk.Label(
                citations_frame,
                text="Sources",
                fg="#9ca3af",
                bg="#111827",
                font=("Segoe UI Semibold", 9),
            ).pack(anchor="w")
            for citation in state.citations:
                tk.Button(
                    citations_frame,
                    text=citation.title,
                    command=lambda url=citation.url: webbrowser.open(url),
                    fg="#93c5fd",
                    bg="#111827",
                    activebackground="#111827",
                    activeforeground="#bfdbfe",
                    borderwidth=0,
                    anchor="w",
                    cursor="hand2",
                ).pack(fill="x", anchor="w")

        for name in ("copy", "submit", "discard"):
            self._widgets[name].pack_forget()
        if state.status is OverlayStatus.ANSWER and state.answer:
            self._widgets["copy"].pack(side="right")
        elif state.status is OverlayStatus.LIMIT_CONFIRMATION:
            self._widgets["discard"].pack(side="right", padx=(8, 0))
            self._widgets["submit"].pack(side="right")

        root.deiconify()
        root.lift()
        root.update_idletasks()
        x = max(20, root.winfo_screenwidth() - root.winfo_width() - 32)
        root.geometry(f"+{x}+32")

    def _copy_answer(self) -> None:
        answer = self._model.state.answer
        if not answer:
            return
        try:
            self._clipboard_factory().write_text(answer)
            self._widgets["message"].configure(text="Answer copied — paste only where intended")
        except Exception:
            self._widgets["message"].configure(text="Clipboard access was denied")

    def _dismiss(self) -> None:
        self._on_cancel()
        self._model.apply(OverlayCommand(OverlayCommandKind.HIDE))
        self._render()

    def _submit_limit(self) -> None:
        self._on_limit_decision(True)
        self._model.apply(OverlayCommand(OverlayCommandKind.SHOW_THINKING))
        self._render()

    def _discard_limit(self) -> None:
        self._on_limit_decision(False)
        self._model.apply(OverlayCommand(OverlayCommandKind.HIDE))
        self._render()


def _button(
    tk: Any,
    parent: Any,
    text: str,
    command: Callable[[], None],
    *,
    primary: bool = False,
) -> Any:
    background = "#2563eb" if primary else "#374151"
    active = "#1d4ed8" if primary else "#4b5563"
    return tk.Button(
        parent,
        text=text,
        command=command,
        fg="#ffffff",
        bg=background,
        activebackground=active,
        activeforeground="#ffffff",
        relief="flat",
        padx=14,
        pady=7,
        cursor="hand2",
        font=("Segoe UI Semibold", 9),
    )


def _play_default_start_sound() -> None:
    try:
        import winsound

        winsound.Beep(880, 140)
    except RuntimeError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Show the complete network-free Ask Glean overlay flow."""

    parser = argparse.ArgumentParser(description="Test the mock Ask Glean overlay")
    parser.add_argument("--answer-seconds", type=float, default=4.0)
    args = parser.parse_args(argv)
    if not 0.5 <= args.answer_seconds <= 30:
        parser.error("--answer-seconds must be between 0.5 and 30")

    with Overlay() as overlay:
        overlay.show_glean_recording()
        time.sleep(1.2)
        overlay.show_thinking()
        time.sleep(0.6)
        citations: tuple[Citation, ...] = ()
        for chunk in MockGleanClient(delay_seconds=0.25).stream_answer("manual mock test"):
            overlay.append_answer(chunk.text_delta)
            if chunk.done:
                citations = chunk.citations
        overlay.complete_answer(citations)
        time.sleep(args.answer_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
