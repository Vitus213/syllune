from __future__ import annotations

import threading
from concurrent.futures import Executor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pytest

import type4me_linux.controller as controller_module
from type4me_linux.app_state import AppState
from type4me_linux.controller import ApplicationController
from type4me_linux.events import RecognitionEvent, RecognitionTranscript
from type4me_linux.history import HistoryPage, HistoryRecord
from type4me_linux.modes import BUILTIN_MODES, Mode


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[Callable[[], object]] = []

    def __call__(self, function: Callable[[], object]) -> None:
        self.calls.append(function)

    def drain(self) -> None:
        while self.calls:
            self.calls.pop(0)()


class _Worker:
    def __init__(self) -> None:
        self.calls: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    def submit(self, function: Callable[..., object], /, *args: object) -> None:
        self.calls.append((function, args))

    def run_next(self) -> None:
        function, args = self.calls.pop(0)
        function(*args)

    def drain(self) -> None:
        while self.calls:
            self.run_next()


class _Modes:
    def __init__(self) -> None:
        self.values = list(BUILTIN_MODES)

    def list(self) -> tuple[Mode, ...]:
        return tuple(self.values)

    def resolve(self, identifier: str | None = None) -> Mode:
        identifier = identifier or "quick"
        for mode in self.values:
            if mode.id == identifier or mode.name == identifier:
                return mode
        raise ValueError(f"没有模式：{identifier}")


class _ScriptedSession:
    def __init__(
        self,
        sink: Callable[[RecognitionEvent], None],
        events: tuple[RecognitionEvent, ...],
    ) -> None:
        self._sink = sink
        self._events = events
        self.run_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0

    def run(self) -> None:
        self.run_calls += 1
        for event in self._events:
            self._sink(event)

    def stop(self) -> None:
        self.stop_calls += 1

    def cancel(self) -> None:
        self.cancel_calls += 1


class _BlockingSession:
    def __init__(self, sink: Callable[[RecognitionEvent], None]) -> None:
        self._sink = sink
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.stop_calls = 0
        self.cancel_calls = 0
        self._sequence = 1

    def run(self) -> None:
        self._sink(RecognitionEvent("ready", 1))
        self.entered.set()
        assert self.finished.wait(timeout=2)

    def stop(self) -> None:
        self.stop_calls += 1
        transcript = _transcript("最终文本", final=True)
        self._sink(RecognitionEvent("transcript", 2, transcript=transcript))
        self._sink(RecognitionEvent("finalized", 3, transcript=transcript))
        self._sink(RecognitionEvent("completed", 4, transcript=transcript))
        self.finished.set()

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._sink(RecognitionEvent("cancelled", 2))
        self.finished.set()


def _transcript(text: str, *, final: bool = False) -> RecognitionTranscript:
    return RecognitionTranscript(
        confirmed_segments=(text,) if text else (),
        partial_text="" if final else text,
        authoritative_text=text if final else "",
        is_final=final,
        backend="sensevoice-vad",
    )


def _controller(
    factory,  # type: ignore[no-untyped-def]
    *,
    scheduler: _Scheduler | None = None,
    worker: _Worker | None = None,
    **kwargs,  # type: ignore[no-untyped-def]
) -> ApplicationController:
    return ApplicationController(
        session_factory=factory,
        modes=kwargs.pop("modes", _Modes()),
        scheduler=scheduler,
        worker=worker,
        model_ids=("sensevoice-int8", "silero-vad"),
        **kwargs,
    )


def test_start_is_nonblocking_and_events_reduce_only_on_scheduler() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    requests = []
    notifications: list[tuple[str, str]] = []
    transcript = _transcript("完成内容", final=True)
    events = (
        RecognitionEvent("ready", 1),
        RecognitionEvent("transcript", 2, transcript=transcript),
        RecognitionEvent("finalized", 3, transcript=transcript),
        RecognitionEvent("completed", 4, transcript=transcript),
    )

    def factory(request):  # type: ignore[no-untyped-def]
        requests.append(request)
        return _ScriptedSession(request.event_sink, events)

    controller = _controller(
        factory,
        scheduler=scheduler,
        worker=worker,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    initial = controller.state

    assert controller.toggle() is True
    assert requests == []
    assert controller.state is initial
    assert len(worker.calls) == 1

    scheduler.drain()
    starting = controller.state
    assert starting.session_state == "starting"
    assert starting is not initial
    with pytest.raises(FrozenInstanceError):
        starting.error = "不可修改"  # type: ignore[misc]

    worker.run_next()
    assert len(requests) == 1
    assert requests[0].mode == "quick"
    assert requests[0].inject is True
    assert controller.state is starting

    scheduler.drain()
    assert controller.state.session_state == "completed"
    assert controller.state.final_text == "完成内容"
    assert controller.state.last_event_sequence == 4
    assert notifications == [("录音已开始", "再次按快捷键结束录音。"), ("识别完成", "完成内容")]


def test_final_notification_uses_short_preview_for_long_text() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    notifications: list[tuple[str, str]] = []
    long_text = "这是一段很长的识别文本" * 8
    transcript = _transcript(long_text, final=True)
    events = (
        RecognitionEvent("ready", 1),
        RecognitionEvent("finalized", 2, transcript=transcript),
        RecognitionEvent("completed", 3, transcript=transcript),
    )

    controller = _controller(
        lambda request: _ScriptedSession(request.event_sink, events),
        scheduler=scheduler,
        worker=worker,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    assert controller.toggle() is True
    scheduler.drain()
    worker.run_next()
    scheduler.drain()

    assert notifications == [
        ("录音已开始", "再次按快捷键结束录音。"),
        ("识别完成", f"{long_text[:47]}…"),
    ]


def test_one_session_ownership_and_hold_actions_are_idempotent() -> None:
    scheduler = _Scheduler()
    sessions: list[_BlockingSession] = []
    notifications: list[tuple[str, str]] = []

    def factory(request):  # type: ignore[no-untyped-def]
        session = _BlockingSession(request.event_sink)
        sessions.append(session)
        return session

    controller = _controller(
        factory,
        scheduler=scheduler,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    try:
        assert controller.hold_start() is True
        assert controller.hold_start() is False
        scheduler.drain()

        assert _wait_until(lambda: bool(sessions) and sessions[0].entered.is_set())
        scheduler.drain()
        assert controller.state.session_state == "recording"
        assert controller.hold_start() is False
        assert notifications == [("录音已开始", "再次按快捷键结束录音。")]

        assert controller.hold_stop() is True
        assert controller.hold_stop() is False
        assert notifications == [
            ("录音已开始", "再次按快捷键结束录音。"),
            ("正在识别", "正在生成最终文本。"),
        ]
        assert _wait_until(lambda: sessions[0].finished.is_set())
        scheduler.drain()

        assert len(sessions) == 1
        assert sessions[0].stop_calls == 1
        assert sessions[0].cancel_calls == 0
        assert controller.state.session_state == "completed"
        assert controller.state.final_text == "最终文本"
        assert notifications == [
            ("录音已开始", "再次按快捷键结束录音。"),
            ("正在识别", "正在生成最终文本。"),
            ("识别完成", "最终文本"),
        ]
    finally:
        controller.close()


def test_toggle_session_ignores_hold_release_and_duplicate_stop() -> None:
    scheduler = _Scheduler()
    sessions: list[_BlockingSession] = []

    def factory(request):  # type: ignore[no-untyped-def]
        session = _BlockingSession(request.event_sink)
        sessions.append(session)
        return session

    controller = _controller(factory, scheduler=scheduler)
    try:
        assert controller.toggle() is True
        assert _wait_until(lambda: bool(sessions) and sessions[0].entered.is_set())
        scheduler.drain()

        assert controller.hold_stop() is False
        assert controller.toggle() is True
        assert controller.toggle() is False
        assert _wait_until(lambda: sessions[0].finished.is_set())
        scheduler.drain()

        assert len(sessions) == 1
        assert sessions[0].stop_calls == 1
        assert controller.state.session_state == "completed"
    finally:
        controller.close()


def test_error_and_cancel_terminal_states_are_preserved() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    notifications: list[tuple[str, str]] = []
    error_events = (
        RecognitionEvent("ready", 1),
        RecognitionEvent("error", 2, message="识别失败：模型损坏"),
        RecognitionEvent("completed", 3, message="识别失败：模型损坏"),
    )

    def error_factory(request):  # type: ignore[no-untyped-def]
        return _ScriptedSession(request.event_sink, error_events)

    controller = _controller(
        error_factory,
        scheduler=scheduler,
        worker=worker,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    assert controller.toggle() is True
    scheduler.drain()
    worker.drain()
    scheduler.drain()
    assert controller.state.session_state == "error"
    assert controller.state.error == "识别失败：模型损坏"
    assert notifications == [
        ("录音已开始", "再次按快捷键结束录音。"),
        ("识别失败", "识别失败：模型损坏"),
    ]

    cancel_scheduler = _Scheduler()
    sessions: list[_BlockingSession] = []

    def blocking_factory(request):  # type: ignore[no-untyped-def]
        session = _BlockingSession(request.event_sink)
        sessions.append(session)
        return session

    cancelling = _controller(blocking_factory, scheduler=cancel_scheduler)
    try:
        assert cancelling.toggle() is True
        assert _wait_until(lambda: bool(sessions) and sessions[0].entered.is_set())
        cancel_scheduler.drain()
        assert cancelling.cancel() is True
        assert cancelling.cancel() is False
        assert _wait_until(lambda: sessions[0].finished.is_set())
        cancel_scheduler.drain()
        assert sessions[0].cancel_calls == 1
        assert cancelling.state.session_state == "cancelled"
        assert cancelling.state.transcript is None
    finally:
        cancelling.close()


def test_mode_history_model_shortcut_and_window_projections() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    modes = _Modes()
    custom = Mode(
        id="11111111-1111-4111-8111-111111111111",
        name="会议记录",
        prompt="{text}",
        processing_label="整理中",
        builtin=False,
        sort_order=10,
    )
    modes.values.append(custom)
    created_at = datetime(2026, 7, 13, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    page = HistoryPage(
        records=(
            HistoryRecord(
                id="history-1",
                created_at=created_at,
                duration_seconds=1.5,
                raw_text="原文",
                processing_mode=custom.id,
                processed_text="终稿",
                final_text="终稿",
                status="completed",
                character_count=2,
                asr_provider="hybrid",
                asr_model="qwen3-asr-0.6b-int8",
            ),
        ),
        next_cursor="下一页",
    )

    class History:
        def query(self, *, limit=50, cursor=None):  # type: ignore[no-untyped-def]
            assert (limit, cursor) == (12, "游标")
            return page

    class Models:
        def check(self, model_id: str) -> dict[str, object]:
            if model_id == "silero-vad":
                raise OSError("清单不可读")
            return {
                "id": model_id,
                "installed": True,
                "ok": True,
                "version": "v1",
                "path": Path("/models/current"),
                "missing": [],
                "extra": [],
                "corrupt": [],
                "errors": [],
            }

    shown: list[str] = []
    controller = _controller(
        lambda request: _ScriptedSession(request.event_sink, ()),
        scheduler=scheduler,
        worker=worker,
        modes=modes,
        history=History(),
        models=Models(),
        show_window_callback=lambda: shown.append("shown"),
    )

    assert controller.select_mode(custom.id) is custom
    controller.refresh_modes()
    controller.refresh_history(limit=12, cursor="游标")
    controller.refresh_model_checks()
    controller.set_shortcut_state(
        "bound",
        ("hold-to-talk", "toggle-recording"),
        "全局快捷键已绑定。",
    )
    controller.show_window()

    assert controller.state.selected_mode.id == "quick"
    assert shown == []
    worker.drain()
    assert controller.state.history_page is None
    scheduler.drain()

    state = controller.state
    assert state.selected_mode is custom
    assert state.modes[-1] is custom
    assert state.history_page == page
    assert [(item.id, item.ok) for item in state.model_checks] == [
        ("sensevoice-int8", True),
        ("silero-vad", False),
    ]
    assert state.model_checks[1].errors == ("无法检查模型：清单不可读",)
    assert state.shortcuts.bound_ids == frozenset({"hold-to-talk", "toggle-recording"})
    assert state.shortcuts.message == "全局快捷键已绑定。"
    assert shown == ["shown"]


def _wait_until(predicate: Callable[[], bool]) -> bool:
    for _ in range(200):
        if predicate():
            return True
        threading.Event().wait(0.01)
    return False


def test_subscriptions_emit_snapshots_and_unsubscribe_idempotently() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    controller = _controller(
        lambda request: _ScriptedSession(request.event_sink, ()),
        scheduler=scheduler,
        worker=worker,
    )
    received: list[AppState] = []
    silent_received: list[AppState] = []

    unsubscribe = controller.subscribe(received.append)
    silent_unsubscribe = controller.subscribe(silent_received.append, emit_current=False)
    assert received == []
    scheduler.drain()
    assert received == [controller.state]
    assert silent_received == []

    controller.set_shortcut_state("binding", message="正在绑定")
    scheduler.drain()
    assert received[-1].shortcuts.status == "binding"
    assert silent_received[-1].shortcuts.status == "binding"

    unsubscribe()
    unsubscribe()
    silent_unsubscribe()
    controller.set_shortcut_state("unavailable", message="不可用")
    scheduler.drain()
    assert len(received) == 2
    assert len(silent_received) == 1


def test_initial_and_selected_mode_loader_failures_are_projected() -> None:
    class BrokenModes:
        def list(self) -> tuple[Mode, ...]:
            raise OSError("模式文件损坏")

        def resolve(self, identifier: str | None = None) -> Mode:
            raise AssertionError("初始化应在 list 失败后停止")

    broken = _controller(
        lambda request: _ScriptedSession(request.event_sink, ()),
        modes=BrokenModes(),
        worker=_Worker(),
    )
    assert broken.state.modes == ()
    assert broken.state.selected_mode is None
    assert broken.state.error == "无法加载输入模式：模式文件损坏"

    scheduler = _Scheduler()
    valid = _controller(
        lambda request: _ScriptedSession(request.event_sink, ()),
        scheduler=scheduler,
        worker=_Worker(),
    )
    assert valid.select_mode("不存在") is None
    assert valid.state.error is None
    scheduler.drain()
    assert valid.state.error == "无法选择输入模式：没有模式：不存在"


def test_projection_loader_failures_and_absent_repositories() -> None:
    scheduler = _Scheduler()
    worker = _Worker()

    class FailingModes(_Modes):
        fail = False

        def list(self) -> tuple[Mode, ...]:
            if self.fail:
                raise OSError("模式读取失败")
            return super().list()

    class FailingHistory:
        def query(self, *, limit=50, cursor=None):  # type: ignore[no-untyped-def]
            raise OSError(f"历史读取失败 {limit} {cursor}")

    class MixedModels:
        def check(self, model_id: str) -> object:
            if model_id == "silero-vad":
                return "不是字典"
            return {
                "id": 42,
                "installed": 1,
                "ok": 0,
                "version": 7,
                "path": Path("/模型"),
                "missing": "tokens.txt",
                "extra": ["临时文件"],
                "corrupt": (),
                "errors": "校验失败",
            }

    modes = FailingModes()
    controller = _controller(
        lambda request: _ScriptedSession(request.event_sink, ()),
        scheduler=scheduler,
        worker=worker,
        modes=modes,
        history=FailingHistory(),
        models=MixedModels(),
    )
    modes.fail = True
    controller.refresh_modes()
    worker.run_next()
    assert controller.state.error is None
    scheduler.drain()
    assert controller.state.error == "无法加载输入模式：模式读取失败"

    controller.refresh_history(limit=8, cursor="下一页")
    worker.run_next()
    assert controller.state.error == "无法加载输入模式：模式读取失败"
    scheduler.drain()
    assert controller.state.error == "无法加载识别历史：历史读取失败 8 下一页"

    controller.refresh_model_checks()
    worker.run_next()
    assert controller.state.model_checks == ()
    scheduler.drain()
    checks = controller.state.model_checks
    assert checks[0].id == "42"
    assert checks[0].installed is True
    assert checks[0].ok is False
    assert checks[0].version == "7"
    assert checks[0].path == "/模型"
    assert checks[0].missing == ("tokens.txt",)
    assert checks[0].extra == ("临时文件",)
    assert checks[0].errors == ("校验失败",)
    assert checks[1].errors == ("模型检查返回了无效结果。",)

    absent = _controller(
        lambda request: _ScriptedSession(request.event_sink, ()),
        scheduler=scheduler,
        worker=worker,
    )
    absent.refresh_history()
    absent.refresh_model_checks()
    absent.show_window()
    scheduler.drain()
    assert absent.state.history_page is None
    assert absent.state.model_checks == ()


def test_old_session_events_and_queued_updates_cannot_overwrite_new_generation() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    requests = []
    final_b = _transcript("第二次结果", final=True)

    def factory(request):  # type: ignore[no-untyped-def]
        requests.append(request)
        if len(requests) == 1:
            return _ScriptedSession(
                request.event_sink,
                (
                    RecognitionEvent("ready", 1),
                    RecognitionEvent("warning", 2, message="过期警告"),
                    RecognitionEvent("completed", 3),
                ),
            )
        return _ScriptedSession(
            request.event_sink,
            (
                RecognitionEvent("ready", 1),
                RecognitionEvent("finalized", 2, transcript=final_b),
                RecognitionEvent("completed", 3, transcript=final_b),
            ),
        )

    controller = _controller(factory, scheduler=scheduler, worker=worker)
    assert controller.toggle() is True
    worker.run_next()
    assert len(scheduler.calls) == 4

    scheduler.calls.pop()()
    assert controller.state.session_state == "completed"
    assert controller.toggle() is True
    scheduler.drain()
    worker.run_next()
    scheduler.drain()
    assert controller.state.final_text == "第二次结果"
    assert controller.state.warnings == ()

    queued_before = len(scheduler.calls)
    requests[0].event_sink(RecognitionEvent("error", 100, message="过期错误"))
    assert len(scheduler.calls) == queued_before
    assert controller.state.error is None


def test_warning_ordering_and_empty_final_notification_contract() -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    notifications: list[tuple[str, str]] = []
    snapshots: list[AppState] = []
    events = (
        RecognitionEvent("ready", 1),
        RecognitionEvent("warning", 2),
        RecognitionEvent("warning", 3, message="校准不可用"),
        RecognitionEvent("warning", 3, message="重复消息"),
        RecognitionEvent("warning", 2, message="乱序消息"),
        RecognitionEvent("finalized", 4),
        RecognitionEvent("completed", 5),
    )
    controller = _controller(
        lambda request: _ScriptedSession(request.event_sink, events),
        scheduler=scheduler,
        worker=worker,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    controller.subscribe(snapshots.append, emit_current=False)
    assert controller.toggle() is True
    scheduler.drain()
    worker.run_next()
    scheduler.drain()

    assert controller.state.session_state == "completed"
    assert controller.state.warnings == ("校准不可用",)
    assert controller.state.last_event_sequence == 5
    assert notifications == [
        ("录音已开始", "再次按快捷键结束录音。"),
        ("识别完成", "语音输入已完成。"),
    ]
    assert snapshots[-1] is controller.state


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("factory", "识别失败：无法创建会话"),
        ("run", "识别失败：录音线程崩溃"),
        ("missing-terminal", "识别失败：识别会话结束时未发布完成事件"),
    ],
)
def test_session_worker_failures_clear_ownership_and_notify(failure: str, message: str) -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    notifications: list[tuple[str, str]] = []

    class Session:
        def run(self) -> None:
            if failure == "run":
                raise OSError("录音线程崩溃")

        def stop(self) -> None:
            raise AssertionError("不应停止")

        def cancel(self) -> None:
            raise AssertionError("不应取消")

    def factory(_request):  # type: ignore[no-untyped-def]
        if failure == "factory":
            raise OSError("无法创建会话")
        return Session()

    controller = _controller(
        factory,
        scheduler=scheduler,
        worker=worker,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    assert controller.toggle() is True
    scheduler.drain()
    worker.run_next()
    scheduler.drain()

    assert controller.state.session_state == "error"
    assert controller.state.error == message
    assert notifications == [("识别失败", message)]
    assert controller.cancel() is False


@pytest.mark.parametrize(("action", "terminal"), [("toggle", "completed"), ("cancel", "cancelled")])
def test_stop_or_cancel_while_factory_is_pending_aborts_without_running(
    action: str,
    terminal: str,
) -> None:
    scheduler = _Scheduler()
    worker = _Worker()
    sessions: list[_ScriptedSession] = []

    def factory(request):  # type: ignore[no-untyped-def]
        session = _ScriptedSession(request.event_sink, ())
        sessions.append(session)
        return session

    controller = _controller(factory, scheduler=scheduler, worker=worker)
    assert controller.toggle() is True
    assert getattr(controller, action)() is True
    assert getattr(controller, action)() is False
    scheduler.drain()
    worker.run_next()
    scheduler.drain()

    assert len(sessions) == 1
    assert sessions[0].run_calls == 0
    assert sessions[0].stop_calls == 0
    assert sessions[0].cancel_calls == 0
    assert controller.state.session_state == terminal


@pytest.mark.parametrize(("action", "failure"), [("toggle", "停止失败"), ("cancel", "取消失败")])
def test_active_session_control_worker_failures_are_reported(action: str, failure: str) -> None:
    scheduler = _Scheduler()
    notifications: list[tuple[str, str]] = []
    sessions = []

    class Session:
        def __init__(self, sink: Callable[[RecognitionEvent], None]) -> None:
            self.sink = sink
            self.entered = threading.Event()
            self.release = threading.Event()

        def run(self) -> None:
            self.sink(RecognitionEvent("ready", 1))
            self.entered.set()
            assert self.release.wait(timeout=2)
            self.sink(RecognitionEvent("completed", 2))

        def stop(self) -> None:
            raise OSError("停止失败")

        def cancel(self) -> None:
            raise OSError("取消失败")

    def factory(request):  # type: ignore[no-untyped-def]
        session = Session(request.event_sink)
        sessions.append(session)
        return session

    controller = _controller(
        factory,
        scheduler=scheduler,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    try:
        assert controller.toggle() is True
        assert _wait_until(lambda: bool(sessions) and sessions[0].entered.is_set())
        scheduler.drain()
        assert getattr(controller, action)() is True

        def failure_arrived() -> bool:
            scheduler.drain()
            return controller.state.error == f"识别失败：{failure}"

        assert _wait_until(failure_arrived)
        expected_notifications = [("录音已开始", "再次按快捷键结束录音。")]
        if action == "toggle":
            expected_notifications.append(("正在识别", "正在生成最终文本。"))
        expected_notifications.append(("识别失败", f"识别失败：{failure}"))
        assert notifications == expected_notifications
        sessions[0].release.set()
        assert _wait_until(lambda: len(scheduler.calls) > 0)
        scheduler.drain()
        assert controller.state.error == f"识别失败：{failure}"
    finally:
        if sessions:
            sessions[0].release.set()
        controller.close()


def test_default_scheduler_applies_updates_immediately() -> None:
    worker = _Worker()
    transcript = _transcript("同步投影", final=True)
    controller = _controller(
        lambda request: _ScriptedSession(
            request.event_sink,
            (
                RecognitionEvent("finalized", 1, transcript=transcript),
                RecognitionEvent("completed", 2, transcript=transcript),
            ),
        ),
        worker=worker,
    )
    assert controller.toggle() is True
    assert controller.state.session_state == "starting"
    worker.run_next()
    assert controller.state.session_state == "completed"
    assert controller.state.final_text == "同步投影"


def test_close_shuts_down_an_owned_executor_and_is_safe_when_idle(monkeypatch) -> None:
    class OwnedExecutor(Executor):
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("空闲关闭不应提交任务")

        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    executor = OwnedExecutor()
    monkeypatch.setattr(controller_module, "ThreadPoolExecutor", lambda **_kwargs: executor)
    controller = _controller(lambda request: _ScriptedSession(request.event_sink, ()))

    controller.close()
    controller.close()
    assert executor.shutdown_calls == [(False, False), (False, False)]
