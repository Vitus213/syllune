from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import replace
from typing import Protocol, TypeAlias

from .app_state import AppState, ModelCheck, ShortcutProjection, ShortcutState
from .events import RecognitionEvent
from .history import HistoryPage
from .model_catalog import MODEL_CATALOG
from .modes import Mode
from .pipeline import RecognitionRequest

ScheduledCall: TypeAlias = Callable[[], object]
Scheduler: TypeAlias = Callable[[ScheduledCall], object]
StateListener: TypeAlias = Callable[[AppState], object]
Notifier: TypeAlias = Callable[[str, str], object]


class LiveSession(Protocol):
    def run(self) -> None: ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...


class SessionFactory(Protocol):
    def __call__(self, request: RecognitionRequest) -> LiveSession: ...


class ModesProjectionRepository(Protocol):
    def list(self) -> tuple[Mode, ...]: ...

    def resolve(self, identifier: str | None = None) -> Mode: ...


class HistoryProjectionRepository(Protocol):
    def query(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> HistoryPage: ...


class ModelProjectionRepository(Protocol):
    def check(self, model_id: str) -> dict[str, object]: ...


class Worker(Protocol):
    def submit(self, function: Callable[..., object], /, *args: object) -> object: ...


class ApplicationController:
    """桌面层唯一的实时识别会话所有者。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        modes: ModesProjectionRepository,
        history: HistoryProjectionRepository | None = None,
        models: ModelProjectionRepository | None = None,
        scheduler: Scheduler | None = None,
        worker: Worker | None = None,
        notifier: Notifier | None = None,
        show_window_callback: Callable[[], object] | None = None,
        model_ids: Iterable[str] = MODEL_CATALOG,
        selected_mode: str | None = None,
        inject: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._modes = modes
        self._history = history
        self._models = models
        self._schedule = scheduler or _run_now
        self._owned_worker = worker is None
        self._worker: Worker = worker or ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="type4me-controller",
        )
        self._notifier = notifier
        self._show_window_callback = show_window_callback
        self._model_ids = tuple(model_ids)
        self._inject = inject

        self._lock = threading.RLock()
        self._listeners: list[StateListener] = []
        self._session: LiveSession | None = None
        self._starting = False
        self._activation: str | None = None
        self._stop_requested = False
        self._cancel_requested = False
        self._terminal_seen = False
        self._generation = 0

        try:
            available_modes = tuple(modes.list())
            current_mode = modes.resolve(selected_mode)
            initial_error = None
        except Exception as exc:
            available_modes = ()
            current_mode = None
            initial_error = f"无法加载输入模式：{exc}"
        self._selected_mode = current_mode
        self._state = AppState(
            selected_mode=current_mode,
            modes=available_modes,
            error=initial_error,
        )

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    def subscribe(
        self, listener: StateListener, *, emit_current: bool = True
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
            current = self._state
        if emit_current:
            self._schedule(lambda: listener(current))

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def toggle(self) -> bool:
        with self._lock:
            active = self._starting or self._session is not None
        if active:
            return self._request_stop(require_hold=False)
        return self._start("toggle")

    def hold_start(self) -> bool:
        return self._start("hold")

    def hold_stop(self) -> bool:
        return self._request_stop(require_hold=True)

    def cancel(self) -> bool:
        with self._lock:
            if not self._starting and self._session is None:
                return False
            if self._cancel_requested:
                return False
            self._cancel_requested = True
            token = self._generation
            session = self._session
        self._publish_for(token, lambda state: replace(state, session_state="stopping"))
        if session is not None:
            self._worker.submit(self._cancel_session, token, session)
        return True

    def show_window(self) -> None:
        callback = self._show_window_callback
        if callback is not None:
            self._schedule(callback)

    def select_mode(self, identifier: str) -> Mode | None:
        try:
            selected = self._modes.resolve(identifier)
            modes = tuple(self._modes.list())
        except Exception as exc:
            message = f"无法选择输入模式：{exc}"
            self._publish(lambda state, message=message: replace(state, error=message))
            return None
        with self._lock:
            self._selected_mode = selected
        self._publish(lambda state: replace(state, selected_mode=selected, modes=modes, error=None))
        return selected

    def refresh_modes(self) -> None:
        self._worker.submit(self._load_modes)

    def refresh_history(self, *, limit: int = 50, cursor: str | None = None) -> None:
        if self._history is None:
            self._publish(lambda state: replace(state, history_page=None))
            return
        self._worker.submit(self._load_history, limit, cursor)

    def refresh_model_checks(self) -> None:
        if self._models is None:
            self._publish(lambda state: replace(state, model_checks=()))
            return
        self._worker.submit(self._load_model_checks)

    def set_shortcut_state(
        self,
        status: ShortcutProjection,
        bound_ids: Iterable[str] = (),
        message: str | None = None,
    ) -> None:
        shortcut_state = ShortcutState(
            status=status,
            bound_ids=frozenset(str(item) for item in bound_ids),
            message=message,
        )
        self._publish(lambda state: replace(state, shortcuts=shortcut_state))

    def close(self) -> None:
        self.cancel()
        if self._owned_worker:
            worker = self._worker
            if isinstance(worker, Executor):
                worker.shutdown(wait=False, cancel_futures=False)

    def _start(self, activation: str) -> bool:
        with self._lock:
            if self._starting or self._session is not None:
                return False
            self._generation += 1
            token = self._generation
            self._starting = True
            self._activation = activation
            self._stop_requested = False
            self._cancel_requested = False
            self._terminal_seen = False
            mode = self._selected_mode
        self._publish_for(
            token,
            lambda state: replace(
                state,
                session_state="starting",
                transcript=None,
                warnings=(),
                error=None,
                last_event_sequence=0,
            ),
        )
        mode_id = None if mode is None else mode.id
        self._worker.submit(self._create_and_run, token, mode_id)
        return True

    def _request_stop(self, *, require_hold: bool) -> bool:
        with self._lock:
            if not self._starting and self._session is None:
                return False
            if require_hold and self._activation != "hold":
                return False
            if self._stop_requested or self._cancel_requested:
                return False
            self._stop_requested = True
            token = self._generation
            session = self._session
        self._publish_for(token, lambda state: replace(state, session_state="stopping"))
        if session is not None:
            self._worker.submit(self._stop_session, token, session)
        return True

    def _create_and_run(self, token: int, mode_id: str | None) -> None:
        request = RecognitionRequest(
            mode=mode_id,
            inject=self._inject,
            event_sink=lambda event: self._schedule_event(token, event),
        )
        try:
            session = self._session_factory(request)
        except Exception as exc:
            self._schedule_worker_failure(token, exc)
            return

        with self._lock:
            if token != self._generation or not self._starting:
                return
            self._session = session
            self._starting = False
            cancel_requested = self._cancel_requested
            stop_requested = self._stop_requested

        if cancel_requested or stop_requested:
            terminal = "cancelled" if cancel_requested else "completed"
            self._schedule_aborted_start(token, terminal)
            return

        try:
            session.run()
        except Exception as exc:
            self._schedule_worker_failure(token, exc)
            return
        with self._lock:
            missing_terminal = token == self._generation and not self._terminal_seen
        if missing_terminal:
            self._schedule_worker_failure(token, RuntimeError("识别会话结束时未发布完成事件"))

    def _stop_session(self, token: int, session: LiveSession) -> None:
        try:
            session.stop()
        except Exception as exc:
            self._schedule_worker_failure(token, exc)

    def _cancel_session(self, token: int, session: LiveSession) -> None:
        try:
            session.cancel()
        except Exception as exc:
            self._schedule_worker_failure(token, exc)

    def _schedule_event(self, token: int, event: RecognitionEvent) -> None:
        with self._lock:
            if token != self._generation:
                return
            if event.type in {"completed", "cancelled"}:
                self._terminal_seen = True
        self._schedule(lambda: self._reduce_event(token, event))

    def _reduce_event(self, token: int, event: RecognitionEvent) -> None:
        notify: tuple[str, str] | None = None
        with self._lock:
            if token != self._generation or event.sequence <= self._state.last_event_sequence:
                return
            state = self._state
            changes: dict[str, object] = {"last_event_sequence": event.sequence}
            if event.type == "ready":
                changes["session_state"] = "recording"
            elif event.type == "transcript":
                changes["transcript"] = event.transcript
            elif event.type == "warning":
                if event.message:
                    changes["warnings"] = (*state.warnings, event.message)
            elif event.type == "error":
                message = event.message or "识别失败。"
                changes.update(session_state="error", error=message)
                notify = ("识别失败", message)
            elif event.type == "cancelled":
                changes["session_state"] = "cancelled"
            elif event.type == "finalized":
                changes.update(session_state="completed", transcript=event.transcript)
                text = "" if event.transcript is None else event.transcript.authoritative_text
                notify = ("识别完成", text or "语音输入已完成。")
            elif event.type == "completed":
                if state.session_state not in {"error", "cancelled"}:
                    changes["session_state"] = "completed"
                if event.transcript is not None:
                    changes["transcript"] = event.transcript
            self._state = replace(state, **changes)
            terminal = event.type in {"completed", "cancelled"}
            if terminal:
                self._clear_session_locked()
            snapshot = self._state
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(snapshot)
        if notify is not None and self._notifier is not None:
            self._notifier(*notify)

    def _schedule_worker_failure(self, token: int, error: Exception) -> None:
        message = f"识别失败：{error}"

        def apply() -> None:
            notify = False
            with self._lock:
                if token != self._generation:
                    return
                self._state = replace(self._state, session_state="error", error=message)
                self._clear_session_locked()
                snapshot = self._state
                listeners = tuple(self._listeners)
                notify = self._notifier is not None
            for listener in listeners:
                listener(snapshot)
            if notify and self._notifier is not None:
                self._notifier("识别失败", message)

        self._schedule(apply)

    def _schedule_aborted_start(self, token: int, terminal: str) -> None:
        def apply() -> None:
            with self._lock:
                if token != self._generation:
                    return
                session_state = "cancelled" if terminal == "cancelled" else "completed"
                self._state = replace(self._state, session_state=session_state)
                self._clear_session_locked()
                snapshot = self._state
                listeners = tuple(self._listeners)
            for listener in listeners:
                listener(snapshot)

        self._schedule(apply)

    def _load_modes(self) -> None:
        try:
            modes = tuple(self._modes.list())
            with self._lock:
                current = self._selected_mode
            selected = self._modes.resolve(None if current is None else current.id)
            with self._lock:
                self._selected_mode = selected
        except Exception as exc:
            message = f"无法加载输入模式：{exc}"
            self._publish(lambda state, message=message: replace(state, error=message))
            return
        self._publish(lambda state: replace(state, modes=modes, selected_mode=selected))

    def _load_history(self, limit: object, cursor: object) -> None:
        try:
            assert self._history is not None
            page = self._history.query(
                limit=int(limit), cursor=None if cursor is None else str(cursor)
            )
        except Exception as exc:
            message = f"无法加载识别历史：{exc}"
            self._publish(lambda state, message=message: replace(state, error=message))
            return
        self._publish(lambda state: replace(state, history_page=page))

    def _load_model_checks(self) -> None:
        assert self._models is not None
        checks: list[ModelCheck] = []
        for model_id in self._model_ids:
            try:
                result = self._models.check(model_id)
                checks.append(ModelCheck.from_result(result, model_id=model_id))
            except Exception as exc:
                checks.append(
                    ModelCheck(
                        id=model_id,
                        installed=False,
                        ok=False,
                        errors=(f"无法检查模型：{exc}",),
                    )
                )
        self._publish(lambda state: replace(state, model_checks=tuple(checks)))

    def _publish(self, reducer: Callable[[AppState], AppState]) -> None:
        self._schedule(lambda: self._apply_state(reducer))

    def _publish_for(self, token: int, reducer: Callable[[AppState], AppState]) -> None:
        self._schedule(lambda: self._apply_state(reducer, token=token))

    def _apply_state(
        self,
        reducer: Callable[[AppState], AppState],
        *,
        token: int | None = None,
    ) -> None:
        with self._lock:
            if token is not None and token != self._generation:
                return
            self._state = reducer(self._state)
            snapshot = self._state
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(snapshot)

    def _clear_session_locked(self) -> None:
        self._session = None
        self._starting = False
        self._activation = None
        self._stop_requested = False
        self._cancel_requested = False


def _run_now(function: ScheduledCall) -> object:
    return function()
