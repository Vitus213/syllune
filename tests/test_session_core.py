from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from type4me_linux.events import RecognitionEvent, RecognitionTranscript
from type4me_linux.inject import InjectionResult
from type4me_linux.session import RecognitionSession


class _Capture:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls

    def stop(self) -> object:
        self.calls.append(("capture.stop", "finishing"))
        return "音频对象"

    def cancel(self) -> None:
        self.calls.append(("capture.cancel", "cancelled"))


def _transcript(
    text: str,
    *,
    partial: str = "",
    final: bool = False,
    backend: str = "fake",
) -> RecognitionTranscript:
    return RecognitionTranscript(
        confirmed_segments=(text,) if text else (),
        partial_text=partial,
        authoritative_text=text,
        is_final=final,
        backend=backend,
    )


def test_wire_records_have_exact_fields_and_are_immutable() -> None:
    transcript = _transcript("测试")
    injection = InjectionResult("test", True)
    event = RecognitionEvent(
        type="transcript",
        sequence=7,
        transcript=transcript,
        message=None,
        injection=injection,
    )

    assert [field.name for field in fields(RecognitionTranscript)] == [
        "confirmed_segments",
        "partial_text",
        "authoritative_text",
        "is_final",
        "backend",
    ]
    assert [field.name for field in fields(RecognitionEvent)] == [
        "type",
        "sequence",
        "transcript",
        "message",
        "injection",
    ]
    with pytest.raises(FrozenInstanceError):
        transcript.partial_text = "变更"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.sequence = 8  # type: ignore[misc]


def test_session_runs_ordered_lifecycle_and_owns_each_side_effect_once() -> None:
    calls: list[tuple[str, str]] = []
    capture = _Capture(calls)
    observed: list[RecognitionEvent] = []
    session: RecognitionSession

    def create_capture() -> _Capture:
        calls.append(("capture.create", session.state))
        return capture

    def finalize(artifact: object) -> RecognitionTranscript:
        assert artifact == "音频对象"
        calls.append(("finalize", session.state))
        return _transcript("原始文本", backend="sensevoice")

    def process(text: str) -> str:
        calls.append((f"process:{text}", session.state))
        return "处理后的文本"

    def inject(text: str) -> InjectionResult:
        calls.append((f"inject:{text}", session.state))
        return InjectionResult("test", True, "已注入")

    def write_history(
        raw: RecognitionTranscript,
        final: RecognitionTranscript,
        injection: InjectionResult | None,
    ) -> None:
        assert raw.authoritative_text == "原始文本"
        assert final.authoritative_text == "处理后的文本"
        assert injection == InjectionResult("test", True, "已注入")
        calls.append(("history.write", session.state))

    session = RecognitionSession(
        capture_factory=create_capture,
        finalizer=finalize,
        processor=process,
        history_writer=write_history,
        injector=inject,
        event_sink=observed.append,
    )

    session.start()
    session.publish_transcript(_transcript("", partial="正在识别"))
    session.stop()

    assert session.state == "idle"
    assert session.state_history == (
        "idle",
        "starting",
        "recording",
        "finishing",
        "processing",
        "injecting",
        "idle",
    )
    assert calls == [
        ("capture.create", "starting"),
        ("capture.stop", "finishing"),
        ("finalize", "finishing"),
        ("process:原始文本", "processing"),
        ("inject:处理后的文本", "injecting"),
        ("history.write", "injecting"),
    ]
    assert observed == list(session.events)
    assert [event.type for event in observed] == [
        "ready",
        "transcript",
        "transcript",
        "finalized",
        "completed",
    ]
    assert [event.sequence for event in observed] == [1, 2, 3, 4, 5]
    assert observed[1].transcript == _transcript("", partial="正在识别")
    final = observed[2].transcript
    assert final == RecognitionTranscript(
        confirmed_segments=("原始文本",),
        partial_text="",
        authoritative_text="处理后的文本",
        is_final=True,
        backend="sensevoice",
    )
    assert observed[3].injection == InjectionResult("test", True, "已注入")

    session.start()
    session.stop()
    session.cancel()
    assert len(session.events) == 5
    assert len(calls) == 6


def test_stop_is_idempotent_without_duplicate_side_effects() -> None:
    calls: list[tuple[str, str]] = []
    session = RecognitionSession(
        capture_factory=lambda: _Capture(calls),
        finalizer=lambda artifact: _transcript(str(artifact)),
        processor=lambda text: calls.append(("process", text)) or text,
        history_writer=lambda raw, final, injection: calls.append(
            ("history", final.authoritative_text)
        ),
        injector=lambda text: calls.append(("inject", text)) or InjectionResult("test", True),
    )

    session.start()
    session.start()
    session.stop()
    session.stop()

    assert [name for name, _ in calls].count("capture.stop") == 1
    assert [name for name, _ in calls].count("process") == 1
    assert [name for name, _ in calls].count("inject") == 1
    assert [name for name, _ in calls].count("history") == 1
    assert [event.type for event in session.events].count("completed") == 1
    assert [event.sequence for event in session.events] == list(range(1, len(session.events) + 1))


def test_cancel_is_idempotent_and_skips_terminal_side_effects() -> None:
    calls: list[tuple[str, str]] = []
    session = RecognitionSession(
        capture_factory=lambda: _Capture(calls),
        finalizer=lambda artifact: calls.append(("finalize", str(artifact))) or _transcript("x"),
        processor=lambda text: calls.append(("process", text)) or text,
        history_writer=lambda raw, final, injection: calls.append(("history", "called")),
        injector=lambda text: calls.append(("inject", text)) or InjectionResult("test", True),
    )

    session.start()
    session.cancel()
    session.cancel()
    session.stop()

    assert session.state_history == ("idle", "starting", "recording", "cancelled", "idle")
    assert calls == [("capture.cancel", "cancelled")]
    assert [event.type for event in session.events] == ["ready", "cancelled"]
    assert [event.sequence for event in session.events] == [1, 2]


def test_partial_transcripts_are_rejected_outside_recording() -> None:
    session = RecognitionSession(
        capture_factory=lambda: _Capture([]),
        finalizer=lambda artifact: _transcript(str(artifact)),
    )

    with pytest.raises(RuntimeError, match="录音中"):
        session.publish_transcript(_transcript("", partial="无效"))

    session.start()
    with pytest.raises(ValueError, match="最终转写"):
        session.publish_transcript(_transcript("错误", final=True))
