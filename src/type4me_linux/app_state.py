from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .events import RecognitionTranscript
from .history import HistoryPage
from .modes import Mode

SessionProjection = Literal[
    "idle",
    "starting",
    "recording",
    "stopping",
    "completed",
    "error",
    "cancelled",
]
ShortcutProjection = Literal["unbound", "binding", "bound", "unavailable"]


@dataclass(frozen=True, slots=True)
class ModelCheck:
    """模型完整性检查的不可变 UI 投影。"""

    id: str
    installed: bool
    ok: bool
    version: str | None = None
    path: str | None = None
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    corrupt: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, result: object, *, model_id: str) -> ModelCheck:
        if not isinstance(result, dict):
            return cls(id=model_id, installed=False, ok=False, errors=("模型检查返回了无效结果。",))

        def strings(name: str) -> tuple[str, ...]:
            value = result.get(name, ())
            if isinstance(value, (list, tuple)):
                return tuple(str(item) for item in value)
            return (str(value),) if value else ()

        value_id = result.get("id", model_id)
        version = result.get("version")
        path = result.get("path")
        return cls(
            id=str(value_id),
            installed=bool(result.get("installed", False)),
            ok=bool(result.get("ok", False)),
            version=None if version is None else str(version),
            path=None if path is None else str(path),
            missing=strings("missing"),
            extra=strings("extra"),
            corrupt=strings("corrupt"),
            errors=strings("errors"),
        )


@dataclass(frozen=True, slots=True)
class ShortcutState:
    status: ShortcutProjection = "unbound"
    bound_ids: frozenset[str] = frozenset()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class AppState:
    """桌面界面消费的完整不可变状态快照。"""

    session_state: SessionProjection = "idle"
    transcript: RecognitionTranscript | None = None
    selected_mode: Mode | None = None
    modes: tuple[Mode, ...] = ()
    model_checks: tuple[ModelCheck, ...] = ()
    shortcuts: ShortcutState = ShortcutState()
    history_page: HistoryPage | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    last_event_sequence: int = 0

    @property
    def is_busy(self) -> bool:
        return self.session_state in {"starting", "recording", "stopping"}

    @property
    def final_text(self) -> str:
        transcript = self.transcript
        if transcript is None or not transcript.is_final:
            return ""
        return transcript.authoritative_text
