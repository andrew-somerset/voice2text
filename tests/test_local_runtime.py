from __future__ import annotations

import numpy as np

from voice2text.gesture import GestureEvent, GestureEventKind
from voice2text.local_runtime import (
    DictationJob,
    DictationResult,
    DictationResultKind,
    FocusTargetCache,
    LocalDictationController,
    LocalTranscriptionWorker,
)
from voice2text.paster import FocusTarget, PasteMethod, PasteOutcome

TARGET = FocusTarget(foreground_window=1234, focused_control=1235)
OTHER_TARGET = FocusTarget(foreground_window=2234, focused_control=2235)


class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.level = 0.58
        self.starts = 0
        self.cancels = 0
        self.last_audio: np.ndarray | None = None

    def start(self) -> None:
        self.starts += 1
        self.is_recording = True

    def stop(self) -> np.ndarray:
        self.is_recording = False
        self.last_audio = np.ones(8_000, dtype=np.float32)
        return self.last_audio

    def cancel(self) -> None:
        self.cancels += 1
        self.is_recording = False


class FakePill:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def show_ready(self, trigger_name: str) -> None:
        self.calls.append(("ready", trigger_name))

    def show_local(self, trigger_name: str) -> None:
        self.calls.append(("local", trigger_name))

    def show_transcribing(self) -> None:
        self.calls.append(("transcribing", None))

    def show_pasted(self) -> None:
        self.calls.append(("pasted", None))

    def show_no_speech(self) -> None:
        self.calls.append(("no-speech", None))

    def show_error(self, message: str) -> None:
        self.calls.append(("error", message))

    def show_paste_blocked(self, text: str, reason: str) -> None:
        self.calls.append(("paste-blocked", (text, reason)))

    def set_level(self, level: float) -> None:
        self.calls.append(("level", level))

    def hide(self) -> None:
        self.calls.append(("hide", None))


class FakeWorker:
    def __init__(self) -> None:
        self.jobs: list[DictationJob] = []
        self.results: list[DictationResult] = []

    def submit(self, job: DictationJob) -> None:
        self.jobs.append(job)

    def get_result_nowait(self) -> DictationResult | None:
        return self.results.pop(0) if self.results else None


class FakeTranscriber:
    def __init__(self, text: str = "locally transcribed text") -> None:
        self.text = text
        self.audio: np.ndarray | None = None

    def transcribe(self, audio: np.ndarray) -> str:
        self.audio = audio
        return self.text


class FakePaster:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        outcome: PasteOutcome | None = None,
    ) -> None:
        self.failure = failure
        self.outcome = outcome or PasteOutcome(True, PasteMethod.SEND_INPUT)
        self.calls: list[tuple[str, FocusTarget | None]] = []

    def paste(self, text: str, *, target: FocusTarget | None = None) -> PasteOutcome:
        self.calls.append((text, target))
        if self.failure is not None:
            raise self.failure
        return self.outcome


def event(kind: GestureEventKind, timestamp_ns: int = 1_000_000_000) -> GestureEvent:
    return GestureEvent(kind=kind, timestamp_ns=timestamp_ns)


def test_focus_cache_freezes_last_idle_target_when_alt_enters_menu_mode() -> None:
    cache = FocusTargetCache()
    cache.observe(TARGET)

    assert cache.target_for_press(None) == TARGET


def test_focus_cache_uses_new_current_target_when_available() -> None:
    cache = FocusTargetCache()
    cache.observe(TARGET)

    assert cache.target_for_press(OTHER_TARGET) == OTHER_TARGET
    assert cache.target_for_press(None) == OTHER_TARGET


def test_controller_captures_audio_for_original_target_and_shows_transcribing() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    worker = FakeWorker()
    controller = LocalDictationController(
        trigger_name="Right Alt",
        recorder=recorder,
        pill=pill,
        worker=worker,  # type: ignore[arg-type]
    )

    controller.remember_target(TARGET)
    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.refresh(1_010_000_000)
    controller.handle((event(GestureEventKind.LOCAL_STOP),))

    assert recorder.starts == 1
    assert ("local", "Right Alt") in pill.calls
    assert ("level", 0.58) in pill.calls
    assert pill.calls[-1] == ("transcribing", None)
    assert len(worker.jobs) == 1
    assert worker.jobs[0].target == TARGET
    assert worker.jobs[0].audio is recorder.last_audio


def test_controller_refuses_paste_job_without_original_window_and_zeros_audio() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    worker = FakeWorker()
    controller = LocalDictationController(
        trigger_name="Right Alt",
        recorder=recorder,
        pill=pill,
        worker=worker,  # type: ignore[arg-type]
    )

    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.handle((event(GestureEventKind.LOCAL_STOP),))

    assert worker.jobs == []
    assert recorder.last_audio is not None
    assert np.count_nonzero(recorder.last_audio) == 0
    assert pill.calls[-1][0] == "error"


def test_controller_surfaces_content_free_worker_results() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    worker = FakeWorker()
    controller = LocalDictationController(
        trigger_name="Right Alt",
        recorder=recorder,
        pill=pill,
        worker=worker,  # type: ignore[arg-type]
    )

    worker.results.extend(
        [
            DictationResult(DictationResultKind.PASTED),
            DictationResult(DictationResultKind.NO_SPEECH),
            DictationResult(
                DictationResultKind.PASTE_BLOCKED,
                text="local fallback text",
                reason="Windows blocked focused text insertion",
            ),
            DictationResult(DictationResultKind.ERROR),
        ]
    )
    controller.refresh(1_000_000_000)

    assert ("pasted", None) in pill.calls
    assert ("no-speech", None) in pill.calls
    assert (
        "paste-blocked",
        ("local fallback text", "Windows blocked focused text insertion"),
    ) in pill.calls
    assert pill.calls[-1][0] == "error"
    assert "Transcription" in str(pill.calls[-1][1])


def test_worker_transcribes_pastes_to_target_and_zeros_audio() -> None:
    transcriber = FakeTranscriber()
    paster = FakePaster()
    worker = LocalTranscriptionWorker(transcriber=transcriber, paster=paster)
    audio = np.ones(8_000, dtype=np.float32)

    worker.start()
    worker.submit(DictationJob(audio=audio, target=TARGET))
    result = worker.wait_for_result()
    worker.stop()

    assert result.kind is DictationResultKind.PASTED
    assert paster.calls == [("locally transcribed text", TARGET)]
    assert np.count_nonzero(audio) == 0


def test_worker_does_not_paste_empty_transcript_and_zeros_audio() -> None:
    transcriber = FakeTranscriber("")
    paster = FakePaster()
    worker = LocalTranscriptionWorker(transcriber=transcriber, paster=paster)
    audio = np.ones(4_000, dtype=np.float32)

    worker.start()
    worker.submit(DictationJob(audio=audio, target=TARGET))
    result = worker.wait_for_result()
    worker.stop()

    assert result.kind is DictationResultKind.NO_SPEECH
    assert paster.calls == []
    assert np.count_nonzero(audio) == 0


def test_worker_sanitizes_paste_failure_and_zeros_audio() -> None:
    private_marker = "private dictated text"
    transcriber = FakeTranscriber(private_marker)
    paster = FakePaster(failure=RuntimeError(private_marker))
    worker = LocalTranscriptionWorker(transcriber=transcriber, paster=paster)
    audio = np.ones(8_000, dtype=np.float32)

    worker.start()
    worker.submit(DictationJob(audio=audio, target=TARGET))
    result = worker.wait_for_result()
    worker.stop()

    assert result.kind is DictationResultKind.ERROR
    assert np.count_nonzero(audio) == 0


def test_worker_returns_in_memory_fallback_when_automatic_paste_is_blocked() -> None:
    private_marker = "dictated fallback text"
    transcriber = FakeTranscriber(private_marker)
    paster = FakePaster(
        outcome=PasteOutcome(False, reason="Windows blocked focused text insertion")
    )
    worker = LocalTranscriptionWorker(transcriber=transcriber, paster=paster)
    audio = np.ones(8_000, dtype=np.float32)

    worker.start()
    worker.submit(DictationJob(audio=audio, target=TARGET))
    result = worker.wait_for_result()
    worker.stop()

    assert result.kind is DictationResultKind.PASTE_BLOCKED
    assert result.text == private_marker
    assert result.reason == "Windows blocked focused text insertion"
    assert private_marker not in repr(result)
    assert np.count_nonzero(audio) == 0
