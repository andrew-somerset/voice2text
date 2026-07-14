"""Entry point: wires hotkey, recorder, transcriber, paster, and overlay UI.

This is the only module allowed to import the sibling modules.

Threading / run-loop model:
the main thread hosts an ``NSApplication`` (accessory app — no Dock icon) whose
run loop both drives the on-screen overlay windows and delivers the Quartz Fn
event-tap callbacks. The tap callbacks only flip the recorder flag, snapshot
audio onto a queue, and marshal a UI update onto the main thread; a single
daemon worker thread drains the queue, transcribes, and pastes. No asyncio.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.metadata
import logging
import os
import queue
import sys
import threading
import time
from typing import IO

import numpy as np
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from PyObjCTools import AppHelper

from voice2text import config
from voice2text.corrections import CorrectionWatcher
from voice2text.hotkey import HotkeyListener
from voice2text.overlay import Overlay, ResultWindow
from voice2text.paster import Paster
from voice2text.recorder import Recorder
from voice2text.transcriber import Transcriber
from voice2text.vocabulary import Vocabulary

logger = logging.getLogger(__name__)


def _acquire_single_instance_lock() -> IO[str] | None:
    """Hold an exclusive lock so only one voice2text runs at a time.

    Two instances would each install an Fn tap and both paste on every
    key-release — duplicate text. Returns the open lock file (keep it alive for
    the process lifetime), or None if another instance already holds it.
    """
    lock_path = config.VOCAB_PATH.parent / "voice2text.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _app_version() -> str:
    """Installed package version, or a dev placeholder when running from source."""
    try:
        return importlib.metadata.version("voice2text")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0-dev"


def _print_banner(model_name: str) -> None:
    """Startup banner — the one place print() is allowed outside __main__ blocks."""
    print(
        f"""
voice2text {_app_version()} — local push-to-talk dictation for macOS
  model: {model_name} (whisper.cpp, fully on-device)

  Hold the Fn key to dictate, release to paste into the focused app.
  A listening pill appears at the bottom-center of your screen while you hold Fn.

  First-run checklist:
    * The app that launched this script (Terminal, iTerm, ...) needs
      Input Monitoring, Accessibility, and Microphone permissions in
      System Settings -> Privacy & Security.
    * Set System Settings -> Keyboard -> "Press \U0001f310 key to" -> "Do Nothing",
      or macOS's built-in dictation will fight this app.
    * If a paste ever silently fails, Secure Keyboard Entry is likely on
      (e.g. Terminal menu -> Secure Keyboard Entry); turn it off. The transcript
      is still put on your clipboard and shown in a window so you never lose it.

  Ctrl+C to quit.
"""
    )


def _should_show_window(mode: str, pasted: bool) -> bool:
    """Whether to surface the copy-the-text window for this outcome."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return not pasted  # "on-failure"


def _worker_loop(
    work_queue: queue.Queue[tuple[float, np.ndarray] | None],
    transcriber: Transcriber,
    paster: Paster,
    overlay: Overlay,
    result_window: ResultWindow,
    window_mode: str,
    vocabulary: Vocabulary,
    watcher: CorrectionWatcher,
) -> None:
    """Drain queued utterances: transcribe, paste, update UI, log latency.

    Runs on a daemon thread. A ``None`` item is the shutdown sentinel. Every
    iteration is exception-guarded so the worker never dies mid-session. All
    UI touches are marshaled to the main thread via ``AppHelper.callAfter``.
    """
    while True:
        item = work_queue.get()
        if item is None:
            break
        t_keyup, audio = item
        try:
            # Learn from any edit the user made to the *previous* paste before
            # starting this one (best-effort; no-op where AX can't read).
            watcher.check_for_correction()

            audio_seconds = len(audio) / config.SAMPLE_RATE
            t_start = time.perf_counter()
            text = transcriber.transcribe(audio, initial_prompt=vocabulary.initial_prompt())
            text = vocabulary.apply_substitutions(text)
            t_transcribed = time.perf_counter()

            AppHelper.callAfter(overlay.hide)

            if not text:
                logger.info(
                    "Discarded utterance (%.2fs of audio): too short or no speech",
                    audio_seconds,
                )
                continue

            outcome = paster.paste(text)
            t_pasted = time.perf_counter()

            if outcome.pasted:
                # Let the paste land, then snapshot the field so a later edit
                # can be detected as a correction.
                time.sleep(0.15)
                watcher.note_paste(text)

            if _should_show_window(window_mode, outcome.pasted):
                reason = outcome.reason if not outcome.pasted else ""
                AppHelper.callAfter(result_window.show, text, reason)

            logger.info(
                "%s %d chars | audio %.2fs | transcribe %.0fms | paste %.0fms | "
                "key-up -> paste %.0fms (budget 700ms)",
                "Pasted" if outcome.pasted else "Copied (paste blocked):",
                len(text),
                audio_seconds,
                (t_transcribed - t_start) * 1000,
                (t_pasted - t_transcribed) * 1000,
                (t_pasted - t_keyup) * 1000,
            )
        except Exception:
            logger.exception("Worker failed while handling an utterance; continuing")


def main() -> None:
    """Wire all components together and run the NSApplication loop on the main thread."""
    parser = argparse.ArgumentParser(
        prog="voice2text",
        description="Local push-to-talk dictation: hold Fn, speak, release, paste.",
    )
    parser.add_argument("--verbose", action="store_true", help="enable DEBUG logging")
    parser.add_argument(
        "--model",
        default=config.MODEL_NAME,
        help=f"whisper model name (default: {config.MODEL_NAME})",
    )
    parser.add_argument(
        "--show-result-window",
        choices=config.RESULT_WINDOW_MODES,
        default=config.RESULT_WINDOW_MODE,
        help=("when to show the copy-the-text window: on-failure (default), always, or never"),
    )
    parser.add_argument(
        "--keep-fillers",
        action="store_true",
        help='keep spoken filler words ("um", "uh", ...) instead of stripping them',
    )
    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="disable learning custom vocabulary from your in-place corrections",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Refuse to start a second instance — two would both paste every utterance.
    lock = _acquire_single_instance_lock()
    if lock is None:
        print(
            "voice2text is already running (another instance holds the lock).\n"
            "Quit that one first (Ctrl+C in its terminal), or run:  pkill -f voice2text",
            file=sys.stderr,
        )
        sys.exit(1)

    _print_banner(args.model)

    # Accessory app: shows floating panels but no Dock icon or menu bar.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    t_load = time.perf_counter()
    transcriber = Transcriber(model_name=args.model, remove_fillers_enabled=not args.keep_fillers)
    logger.info(
        "Model '%s' loaded and warmed up in %.0fms",
        args.model,
        (time.perf_counter() - t_load) * 1000,
    )

    recorder = Recorder()
    recorder.open()  # permanent stream; triggers the mic permission prompt on first run
    paster = Paster()
    overlay = Overlay(level_provider=recorder.level)  # live waveform reacts to mic
    result_window = ResultWindow()

    vocabulary = Vocabulary()
    learn_enabled = config.LEARN_CORRECTIONS and not args.no_learn
    watcher = CorrectionWatcher(on_learn=vocabulary.learn, enabled=learn_enabled)
    if learn_enabled and not watcher.available:
        logger.info("Correction learning: Accessibility unavailable; substitutions still apply")
    elif learn_enabled:
        logger.info("Correction learning on (edit a pasted word to teach it)")
    terms = vocabulary.terms()
    if terms:
        logger.info("Loaded %d custom vocabulary term(s)", len(terms))

    work_queue: queue.Queue[tuple[float, np.ndarray] | None] = queue.Queue()
    worker = threading.Thread(
        target=_worker_loop,
        args=(
            work_queue,
            transcriber,
            paster,
            overlay,
            result_window,
            args.show_result_window,
            vocabulary,
            watcher,
        ),
        name="voice2text-worker",
        daemon=True,
    )
    worker.start()

    # Tap-callback path: both handlers must be near-instant. Recorder start/stop
    # and take() are lock-guarded and sub-millisecond; UI work is deferred to the
    # main loop via callAfter so the callback itself never blocks (a slow tap
    # callback gets the event tap disabled by macOS).
    def on_press() -> None:
        recorder.start()
        AppHelper.callAfter(overlay.show_listening)

    def on_release() -> None:
        recorder.stop()
        # Snapshot the audio here, not in the worker: the next Fn press clears
        # the recorder buffer, which must never destroy a queued utterance.
        work_queue.put((time.perf_counter(), recorder.take()))
        AppHelper.callAfter(overlay.show_transcribing)

    listener = HotkeyListener(on_press, on_release)
    try:
        listener.install()  # adds the tap to this (main) thread's run loop
    except PermissionError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(
            "Grant permissions to the app that launched this script (Terminal, iTerm, ...):\n"
            "  System Settings -> Privacy & Security -> Input Monitoring\n"
            "  System Settings -> Privacy & Security -> Accessibility\n"
            "then quit and relaunch that app and re-run voice2text. macOS never\n"
            "re-prompts after a denial — the toggles must be flipped manually.",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info("Ready — hold Fn to dictate")
    try:
        # Drives the main run loop AND delivers the event-tap callbacks. The
        # installInterrupt handler turns Ctrl+C into a clean loop stop.
        AppHelper.runEventLoop(installInterrupt=True)
    except KeyboardInterrupt:
        pass

    logger.info("Shutting down")
    work_queue.put(None)  # shutdown sentinel for the worker
    listener.stop()
    worker.join(timeout=2.0)
    recorder.close()
    logger.info("Goodbye")


if __name__ == "__main__":
    main()
