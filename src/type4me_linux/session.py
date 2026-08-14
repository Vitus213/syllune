from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol

from .events import RecognitionEvent, RecognitionTranscript
from .inject import InjectionResult
from .providers import ASRProvider, RecognitionResult

SessionState = Literal[
    "idle",
    "starting",
    "recording",
    "finishing",
    "processing",
    "injecting",
    "cancelled",
]


class CaptureHandle(Protocol):
    def stop(self) -> object:
        """停止采集并返回供最终识别使用的对象。"""

    def cancel(self) -> None:
        """取消采集并清理其拥有的资源。"""


class StreamingCaptureHandle(CaptureHandle, Protocol):
    def start(self) -> object: ...

    def iter_chunks(self) -> Iterable[bytes]: ...
    def request_stop(self) -> None: ...

    def release_wav(self) -> None: ...


class TranscriptStreamer(Protocol):
    def accept_chunk(self, pcm16le: bytes) -> tuple[RecognitionTranscript, ...]: ...

    def flush(self) -> RecognitionTranscript: ...


CaptureFactory = Callable[[], CaptureHandle]
Finalizer = Callable[[object], RecognitionTranscript]
Processor = Callable[[str], str]
HistoryWriter = Callable[
    [RecognitionTranscript, RecognitionTranscript, InjectionResult | None], None
]
FailedHistoryWriter = Callable[[str], None]
Injector = Callable[[str], InjectionResult]
EventSink = Callable[[RecognitionEvent], None]


class RecognitionSession:
    """一次识别任务的唯一生命周期与副作用边界。"""

    def __init__(
        self,
        *,
        capture_factory: CaptureFactory,
        finalizer: Finalizer | None = None,
        streamer: TranscriptStreamer | None = None,
        calibrator: ASRProvider | None = None,
        processor: Processor | None = None,
        history_writer: HistoryWriter | None = None,
        failed_history_writer: FailedHistoryWriter | None = None,
        injector: Injector | None = None,
        event_sink: EventSink | None = None,
        startup_warnings: Iterable[str] = (),
        calibration_backend: str = "hybrid",
    ) -> None:
        if finalizer is None and streamer is None:
            raise ValueError("识别会话必须配置批量终结器或流式识别器")
        self._capture_factory = capture_factory
        self._finalizer = finalizer
        self._streamer = streamer
        self._calibrator = calibrator
        self._calibration_backend = calibration_backend
        self._processor = processor or _identity
        self._history_writer = history_writer
        self._failed_history_writer = failed_history_writer
        self._injector = injector
        self._event_sink = event_sink
        self._startup_warnings = tuple(str(message) for message in startup_warnings)

        self._lock = RLock()
        self._state: SessionState = "idle"
        self._states: list[SessionState] = ["idle"]
        self._events: list[RecognitionEvent] = []
        self._sequence = 0
        self._capture: CaptureHandle | None = None
        self._started = False
        self._terminal = False
        self._was_cancelled = False
        self._failure_recorded = False
        self._consuming = False
        self._stop_requested = False
        self._finalizing = False

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def state_history(self) -> tuple[SessionState, ...]:
        with self._lock:
            return tuple(self._states)

    @property
    def events(self) -> tuple[RecognitionEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def start(self) -> None:
        with self._lock:
            if self._started or self._terminal:
                return
            self._started = True
            self._transition("starting")
            try:
                capture = self._capture_factory()
                self._capture = capture
                start = getattr(capture, "start", None)
                if callable(start):
                    start()
            except Exception as exc:
                self._fail(exc)
                return
            self._transition("recording")
            self._emit("ready")
            for message in self._startup_warnings:
                self._emit("warning", message=message)

    def run(self) -> None:
        """同步消费原始采集流；采集 EOF 会按正常停止处理。"""

        self.start()
        self.consume()

    def consume(self) -> None:
        with self._lock:
            if self._state != "recording" or self._consuming:
                return
            capture = self._capture
            streamer = self._streamer
        if capture is None or streamer is None:
            self._fail(RuntimeError("当前识别会话未配置原始音频流"))
            return
        iterator = getattr(capture, "iter_chunks", None)
        if not callable(iterator):
            self._fail(RuntimeError("采集句柄不支持原始音频分块"))
            return

        with self._lock:
            if self._state != "recording" or self._consuming:
                return
            self._consuming = True

        failure: Exception | None = None
        finalize_capture: CaptureHandle | None = None
        missing_capture = False
        try:
            for chunk in iterator():
                with self._lock:
                    if self._was_cancelled or self._state != "recording":
                        break
                for transcript in streamer.accept_chunk(chunk):
                    with self._lock:
                        if self._was_cancelled or self._state != "recording":
                            break
                    self.publish_transcript(transcript)
                with self._lock:
                    if self._was_cancelled or self._state != "recording":
                        break
        except Exception as exc:
            failure = exc
        finally:
            with self._lock:
                self._consuming = False
                cancelled = self._was_cancelled
                if not cancelled and failure is not None:
                    self._terminal = True
                elif (
                    not cancelled
                    and self._state == "recording"
                    and not self._terminal
                    and not self._finalizing
                ):
                    finalize_capture = self._capture
                    self._stop_requested = True
                    self._terminal = True
                    if finalize_capture is None:
                        missing_capture = True
                    else:
                        self._finalizing = True
                        self._transition("finishing")

        if cancelled:
            return
        if failure is not None:
            self._fail(failure)
            return
        if missing_capture:
            self._fail(RuntimeError("识别会话缺少采集句柄"))
            return
        if finalize_capture is not None:
            self._finalize_stop(finalize_capture)

    def publish_transcript(self, transcript: RecognitionTranscript) -> None:
        with self._lock:
            if self._state != "recording":
                raise RuntimeError("只有录音中的识别会话才能发布转写结果")
            if transcript.is_final:
                raise ValueError("录音阶段不能发布最终转写结果")
            self._emit("transcript", transcript=transcript)

    def publish_warning(self, message: str) -> RecognitionEvent | None:
        """在终结或文本处理阶段发布非致命警告。"""

        with self._lock:
            if self._state not in {"finishing", "processing"}:
                return None
            return self._emit("warning", message=str(message))

    def stop(self) -> None:
        with self._lock:
            if self._terminal or self._state != "recording" or self._finalizing:
                return
            capture = self._capture
            if capture is None:
                missing_capture = True
                deferred = False
                request_stop = None
            elif self._consuming:
                missing_capture = False
                deferred = True
                if self._stop_requested:
                    return
                self._stop_requested = True
                request_stop = getattr(capture, "request_stop", None)
            else:
                missing_capture = False
                deferred = False
                request_stop = None
                self._stop_requested = True
                self._terminal = True
                self._finalizing = True
                self._transition("finishing")

        if missing_capture:
            self._fail(RuntimeError("识别会话缺少采集句柄"))
            return
        if capture is None:
            return
        if deferred:
            if callable(request_stop):
                try:
                    request_stop()
                except Exception as exc:
                    self._fail(exc)
            return
        self._finalize_stop(capture)

    def _finalize_stop(self, capture: CaptureHandle) -> None:
        try:
            artifact = capture.stop()
            if self._is_cancelled():
                return
            raw_transcript = self._finish_sensevoice(artifact)
        except Exception as exc:
            if self._is_cancelled():
                return
            self._fail(exc)
            return
        if self._is_cancelled():
            return

        calibrated = self._calibrate(raw_transcript, artifact)
        if self._is_cancelled():
            return
        with self._lock:
            if self._was_cancelled:
                return
            self._transition("processing")
        final_transcript = self._process(calibrated)
        if self._is_cancelled():
            return
        with self._lock:
            if self._was_cancelled:
                return
            self._emit("transcript", transcript=final_transcript)
            if self._was_cancelled:
                return
            self._transition("injecting")

        try:
            injection = (
                self._injector(final_transcript.authoritative_text)
                if self._injector is not None
                else None
            )
            if self._is_cancelled():
                return
            if self._history_writer is not None:
                self._history_writer(raw_transcript, final_transcript, injection)
            if self._is_cancelled():
                return
        except Exception as exc:
            if self._is_cancelled():
                return
            self._fail(exc)
            return

        with self._lock:
            if self._was_cancelled:
                return
            self._emit("finalized", transcript=final_transcript, injection=injection)
            if self._was_cancelled:
                return
            self._capture = None
            self._finalizing = False
            self._transition("idle")
            self._emit("completed", transcript=final_transcript, injection=injection)
        self._release_capture_wav(capture)

    def cancel(self) -> None:
        with self._lock:
            cancellable = {"starting", "recording", "finishing", "processing", "injecting"}
            if self._was_cancelled or self._state not in cancellable:
                return
            self._terminal = True
            self._was_cancelled = True
            self._finalizing = False
            self._transition("cancelled")
            capture = self._capture
            self._capture = None

        if capture is not None:
            try:
                capture.cancel()
            except Exception:
                pass
            self._release_capture_wav(capture)

        with self._lock:
            self._emit("cancelled")
            self._transition("idle")

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._was_cancelled

    def _finish_sensevoice(self, artifact: object) -> RecognitionTranscript:
        if self._streamer is not None:
            transcript = self._streamer.flush()
        else:
            if self._finalizer is None:
                raise RuntimeError("识别会话缺少批量终结器")
            transcript = self._finalizer(artifact)
        return replace(transcript, partial_text="", is_final=True)

    def _calibrate(
        self,
        sensevoice: RecognitionTranscript,
        artifact: object,
    ) -> RecognitionTranscript:
        if self._calibrator is None:
            return sensevoice
        if not isinstance(artifact, (str, Path)):
            self.publish_warning("最终校准缺少 WAV 文件，已保留草稿结果")
            return replace(sensevoice, backend="hybrid-fallback")
        try:
            result: RecognitionResult = self._calibrator.transcribe(Path(artifact))
            if not result.text.strip():
                raise ValueError("最终校准模型返回了空文本")
        except Exception as exc:
            self.publish_warning(f"最终校准失败，已保留草稿结果：{exc}")
            return replace(sensevoice, backend="hybrid-fallback")
        return replace(
            sensevoice,
            partial_text="",
            authoritative_text=result.text.strip(),
            is_final=True,
            backend=self._calibration_backend,
        )

    def _process(self, transcript: RecognitionTranscript) -> RecognitionTranscript:
        try:
            processed_text = self._processor(transcript.authoritative_text)
        except Exception as exc:
            self.publish_warning(f"文本处理失败，已保留识别原文：{exc}")
            processed_text = transcript.authoritative_text
        return replace(
            transcript,
            partial_text="",
            authoritative_text=processed_text,
            is_final=True,
        )

    def _fail(self, error: Exception) -> None:
        with self._lock:
            if self._was_cancelled:
                return
            if any(event.type == "error" for event in self._events):
                return
            self._terminal = True
            capture = self._capture
            if capture is not None:
                try:
                    capture.cancel()
                except Exception:
                    pass
                self._release_capture_wav(capture)
            self._capture = None
            message = f"识别失败：{error}"
            self._emit("error", message=message)
            if self._failed_history_writer is not None and not self._failure_recorded:
                self._failure_recorded = True
                try:
                    self._failed_history_writer(message)
                except Exception:
                    pass
            self._emit("completed", message=message)
            if self._state != "idle":
                self._transition("idle")

    @staticmethod
    def _release_capture_wav(capture: CaptureHandle) -> None:
        release = getattr(capture, "release_wav", None)
        if callable(release):
            try:
                release()
            except Exception:
                pass

    def _transition(self, target: SessionState) -> None:
        if target == self._state:
            return
        allowed: dict[SessionState, frozenset[SessionState]] = {
            "idle": frozenset({"starting"}),
            "starting": frozenset({"recording", "cancelled", "idle"}),
            "recording": frozenset({"finishing", "cancelled", "idle"}),
            "finishing": frozenset({"processing", "cancelled", "idle"}),
            "processing": frozenset({"injecting", "cancelled", "idle"}),
            "injecting": frozenset({"cancelled", "idle"}),
            "cancelled": frozenset({"idle"}),
        }
        if target not in allowed[self._state]:
            raise RuntimeError(f"无效的识别会话状态转换：{self._state} -> {target}")
        self._state = target
        self._states.append(target)

    def _emit(
        self,
        event_type: Literal[
            "ready",
            "transcript",
            "warning",
            "error",
            "cancelled",
            "completed",
            "finalized",
        ],
        *,
        transcript: RecognitionTranscript | None = None,
        message: str | None = None,
        injection: InjectionResult | None = None,
    ) -> RecognitionEvent:
        self._sequence += 1
        event = RecognitionEvent(
            type=event_type,
            sequence=self._sequence,
            transcript=transcript,
            message=message,
            injection=injection,
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)
        return event


def _identity(text: str) -> str:
    return text
