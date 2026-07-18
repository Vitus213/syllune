from __future__ import annotations

from collections.abc import Callable

from type4me_linux.controller import ApplicationController
from type4me_linux.history import HistoryPage
from type4me_linux.modes import BUILTIN_MODES, Mode


class _DeferredScheduler:
    def __init__(self) -> None:
        self.pending: list[Callable[[], object]] = []

    def __call__(self, callback: Callable[[], object]) -> None:
        self.pending.append(callback)

    def run_next(self) -> None:
        self.pending.pop(0)()


class _ImmediateWorker:
    def submit(self, callback, /, *args):  # type: ignore[no-untyped-def]
        return callback(*args)


class _Modes:
    def __init__(self) -> None:
        self.fail = False

    def list(self) -> tuple[Mode, ...]:
        if self.fail:
            raise RuntimeError("模式仓库不可读")
        return BUILTIN_MODES

    def resolve(self, identifier: str | None = None) -> Mode:
        if self.fail:
            raise RuntimeError("模式仓库不可读")
        return BUILTIN_MODES[0]


class _History:
    def query(self, *, limit: int = 50, cursor: str | None = None) -> HistoryPage:
        raise RuntimeError("历史数据库繁忙")


def test_deferred_scheduler_keeps_repository_error_messages() -> None:
    scheduler = _DeferredScheduler()
    modes = _Modes()
    controller = ApplicationController(
        session_factory=lambda request: None,  # type: ignore[arg-type,return-value]
        modes=modes,
        history=_History(),
        scheduler=scheduler,
        worker=_ImmediateWorker(),
        model_ids=(),
    )

    modes.fail = True
    assert controller.select_mode("quick") is None
    scheduler.run_next()
    assert controller.state.error == "无法选择输入模式：模式仓库不可读"

    controller.refresh_modes()
    scheduler.run_next()
    assert controller.state.error == "无法加载输入模式：模式仓库不可读"

    controller.refresh_history()
    scheduler.run_next()
    assert controller.state.error == "无法加载识别历史：历史数据库繁忙"
