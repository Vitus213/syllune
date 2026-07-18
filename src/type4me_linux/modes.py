from __future__ import annotations

import json
import os
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .paths import AppPaths


class ModesError(RuntimeError):
    """模式数据无法读取或更新。"""


@dataclass(frozen=True, slots=True)
class Mode:
    id: str
    name: str
    prompt: str
    processing_label: str
    builtin: bool
    sort_order: int


BUILTIN_MODES: tuple[Mode, ...] = (
    Mode(
        id="quick",
        name="快速输入",
        prompt="",
        processing_label="处理中",
        builtin=True,
        sort_order=0,
    ),
    Mode(
        id="voice-polish",
        name="语音润色",
        prompt=(
            "在不改变原意、不编造事实的前提下，删除口头语并修正语病。"
            "只输出处理后的文本，不要解释。原文：{text}"
        ),
        processing_label="润色中",
        builtin=True,
        sort_order=1,
    ),
    Mode(
        id="prompt-optimize",
        name="提示词优化",
        prompt=(
            "将以下需求改写为清晰、可执行的提示词。保留所有事实和约束；"
            "只输出提示词，不要解释。原文：{text}"
        ),
        processing_label="优化中",
        builtin=True,
        sort_order=2,
    ),
    Mode(
        id="translate-en",
        name="翻译为英文",
        prompt="将以下文本准确翻译为自然英文。只输出译文，不要解释。原文：{text}",
        processing_label="翻译中",
        builtin=True,
        sort_order=3,
    ),
)

_BUILTINS_BY_ID = {mode.id: mode for mode in BUILTIN_MODES}
_MODE_KEYS = frozenset(Mode.__dataclass_fields__)
_TEMPLATE_FIELDS = frozenset({"text", "selected", "clipboard"})


class ModesRepository:
    """持久化内置模式和用户模式，并提供稳定的名称或 ID 解析。"""

    def __init__(
        self,
        paths: AppPaths,
        *,
        uuid_factory: Callable[[], uuid.UUID | str] = uuid.uuid4,
    ) -> None:
        self._path = paths.config / "modes.json"
        self._uuid_factory = uuid_factory
        self._lock = threading.RLock()
        self._modes: tuple[Mode, ...] = ()
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._path.exists():
            self.reload()
        else:
            self._store(BUILTIN_MODES)

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> tuple[Mode, ...]:
        with self._lock:
            return self._modes

    def list_modes(self) -> tuple[Mode, ...]:
        return self.list()

    def get(self, mode_id: str) -> Mode:
        with self._lock:
            for mode in self._modes:
                if mode.id == mode_id:
                    return mode
        raise ModesError(f"找不到模式 ID：{mode_id}")

    def resolve(self, identifier: str | None = None) -> Mode:
        """按 ID 或规范化名称解析；未指定时返回快速输入模式。"""
        if identifier is None or not identifier.strip():
            return self.get("quick")
        key = _normalized_name(identifier)
        with self._lock:
            for mode in self._modes:
                if mode.id == identifier:
                    return mode
            for mode in self._modes:
                if _normalized_name(mode.name) == key:
                    return mode
        raise ModesError(f"找不到模式：{identifier}")

    def resolve_current(self, identifier: str | None) -> Mode:
        return self.resolve(identifier)

    def add(
        self,
        name: str,
        prompt: str,
        processing_label: str = "",
        *,
        sort_order: int | None = None,
    ) -> Mode:
        clean_name = _nonempty_string(name, "模式名称")
        clean_prompt = _string(prompt, "模式提示词")
        clean_label = _string(processing_label, "处理标签")
        with self._lock:
            self._ensure_unique_name(clean_name)
            order = self._next_sort_order() if sort_order is None else _sort_order(sort_order)
            mode_id = self._new_uuid()
            mode = Mode(mode_id, clean_name, clean_prompt, clean_label, False, order)
            self._store((*self._modes, mode))
            return mode

    def create(
        self,
        name: str,
        prompt: str,
        processing_label: str = "",
        *,
        sort_order: int | None = None,
    ) -> Mode:
        return self.add(name, prompt, processing_label, sort_order=sort_order)

    def update(
        self,
        mode_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        processing_label: str | None = None,
        sort_order: int | None = None,
    ) -> Mode:
        with self._lock:
            current = self.get(mode_id)
            if current.builtin:
                raise ModesError(f"内置模式不可修改：{mode_id}")
            updated = replace(
                current,
                name=current.name if name is None else _nonempty_string(name, "模式名称"),
                prompt=current.prompt if prompt is None else _string(prompt, "模式提示词"),
                processing_label=(
                    current.processing_label
                    if processing_label is None
                    else _string(processing_label, "处理标签")
                ),
                sort_order=(current.sort_order if sort_order is None else _sort_order(sort_order)),
            )
            self._ensure_unique_name(updated.name, excluding=mode_id)
            modes = tuple(updated if mode.id == mode_id else mode for mode in self._modes)
            self._store(modes)
            return updated

    def remove(self, mode_id: str) -> Mode:
        with self._lock:
            current = self.get(mode_id)
            if current.builtin:
                raise ModesError(f"内置模式不可删除：{mode_id}")
            self._store(tuple(mode for mode in self._modes if mode.id != mode_id))
            return current

    def delete(self, mode_id: str) -> Mode:
        return self.remove(mode_id)

    def reload(self) -> tuple[Mode, ...]:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                modes = _validate_modes(raw)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ModesError(f"无法读取模式文件“{self._path}”：{exc}") from exc
            self._modes = _sorted_modes(modes)
            return self._modes

    def _store(self, modes: tuple[Mode, ...]) -> None:
        ordered = _sorted_modes(modes)
        _validate_snapshot(ordered)
        content = (
            json.dumps([asdict(mode) for mode in ordered], ensure_ascii=False, indent=2) + "\n"
        )
        _atomic_write(self._path, content)
        self._modes = ordered

    def _new_uuid(self) -> str:
        for _ in range(100):
            value = str(self._uuid_factory())
            try:
                canonical = str(uuid.UUID(value))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ModesError("UUID 生成器返回了无效值") from exc
            if all(mode.id != canonical for mode in self._modes):
                return canonical
        raise ModesError("无法生成不重复的模式 UUID")

    def _next_sort_order(self) -> int:
        return max((mode.sort_order for mode in self._modes), default=-1) + 1

    def _ensure_unique_name(self, name: str, *, excluding: str | None = None) -> None:
        key = _normalized_name(name)
        if any(mode.id != excluding and _normalized_name(mode.name) == key for mode in self._modes):
            raise ModesError(f"模式名称已存在：{name}")


ModeRepository = ModesRepository


def render_template(
    template: str,
    *,
    text: str,
    selected: str = "",
    clipboard: str = "",
) -> str:
    """一次扫描替换受支持的占位符，不再解析插入内容。"""
    values = {"text": text, "selected": selected, "clipboard": clipboard}
    output: list[str] = []
    cursor = 0
    while cursor < len(template):
        opening = template.find("{", cursor)
        if opening < 0:
            output.append(template[cursor:])
            break
        output.append(template[cursor:opening])
        closing = template.find("}", opening + 1)
        if closing < 0:
            output.append(template[opening:])
            break
        field = template[opening + 1 : closing]
        if field in _TEMPLATE_FIELDS:
            output.append(values[field])
        else:
            output.append(template[opening : closing + 1])
        cursor = closing + 1
    return "".join(output)


def _validate_modes(raw: Any) -> tuple[Mode, ...]:
    if not isinstance(raw, list):
        raise TypeError("模式文件顶层必须是数组")
    modes: list[Mode] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"第 {index + 1} 个模式必须是对象")
        if set(item) != _MODE_KEYS:
            missing = sorted(_MODE_KEYS - set(item))
            extra = sorted(set(item) - _MODE_KEYS)
            raise ValueError(f"模式字段不匹配（缺少：{missing}；多余：{extra}）")
        mode = Mode(
            id=_nonempty_string(item["id"], "模式 ID"),
            name=_nonempty_string(item["name"], "模式名称"),
            prompt=_string(item["prompt"], "模式提示词"),
            processing_label=_string(item["processing_label"], "处理标签"),
            builtin=_boolean(item["builtin"], "内置标记"),
            sort_order=_sort_order(item["sort_order"]),
        )
        modes.append(mode)
    result = tuple(modes)
    _validate_snapshot(result)
    return result


def _validate_snapshot(modes: tuple[Mode, ...]) -> None:
    ids: set[str] = set()
    names: set[str] = set()
    for mode in modes:
        if mode.id in ids:
            raise ModesError(f"模式 ID 重复：{mode.id}")
        ids.add(mode.id)
        name_key = _normalized_name(mode.name)
        if name_key in names:
            raise ModesError(f"模式名称重复：{mode.name}")
        names.add(name_key)
        builtin = _BUILTINS_BY_ID.get(mode.id)
        if mode.builtin:
            if builtin is None or mode != builtin:
                raise ModesError(f"内置模式定义不合法：{mode.id}")
        elif builtin is not None:
            raise ModesError(f"内置模式 ID 不可用于用户模式：{mode.id}")
        else:
            try:
                if str(uuid.UUID(mode.id)) != mode.id:
                    raise ValueError(mode.id)
            except ValueError as exc:
                raise ModesError(f"用户模式 ID 必须是 UUID：{mode.id}") from exc
    missing = set(_BUILTINS_BY_ID) - ids
    if missing:
        raise ModesError(f"缺少内置模式：{'、'.join(sorted(missing))}")


def _sorted_modes(modes: tuple[Mode, ...]) -> tuple[Mode, ...]:
    return tuple(sorted(modes, key=lambda mode: (mode.sort_order, mode.name, mode.id)))


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}必须是字符串")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    result = _string(value, label).strip()
    if not result:
        raise ValueError(f"{label}不能为空")
    return result


def _sort_order(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("排序值必须是整数")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label}必须是布尔值")
    return value


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise ModesError(f"无法原子写入模式文件“{path}”：{exc}") from exc

    # 替换已提交后不能向调用方报告失败，否则内存快照会与磁盘内容分叉。
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
