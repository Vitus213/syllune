from __future__ import annotations

import json
import signal
from collections.abc import Callable

import pytest

from type4me_linux.cli import main
from type4me_linux.events import RecognitionEvent, RecognitionTranscript
from type4me_linux.inject import InjectionResult


def _transcript(
    text: str,
    *,
    final: bool,
    partial: str = "",
    backend: str = "sensevoice-vad",
) -> RecognitionTranscript:
    return RecognitionTranscript(
        confirmed_segments=(text,) if final else (),
        partial_text=partial,
        authoritative_text=text,
        is_final=final,
        backend=backend,
    )


def _event(
    event_type: str,
    sequence: int,
    *,
    transcript: RecognitionTranscript | None = None,
    message: str | None = None,
    injection: InjectionResult | None = None,
) -> RecognitionEvent:
    return RecognitionEvent(  # type: ignore[arg-type]
        event_type,
        sequence,
        transcript=transcript,
        message=message,
        injection=injection,
    )


class _ScriptedSession:
    def __init__(
        self,
        sink: Callable[[RecognitionEvent], None],
        events: tuple[RecognitionEvent, ...],
    ) -> None:
        self.sink = sink
        self.events = events
        self.calls: list[str] = []

    def run(self) -> None:
        self.calls.append("run")
        for event in self.events:
            self.sink(event)

    def stop(self) -> None:
        self.calls.append("stop")

    def cancel(self) -> None:
        self.calls.append("cancel")


def _install_pipeline(monkeypatch, events):  # type: ignore[no-untyped-def]
    sessions: list[_ScriptedSession] = []
    requests: list[object] = []
    configs: list[object] = []

    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            configs.append(config)

        def create_session(self, request):  # type: ignore[no-untyped-def]
            requests.append(request)
            session = _ScriptedSession(request.event_sink, tuple(events))
            sessions.append(session)
            return session

    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)
    return sessions, requests, configs


def test_json_stream_has_exact_keys_and_order(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    injection = InjectionResult("wtype", True, "")
    partial = _transcript("局部", final=False, partial="局部")
    final = _transcript("最终文本", final=True, backend="hybrid")
    events = (
        _event("ready", 1),
        _event("transcript", 2, transcript=partial),
        _event("transcript", 3, transcript=final),
        _event("finalized", 4, transcript=final, injection=injection),
        _event("completed", 5, transcript=final, injection=injection),
    )
    sessions, requests, configs = _install_pipeline(monkeypatch, events)

    code = main(["stream", "--mode", "快速输入", "--no-inject", "--json"])

    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert [payload["type"] for payload in payloads] == [
        "ready",
        "transcript",
        "transcript",
        "finalized",
        "completed",
    ]
    assert all(
        set(payload) == {"type", "sequence", "transcript", "message", "injection"}
        for payload in payloads
    )
    assert payloads[1]["transcript"] == {
        "confirmed_segments": [],
        "partial_text": "局部",
        "authoritative_text": "局部",
        "is_final": False,
        "backend": "sensevoice-vad",
    }
    assert payloads[3]["injection"] == {"ok": True, "method": "wtype", "message": ""}
    assert requests[0].mode == "快速输入"
    assert requests[0].inject is False
    assert configs[0].asr.streaming_backend == "sensevoice-vad"
    assert sessions[0].calls == ["run"]


def test_non_tty_stream_prints_only_one_final_stdout(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    final = _transcript("唯一最终文本", final=True, backend="hybrid")
    events = (
        _event("ready", 1),
        _event("transcript", 2, transcript=_transcript("草稿", final=False, partial="草稿")),
        _event("transcript", 3, transcript=final),
        _event("finalized", 4, transcript=final),
        _event("completed", 5, transcript=final),
    )
    _install_pipeline(monkeypatch, events)

    assert main(["stream", "--no-inject"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "唯一最终文本\n"
    assert "草稿" not in captured.out


def test_stream_error_is_exit_one_and_has_no_stdout(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _install_pipeline(
        monkeypatch,
        (
            _event("ready", 1),
            _event("error", 2, message="采集失败"),
            _event("completed", 3, message="采集失败"),
        ),
    )

    assert main(["stream", "--no-inject"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "采集失败\n"


def test_stream_cancel_event_exits_130(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _install_pipeline(monkeypatch, (_event("ready", 1), _event("cancelled", 2)))

    assert main(["stream", "--json"]) == 130
    assert [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()] == [
        "ready",
        "cancelled",
    ]


def _signal_pipeline(monkeypatch, *, signals: tuple[int, ...]):  # type: ignore[no-untyped-def]
    installed: dict[int, Callable[..., None]] = {}
    session_holder: list[object] = []

    def fake_signal(signum, handler):  # type: ignore[no-untyped-def]
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class _Session:
        def __init__(self, sink) -> None:  # type: ignore[no-untyped-def]
            self.sink = sink
            self.calls: list[str] = []
            self.sequence = 0

        def _emit(self, event_type: str, transcript=None) -> None:  # type: ignore[no-untyped-def]
            self.sequence += 1
            self.sink(_event(event_type, self.sequence, transcript=transcript))

        def run(self) -> None:
            self._emit("ready")
            for signum in signals:
                installed[signum](signum, None)

        def stop(self) -> None:
            self.calls.append("stop")
            final = _transcript("信号终结文本", final=True, backend="hybrid")
            self._emit("transcript", final)
            self._emit("finalized", final)
            self._emit("completed", final)

        def cancel(self) -> None:
            self.calls.append("cancel")
            self._emit("cancelled")

    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def create_session(self, request):  # type: ignore[no-untyped-def]
            session = _Session(request.event_sink)
            session_holder.append(session)
            return session

    monkeypatch.setattr("type4me_linux.cli.signal.signal", fake_signal)
    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)
    return session_holder


def test_first_sigint_stops_and_finalizes(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    sessions = _signal_pipeline(monkeypatch, signals=(signal.SIGINT,))

    assert main(["stream", "--no-inject"]) == 0
    assert sessions[0].calls == ["stop"]
    assert capsys.readouterr().out == "信号终结文本\n"


@pytest.mark.parametrize(
    "signals",
    [
        (signal.SIGINT, signal.SIGINT),
        (signal.SIGTERM,),
    ],
)
def test_second_sigint_or_sigterm_cancels_and_exits_130(monkeypatch, capsys, signals) -> None:  # type: ignore[no-untyped-def]
    sessions = _signal_pipeline(monkeypatch, signals=signals)

    assert main(["stream", "--json"]) == 130
    assert sessions[0].calls[-1] == "cancel"
    types = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
    assert "cancelled" in types
