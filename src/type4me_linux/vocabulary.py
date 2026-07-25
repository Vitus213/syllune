from __future__ import annotations

import json
import os
import re
import sys
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .paths import AppPaths


class VocabularyError(RuntimeError):
    """词汇数据无法读取或更新。"""


class VocabularyService:
    """合并只读默认词汇与用户词汇，并维护派生热词缓存。"""

    def __init__(
        self,
        paths: AppPaths,
        defaults_directory: Path | None = None,
    ) -> None:
        self._paths = paths
        self._defaults_directory = (
            Path(defaults_directory) if defaults_directory is not None else _discover_defaults()
        )
        self._user_directory = paths.data / "vocabulary"
        self._hotwords_path = self._user_directory / "hotwords.json"
        self._snippets_path = self._user_directory / "snippets.json"
        self._cache_path = paths.cache / "hotwords.txt"
        self._lock = threading.RLock()
        self._user_hotwords: tuple[str, ...] = ()
        self._user_snippets: dict[str, str] = {}
        self._hotwords: tuple[str, ...] = ()
        self._snippets: dict[str, str] = {}

        self._user_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.reload()

    @property
    def hotwords_cache_path(self) -> Path:
        return self._cache_path

    def list_hotwords(self) -> tuple[str, ...]:
        with self._lock:
            return self._hotwords

    def list_snippets(self) -> dict[str, str]:
        with self._lock:
            return dict(self._snippets)

    def add_hotword(self, word: str) -> tuple[str, ...]:
        value = _clean_text(word, "热词")
        key = _normalized_key(value)
        with self._lock:
            if any(_normalized_key(item) == key for item in self._user_hotwords):
                raise VocabularyError(f"用户热词已存在：{value}")
            self._store_hotwords((*self._user_hotwords, value))
            return self._hotwords

    def correct(self, transcript: str, replacement: str) -> None:
        """Teach the local recognizer and replace a recurring misrecognition."""
        spoken = _clean_text(transcript, "误识别文本")
        corrected = _clean_text(replacement, "修正文本")
        spoken_key = _normalized_key(spoken)
        corrected_key = _normalized_key(corrected)
        if spoken_key == corrected_key:
            raise VocabularyError("误识别文本与修正文本不能相同")

        with self._lock:
            hotwords = list(self._user_hotwords)
            if _find_key(self._hotwords, corrected_key) is None:
                hotwords.append(corrected)

            snippets = dict(self._user_snippets)
            existing_trigger = _find_mapping_key(snippets, spoken_key)
            if existing_trigger is None:
                snippets[spoken] = corrected
            else:
                snippets[existing_trigger] = corrected

            hotwords_changed = tuple(hotwords) != self._user_hotwords
            snippets_changed = snippets != self._user_snippets
            if hotwords_changed:
                self._store_hotwords(hotwords)
            if snippets_changed:
                self._store_snippets(snippets)

    def update_hotword(self, word: str, replacement: str) -> tuple[str, ...]:
        old_key = _normalized_key(_clean_text(word, "热词"))
        value = _clean_text(replacement, "热词")
        new_key = _normalized_key(value)
        with self._lock:
            index = _find_key(self._user_hotwords, old_key)
            if index is None:
                raise VocabularyError(f"找不到用户热词：{word}")
            if any(
                position != index and _normalized_key(item) == new_key
                for position, item in enumerate(self._user_hotwords)
            ):
                raise VocabularyError(f"用户热词已存在：{value}")
            updated = list(self._user_hotwords)
            updated[index] = value
            self._store_hotwords(updated)
            return self._hotwords

    def remove_hotword(self, word: str) -> tuple[str, ...]:
        key = _normalized_key(_clean_text(word, "热词"))
        with self._lock:
            index = _find_key(self._user_hotwords, key)
            if index is None:
                raise VocabularyError(f"找不到用户热词：{word}")
            updated = list(self._user_hotwords)
            del updated[index]
            self._store_hotwords(updated)
            return self._hotwords

    def add_snippet(self, trigger: str, replacement: str) -> dict[str, str]:
        spoken = _clean_text(trigger, "片段触发词")
        value = _require_string(replacement, "片段替换文本")
        key = _normalized_key(spoken)
        with self._lock:
            if _find_mapping_key(self._user_snippets, key) is not None:
                raise VocabularyError(f"用户片段触发词已存在：{spoken}")
            updated = dict(self._user_snippets)
            updated[spoken] = value
            self._store_snippets(updated)
            return dict(self._snippets)

    def update_snippet(
        self,
        trigger: str,
        replacement: str,
        *,
        new_trigger: str | None = None,
    ) -> dict[str, str]:
        old_key = _normalized_key(_clean_text(trigger, "片段触发词"))
        value = _require_string(replacement, "片段替换文本")
        with self._lock:
            stored_trigger = _find_mapping_key(self._user_snippets, old_key)
            if stored_trigger is None:
                raise VocabularyError(f"找不到用户片段触发词：{trigger}")
            spoken = (
                stored_trigger if new_trigger is None else _clean_text(new_trigger, "片段触发词")
            )
            new_key = _normalized_key(spoken)
            conflict = _find_mapping_key(self._user_snippets, new_key)
            if conflict is not None and conflict != stored_trigger:
                raise VocabularyError(f"用户片段触发词已存在：{spoken}")
            updated = dict(self._user_snippets)
            del updated[stored_trigger]
            updated[spoken] = value
            self._store_snippets(updated)
            return dict(self._snippets)

    def remove_snippet(self, trigger: str) -> dict[str, str]:
        key = _normalized_key(_clean_text(trigger, "片段触发词"))
        with self._lock:
            stored_trigger = _find_mapping_key(self._user_snippets, key)
            if stored_trigger is None:
                raise VocabularyError(f"找不到用户片段触发词：{trigger}")
            updated = dict(self._user_snippets)
            del updated[stored_trigger]
            self._store_snippets(updated)
            return dict(self._snippets)

    def reload(self) -> None:
        with self._lock:
            default_hotwords = _load_hotwords(
                self._defaults_directory / "hotwords.json", required=True
            )
            default_snippets = _load_snippets(
                self._defaults_directory / "snippets.json", required=True
            )
            user_hotwords = _load_hotwords(self._hotwords_path, required=False)
            user_snippets = _load_snippets(self._snippets_path, required=False)

            effective_hotwords = _merge_hotwords(default_hotwords, user_hotwords)
            effective_snippets = _merge_snippets(default_snippets, user_snippets)
            self._regenerate_cache(effective_hotwords)
            self._user_hotwords = tuple(user_hotwords)
            self._user_snippets = user_snippets
            self._hotwords = effective_hotwords
            self._snippets = effective_snippets

    def apply_snippets(self, text: str) -> str:
        with self._lock:
            snippets = tuple(self._snippets.items())
        output = text
        for trigger, replacement in snippets:
            output = _snippet_pattern(trigger).sub(lambda _match: replacement, output)
        return output

    def _store_hotwords(self, hotwords: Sequence[str]) -> None:
        values = _deduplicate_hotwords(hotwords)
        _atomic_json_write(self._hotwords_path, list(values))
        self.reload()

    def _store_snippets(self, snippets: Mapping[str, str]) -> None:
        values = _validated_snippets(snippets)
        _atomic_json_write(self._snippets_path, values)
        self.reload()

    def _regenerate_cache(self, hotwords: Sequence[str]) -> None:
        content = "".join(f"{word}\n" for word in hotwords)
        _atomic_text_write(self._cache_path, content)


def _discover_defaults() -> Path:
    candidates: list[Path] = []
    executable = Path(sys.argv[0]).expanduser()
    if executable.is_absolute():
        candidates.append(executable.resolve().parent.parent / "share/type4me-linux/vocabulary")
    candidates.append(Path(sys.prefix) / "share/type4me-linux/vocabulary")

    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        candidates.append(parent / "share/type4me-linux/vocabulary")
        candidates.append(parent / "data/vocabulary")

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "hotwords.json").is_file() and (candidate / "snippets.json").is_file():
            return candidate
    raise VocabularyError("找不到随应用安装的默认词汇目录")


def _load_hotwords(path: Path, *, required: bool) -> list[str]:
    raw = _load_json(path, required=required, empty=[])
    if not isinstance(raw, list):
        raise VocabularyError(f"热词文件必须包含 JSON 数组：{path}")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise VocabularyError(f"热词必须是字符串：{path}")
        values.append(_clean_text(item, "热词"))
    return list(_deduplicate_hotwords(values))


def _load_snippets(path: Path, *, required: bool) -> dict[str, str]:
    raw = _load_json(path, required=required, empty={})
    if not isinstance(raw, dict):
        raise VocabularyError(f"片段文件必须包含 JSON 对象：{path}")
    return _validated_snippets(raw)


def _load_json(path: Path, *, required: bool, empty: Any) -> Any:
    if not path.exists():
        if required:
            raise VocabularyError(f"找不到词汇文件：{path}")
        return empty
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VocabularyError(f"无法读取词汇文件“{path}”：{exc}") from exc


def _merge_hotwords(defaults: Sequence[str], users: Sequence[str]) -> tuple[str, ...]:
    return _deduplicate_hotwords((*defaults, *users))


def _merge_snippets(defaults: Mapping[str, str], users: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    for source in (defaults, users):
        for trigger, replacement in source.items():
            key = _normalized_key(trigger)
            if key not in merged:
                order.append(key)
            merged[key] = (trigger, replacement)
    return {merged[key][0]: merged[key][1] for key in order}


def _deduplicate_hotwords(words: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for word in words:
        key = _normalized_key(word)
        if key in seen:
            continue
        seen.add(key)
        result.append(word)
    return tuple(result)


def _validated_snippets(raw: Mapping[Any, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    normalized: set[str] = set()
    for trigger, replacement in raw.items():
        if not isinstance(trigger, str):
            raise VocabularyError("片段触发词必须是字符串")
        spoken = _clean_text(trigger, "片段触发词")
        value = _require_string(replacement, "片段替换文本")
        key = _normalized_key(spoken)
        if key in normalized:
            raise VocabularyError(f"片段触发词规范化后重复：{spoken}")
        normalized.add(key)
        result[spoken] = value
    return result


def _clean_text(value: str, label: str) -> str:
    text = _require_string(value, label).strip()
    if not text:
        raise VocabularyError(f"{label}不能为空")
    return text


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise VocabularyError(f"{label}必须是字符串")
    return value


def _normalized_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def _find_key(values: Sequence[str], key: str) -> int | None:
    for index, value in enumerate(values):
        if _normalized_key(value) == key:
            return index
    return None


def _find_mapping_key(values: Mapping[str, str], key: str) -> str | None:
    for value in values:
        if _normalized_key(value) == key:
            return value
    return None


def _snippet_pattern(trigger: str) -> re.Pattern[str]:
    normalized = unicodedata.normalize("NFKC", trigger)
    characters = [character for character in normalized if not character.isspace()]
    body = r"\s*".join(re.escape(character) for character in characters)
    return re.compile(
        rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )


def _atomic_json_write(path: Path, value: Any) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    _atomic_text_write(path, content)


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise VocabularyError(f"无法原子写入词汇文件“{path}”：{exc}") from exc
