from __future__ import annotations

import io
import os
import signal
import tempfile

from pathlib import Path
from threading import Event, Thread, get_ident

import pytest

from type4me_linux.capture import RawCaptureSession
from type4me_linux.config import CaptureConfig
from type4me_linux.events import RecognitionTranscript
from type4me_linux.inject import InjectionResult
from type4me_linux.providers import RecognitionResult
from type4me_linux.session import RecognitionSession


def _transcript(
    text: str,
    *,
    partial: str = "",
    final: bool = False,
    backend: str = "sensevoice-vad",
) -> RecognitionTranscript:
    return RecognitionTranscript(
        confirmed_segments=(text,) if text else (),
        partial_text=partial,
        authoritative_text=text,
        is_final=final,
        backend=backend,
    )


class _Capture:
    def __init__(self, wav_path: Path, chunks: tuple[bytes, ...] = (b"pcm",)) -> None:
        self.wav_path = wav_path
        self.chunks = chunks
        self.started = 0
        self.stopped = 0
        self.cancelled = 0
        self.released = 0

    def start(self) -> _Capture:
        self.started += 1
        return self

    def iter_chunks(self):  # type: ignore[no-untyped-def]
        yield from self.chunks

    def stop(self) -> Path:
        self.stopped += 1
        return self.wav_path

    def cancel(self) -> None:
        self.cancelled += 1

    def release_wav(self) -> None:
        self.released += 1
        self.wav_path.unlink(missing_ok=True)


class _Streamer:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.accepted = 0
        self.flushed = 0

    def accept_chunk(self, pcm16le: bytes) -> tuple[RecognitionTranscript, ...]:
        self.accepted += 1
        if self.fail is not None:
            raise self.fail
        if self.accepted == 1:
            return (_transcript("", partial="正在识别"),)
        return ()

    def flush(self) -> RecognitionTranscript:
        self.flushed += 1
        return _transcript("SenseVoice 原文", final=True)


class _Calibrator:
    def __init__(self, result: str | Exception) -> None:
        self.result = result
        self.calls: list[Path] = []

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        self.calls.append(wav_path)
        if isinstance(self.result, Exception):
            raise self.result
        return RecognitionResult(self.result, "qwen3-sherpa")


class _SignalFileIO(io.FileIO):
    def __init__(self, fd: int, read_started: Event) -> None:
        super().__init__(fd, mode="rb", closefd=True)
        self._read_started = read_started

    def readinto(self, buffer):  # type: ignore[no-untyped-def]
        self._read_started.set()
        return super().readinto(buffer)


class _SignalPipeProcess:
    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self.read_started = Event()
        self.stdout = io.BufferedReader(_SignalFileIO(read_fd, self.read_started))
        self.stderr = io.BytesIO()
        self._write_fd: int | None = write_fd
        self.terminated = 0
        self.killed = 0
        self.wait_calls: list[float | None] = []
        self.reaped = False

    def terminate(self) -> None:
        self.terminated += 1
        self.close_writer()

    def kill(self) -> None:
        self.killed += 1
        self.close_writer()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.reaped = True
        return 0

    def close_writer(self) -> None:
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None


def _raw_capture(tmp_path: Path, process: _SignalPipeProcess) -> RawCaptureSession:
    def create_temp(**kwargs: object):  # type: ignore[no-untyped-def]
        return tempfile.NamedTemporaryFile(dir=tmp_path, **kwargs)

    return RawCaptureSession(
        CaptureConfig(),
        popen_factory=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
        temp_factory=create_temp,
        shutdown_timeout=0.25,
    )


def _capture(tmp_path: Path) -> _Capture:
    wav_path = tmp_path / "owned.wav"
    wav_path.write_bytes(b"RIFF")
    return _Capture(wav_path)


def test_streaming_session_orders_partial_final_processing_and_injection(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)
    streamer = _Streamer()
    calibrator = _Calibrator("Qwen 终稿")
    processed: list[str] = []
    injected: list[str] = []
    history: list[tuple[RecognitionTranscript, RecognitionTranscript]] = []

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=streamer,
        calibrator=calibrator,  # type: ignore[arg-type]
        processor=lambda text: processed.append(text) or f"处理：{text}",
        injector=lambda text: injected.append(text) or InjectionResult("wtype", True),
        history_writer=lambda raw, final, injection: history.append((raw, final)),
    )

    session.run()

    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "transcript",
        "finalized",
        "completed",
    ]
    final = session.events[2].transcript
    assert final is not None
    assert final.authoritative_text == "处理：Qwen 终稿"
    assert final.backend == "hybrid"
    assert final.is_final is True
    assert processed == ["Qwen 终稿"]
    assert injected == ["处理：Qwen 终稿"]
    assert history[0][0].authoritative_text == "SenseVoice 原文"
    assert history[0][1] is final
    assert calibrator.calls == [capture.wav_path]
    assert (capture.started, capture.stopped, capture.cancelled, capture.released) == (1, 1, 0, 1)
    assert not capture.wav_path.exists()
    assert session.state_history == (
        "idle",
        "starting",
        "recording",
        "finishing",
        "processing",
        "injecting",
        "idle",
    )


def test_qwen_failure_warns_then_finalizes_hybrid_fallback(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    injected: list[str] = []
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        calibrator=_Calibrator(RuntimeError("模型不可用")),  # type: ignore[arg-type]
        injector=lambda text: injected.append(text) or InjectionResult("wtype", True),
    )

    session.run()

    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "warning",
        "transcript",
        "finalized",
        "completed",
    ]
    warning = session.events[2]
    final = session.events[3].transcript
    assert warning.message is not None and "Qwen3-ASR 校准失败" in warning.message
    assert final is not None
    assert final.authoritative_text == "SenseVoice 原文"
    assert final.backend == "hybrid-fallback"
    assert injected == ["SenseVoice 原文"]


def test_streaming_failure_emits_one_error_then_completed_without_injection(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)
    injected: list[str] = []
    successful_history: list[str] = []
    failed_history: list[str] = []
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(fail=RuntimeError("VAD 崩溃")),
        injector=lambda text: injected.append(text) or InjectionResult("wtype", True),
        history_writer=lambda raw, final, injection: successful_history.append(
            final.authoritative_text
        ),
        failed_history_writer=failed_history.append,
    )

    session.run()
    session.stop()
    session.cancel()

    assert [event.type for event in session.events] == ["ready", "error", "completed"]
    assert session.events[1].message is not None and "VAD 崩溃" in session.events[1].message
    assert injected == []
    assert successful_history == []
    assert len(failed_history) == 1
    assert capture.stopped == 0
    assert capture.cancelled == 1
    assert capture.released == 1
    assert not capture.wav_path.exists()


def test_cancel_emits_only_cancel_terminal_and_skips_final_side_effects(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    streamer = _Streamer()
    calibrator = _Calibrator("不应调用")
    injected: list[str] = []
    history: list[str] = []
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=streamer,
        calibrator=calibrator,  # type: ignore[arg-type]
        injector=lambda text: injected.append(text) or InjectionResult("wtype", True),
        history_writer=lambda raw, final, injection: history.append(final.authoritative_text),
    )

    session.start()
    session.cancel()
    session.cancel()
    session.stop()

    assert [event.type for event in session.events] == ["ready", "cancelled"]
    assert capture.cancelled == 1
    assert capture.stopped == 0
    assert capture.released == 1
    assert streamer.flushed == 0
    assert calibrator.calls == []
    assert injected == []
    assert history == []
    assert session.state_history == ("idle", "starting", "recording", "cancelled", "idle")


def test_normal_stop_is_idempotent_and_each_side_effect_runs_once(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    streamer = _Streamer()
    calls: list[str] = []
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=streamer,
        processor=lambda text: calls.append("process") or text,
        injector=lambda text: calls.append("inject") or InjectionResult("test", True),
        history_writer=lambda raw, final, injection: calls.append("history"),
    )

    session.start()
    session.stop()
    session.stop()
    session.cancel()

    assert streamer.flushed == 1
    assert capture.stopped == 1
    assert capture.released == 1
    assert calls == ["process", "inject", "history"]
    assert [event.type for event in session.events].count("completed") == 1


def test_startup_warnings_follow_ready_and_do_not_skip_processing(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    processed: list[str] = []
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        startup_warnings=("未读取到主选区", "剪贴板不可用"),
        processor=lambda text: processed.append(text) or text,
    )

    session.run()

    assert [event.type for event in session.events] == [
        "ready",
        "warning",
        "warning",
        "transcript",
        "transcript",
        "finalized",
        "completed",
    ]
    assert [event.message for event in session.events[1:3]] == [
        "未读取到主选区",
        "剪贴板不可用",
    ]
    assert processed == ["SenseVoice 原文"]


def test_processing_can_publish_sequenced_warning_and_keep_fallback_text(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)
    holder: dict[str, RecognitionSession] = {}

    def process(text: str) -> str:
        event = holder["session"].publish_warning("处理服务超时，已保留原文")
        assert event is not None
        return text

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        processor=process,
    )
    holder["session"] = session

    session.run()
    event_count = len(session.events)
    assert session.publish_warning("完成后不应发布") is None

    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "warning",
        "transcript",
        "finalized",
        "completed",
    ]
    assert session.events[2].message == "处理服务超时，已保留原文"
    assert session.events[3].transcript is not None
    assert session.events[3].transcript.authoritative_text == "SenseVoice 原文"
    assert len(session.events) == event_count


def test_session_requires_a_finalization_strategy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="批量终结器或流式识别器"):
        RecognitionSession(capture_factory=lambda: _capture(tmp_path))


def test_batch_finalizer_receives_artifact_and_non_wav_calibration_falls_back() -> None:
    artifacts: list[object] = []

    class BatchCapture:
        def stop(self) -> object:
            return object()

        def cancel(self) -> None:
            raise AssertionError("正常完成不应取消采集")

    def finalize(artifact: object) -> RecognitionTranscript:
        artifacts.append(artifact)
        return _transcript("批量原文", partial="过期局部")

    calibrator = _Calibrator("不应调用")
    session = RecognitionSession(
        capture_factory=BatchCapture,
        finalizer=finalize,
        calibrator=calibrator,  # type: ignore[arg-type]
    )

    session.start()
    session.stop()

    assert len(artifacts) == 1
    assert calibrator.calls == []
    assert [event.type for event in session.events] == [
        "ready",
        "warning",
        "transcript",
        "finalized",
        "completed",
    ]
    assert session.events[1].message is not None
    assert "缺少 WAV 文件" in session.events[1].message
    final = session.events[2].transcript
    assert final is not None
    assert final.partial_text == ""
    assert final.authoritative_text == "批量原文"
    assert final.backend == "hybrid-fallback"


def test_start_failure_records_one_failure_even_when_failure_writer_raises() -> None:
    failure_calls: list[str] = []

    def create_capture() -> _Capture:
        raise OSError("无法启动 pw-record")

    def write_failure(message: str) -> None:
        failure_calls.append(message)
        raise RuntimeError("历史库只读")

    session = RecognitionSession(
        capture_factory=create_capture,
        streamer=_Streamer(),
        failed_history_writer=write_failure,
    )

    session.start()
    session.start()
    session.stop()

    assert [event.type for event in session.events] == ["error", "completed"]
    assert "无法启动 pw-record" in (session.events[0].message or "")
    assert len(failure_calls) == 1
    assert session.state == "idle"


@pytest.mark.parametrize("missing", ["streamer", "iterator"])
def test_consume_rejects_incomplete_streaming_capture(
    tmp_path: Path,
    missing: str,
) -> None:
    capture = _capture(tmp_path)
    if missing == "iterator":
        capture.iter_chunks = None  # type: ignore[method-assign,assignment]
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=None if missing == "streamer" else _Streamer(),
        finalizer=(lambda _artifact: _transcript("批量")) if missing == "streamer" else None,
    )

    session.start()
    session.consume()

    assert [event.type for event in session.events] == ["ready", "error", "completed"]
    assert capture.cancelled == 1
    assert capture.released == 1


def test_recording_rejects_final_transcript_and_post_stop_publication(tmp_path: Path) -> None:
    session = RecognitionSession(
        capture_factory=lambda: _capture(tmp_path),
        streamer=_Streamer(),
    )
    session.start()

    with pytest.raises(ValueError, match="不能发布最终"):
        session.publish_transcript(_transcript("过早终稿", final=True))

    session.stop()
    with pytest.raises(RuntimeError, match="只有录音中的"):
        session.publish_transcript(_transcript("迟到局部"))


def test_empty_calibration_and_processing_failure_both_preserve_sensevoice(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)

    def fail_processing(_text: str) -> str:
        raise TimeoutError("处理超时")

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        calibrator=_Calibrator("   "),  # type: ignore[arg-type]
        processor=fail_processing,
    )

    session.run()

    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "warning",
        "warning",
        "transcript",
        "finalized",
        "completed",
    ]
    assert "返回了空文本" in (session.events[2].message or "")
    assert "文本处理失败" in (session.events[3].message or "")
    assert session.events[4].transcript is not None
    assert session.events[4].transcript.authoritative_text == "SenseVoice 原文"


@pytest.mark.parametrize("side_effect", ["injector", "history"])
def test_final_side_effect_failure_becomes_terminal_error_and_releases_audio(
    tmp_path: Path,
    side_effect: str,
) -> None:
    capture = _capture(tmp_path)

    def inject(_text: str) -> InjectionResult:
        if side_effect == "injector":
            raise RuntimeError("输入失败")
        return InjectionResult("test", True)

    def write_history(
        _raw: RecognitionTranscript,
        _final: RecognitionTranscript,
        _injection: InjectionResult | None,
    ) -> None:
        if side_effect == "history":
            raise RuntimeError("历史写入失败")

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        injector=inject,
        history_writer=write_history,
    )

    session.run()

    assert [event.type for event in session.events][-2:] == ["error", "completed"]
    assert all(event.type != "finalized" for event in session.events)
    assert capture.released == 1
    assert not capture.wav_path.exists()


def test_cancel_swallows_capture_cleanup_failures_and_remains_idempotent() -> None:
    class BrokenCapture:
        def start(self) -> None:
            return None

        def cancel(self) -> None:
            raise OSError("终止失败")

        def release_wav(self) -> None:
            raise OSError("清理失败")

    session = RecognitionSession(
        capture_factory=BrokenCapture,
        streamer=_Streamer(),
    )

    session.start()
    session.cancel()
    session.cancel()

    assert [event.type for event in session.events] == ["ready", "cancelled"]
    assert session.state == "idle"


def test_stop_from_another_thread_only_requests_consumer_shutdown(
    tmp_path: Path,
) -> None:
    process = _SignalPipeProcess()
    capture = _raw_capture(tmp_path, process)
    flush_threads: list[int] = []

    class OwnerTrackingStreamer(_Streamer):
        def flush(self) -> RecognitionTranscript:
            flush_threads.append(get_ident())
            return super().flush()

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=OwnerTrackingStreamer(),
    )
    consumer = Thread(target=session.run)
    consumer.start()
    assert process.read_started.wait(1)

    session.stop()
    consumer.join(1)

    assert not consumer.is_alive()
    assert flush_threads == [consumer.ident]
    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "finalized",
        "completed",
    ]
    assert process.terminated == 1
    assert process.reaped
    assert not capture.wav_path.exists()


def test_first_sigint_during_real_buffered_read_defers_finalization_to_eof(
    tmp_path: Path,
) -> None:
    process = _SignalPipeProcess()
    capture = _raw_capture(tmp_path, process)
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
    )
    sender_failures: list[str] = []
    previous = signal.getsignal(signal.SIGINT)

    def handle_sigint(_signum: int, _frame: object) -> None:
        session.stop()

    def send_sigint() -> None:
        if not process.read_started.wait(1):
            sender_failures.append("录音读取未开始")
            process.close_writer()
            return
        os.kill(os.getpid(), signal.SIGINT)

    signal.signal(signal.SIGINT, handle_sigint)
    sender = Thread(target=send_sigint)
    sender.start()
    try:
        session.run()
    finally:
        process.close_writer()
        sender.join(1)
        signal.signal(signal.SIGINT, previous)

    assert sender_failures == []
    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "finalized",
        "completed",
    ]
    assert all(event.type != "error" for event in session.events)
    assert process.terminated == 1
    assert process.reaped
    assert process.stdout.closed
    assert not capture.wav_path.exists()


def test_second_sigint_during_calibration_cancels_without_terminal_success(
    tmp_path: Path,
) -> None:
    process = _SignalPipeProcess()
    capture = _raw_capture(tmp_path, process)
    calibration_started = Event()
    release_calibration = Event()
    cancelled = Event()

    class BlockingCalibrator:
        def transcribe(self, _wav_path: Path) -> RecognitionResult:
            calibration_started.set()
            assert release_calibration.wait(2)
            return RecognitionResult("不应发布", "qwen3-sherpa")

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        calibrator=BlockingCalibrator(),  # type: ignore[arg-type]
        event_sink=lambda event: cancelled.set() if event.type == "cancelled" else None,
    )
    interrupt_count = 0
    sender_failures: list[str] = []
    previous = signal.getsignal(signal.SIGINT)

    def handle_sigint(_signum: int, _frame: object) -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            session.stop()
        else:
            session.cancel()

    def send_sigints() -> None:
        if not process.read_started.wait(1):
            sender_failures.append("录音读取未开始")
            process.close_writer()
            release_calibration.set()
            return
        os.kill(os.getpid(), signal.SIGINT)
        if not calibration_started.wait(2):
            sender_failures.append("校准未开始")
            release_calibration.set()
            return
        os.kill(os.getpid(), signal.SIGINT)
        if not cancelled.wait(2):
            sender_failures.append("取消事件未发布")
        release_calibration.set()

    signal.signal(signal.SIGINT, handle_sigint)
    sender = Thread(target=send_sigints)
    sender.start()
    try:
        session.run()
    finally:
        process.close_writer()
        release_calibration.set()
        sender.join(2)
        signal.signal(signal.SIGINT, previous)

    assert sender_failures == []
    assert interrupt_count == 2
    assert [event.type for event in session.events] == ["ready", "cancelled"]
    assert process.terminated == 1
    assert process.reaped
    assert process.stdout.closed
    assert not capture.wav_path.exists()


def test_cancel_from_another_thread_interrupts_calibration(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    calibration_started = Event()
    cancel_returned = Event()
    holder: dict[str, RecognitionSession] = {}

    class BlockingCalibrator:
        def transcribe(self, _wav_path: Path) -> RecognitionResult:
            calibration_started.set()
            assert cancel_returned.wait(1)
            return RecognitionResult("不应发布", "qwen3-sherpa")

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        calibrator=BlockingCalibrator(),  # type: ignore[arg-type]
    )
    holder["session"] = session

    def cancel_from_worker() -> None:
        assert calibration_started.wait(1)
        holder["session"].cancel()
        cancel_returned.set()

    session.start()
    worker = Thread(target=cancel_from_worker)
    worker.start()
    session.stop()
    worker.join(1)

    assert not worker.is_alive()
    assert [event.type for event in session.events] == ["ready", "cancelled"]
    assert capture.cancelled == 1
    assert capture.released == 1
    assert not capture.wav_path.exists()


def test_capture_stop_failure_cancels_and_records_failure(tmp_path: Path) -> None:
    capture = _capture(tmp_path)

    def fail_stop() -> Path:
        capture.stopped += 1
        raise OSError("录音收尾失败")

    capture.stop = fail_stop  # type: ignore[method-assign]
    failures: list[str] = []
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
        failed_history_writer=failures.append,
    )

    session.run()

    assert [event.type for event in session.events] == [
        "ready",
        "transcript",
        "error",
        "completed",
    ]
    assert failures and "录音收尾失败" in failures[0]
    assert capture.cancelled == 1
    assert capture.released == 1


@pytest.mark.parametrize(
    "stage",
    ["capture-stop", "streamer-flush", "processor", "final-event", "injector", "history"],
)
def test_forced_cancellation_at_each_finalization_stage_skips_later_effects(
    tmp_path: Path,
    stage: str,
) -> None:
    capture = _capture(tmp_path)
    holder: dict[str, RecognitionSession] = {}
    effects: list[str] = []

    class CancellingStreamer(_Streamer):
        def flush(self) -> RecognitionTranscript:
            if stage == "streamer-flush":
                holder["session"].cancel()
            return super().flush()

    original_stop = capture.stop

    def stop_capture() -> Path:
        artifact = original_stop()
        if stage == "capture-stop":
            holder["session"].cancel()
        return artifact

    capture.stop = stop_capture  # type: ignore[method-assign]

    def process(text: str) -> str:
        effects.append("process")
        if stage == "processor":
            holder["session"].cancel()
        return text

    def inject(_text: str) -> InjectionResult:
        effects.append("inject")
        if stage == "injector":
            holder["session"].cancel()
        return InjectionResult("test", True)

    def history(
        _raw: RecognitionTranscript,
        _final: RecognitionTranscript,
        _injection: InjectionResult | None,
    ) -> None:
        effects.append("history")
        if stage == "history":
            holder["session"].cancel()

    def sink(event) -> None:  # type: ignore[no-untyped-def]
        if (
            stage == "final-event"
            and event.type == "transcript"
            and event.transcript is not None
            and event.transcript.is_final
        ):
            holder["session"].cancel()

    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=CancellingStreamer(),
        processor=process,
        injector=inject,
        history_writer=history,
        event_sink=sink,
    )
    holder["session"] = session

    session.start()
    session.stop()

    assert session.events[-1].type == "cancelled"
    assert all(event.type not in {"finalized", "completed"} for event in session.events)
    assert capture.cancelled == 1
    assert capture.released == 1
    assert not capture.wav_path.exists()
    if stage in {"capture-stop", "streamer-flush"}:
        assert effects == []
    elif stage in {"processor", "final-event"}:
        assert effects == ["process"]
    elif stage == "injector":
        assert effects == ["process", "inject"]
    else:
        assert effects == ["process", "inject", "history"]


def test_partial_event_cancellation_stops_consuming_remaining_chunks(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    capture.chunks = (b"first", b"second")
    holder: dict[str, RecognitionSession] = {}

    def sink(event) -> None:  # type: ignore[no-untyped-def]
        if event.type == "transcript":
            holder["session"].cancel()

    streamer = _Streamer()
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=streamer,
        event_sink=sink,
    )
    holder["session"] = session

    session.run()

    assert [event.type for event in session.events] == ["ready", "transcript", "cancelled"]
    assert streamer.accepted == 1
    assert capture.stopped == 0


def test_iterator_error_after_cancellation_does_not_emit_an_error(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    holder: dict[str, RecognitionSession] = {}

    def cancelled_iterator():  # type: ignore[no-untyped-def]
        holder["session"].cancel()
        raise OSError("取消后的读取错误")
        yield b"unreachable"

    capture.iter_chunks = cancelled_iterator  # type: ignore[method-assign]
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(),
    )
    holder["session"] = session

    session.run()

    assert [event.type for event in session.events] == ["ready", "cancelled"]


def test_failure_cleanup_errors_do_not_hide_original_recognition_error(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)

    def fail_cancel() -> None:
        raise OSError("取消进程失败")

    def fail_release() -> None:
        raise OSError("删除录音失败")

    capture.cancel = fail_cancel  # type: ignore[method-assign]
    capture.release_wav = fail_release  # type: ignore[method-assign]
    session = RecognitionSession(
        capture_factory=lambda: capture,
        streamer=_Streamer(fail=RuntimeError("原始 VAD 错误")),
    )

    session.run()

    assert [event.type for event in session.events] == ["ready", "error", "completed"]
    assert "原始 VAD 错误" in (session.events[1].message or "")
