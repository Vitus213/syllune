from __future__ import annotations

import base64
import contextlib
import csv
import fcntl
import io
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from .paths import AppPaths


CSV_HEADER = (
    "id",
    "created_at",
    "duration_seconds",
    "raw_text",
    "processing_mode",
    "processed_text",
    "final_text",
    "status",
    "character_count",
    "asr_provider",
    "asr_model",
)
_SCHEMA_VERSION = 1
_DEFAULT_BUSY_TIMEOUT_MS = 5_000


class HistoryError(RuntimeError):
    """历史数据库无法安全读取或更新。"""


@dataclass(frozen=True)
class CompletedHistoryRecord:
    raw_text: str
    final_text: str
    duration_seconds: float | None = None
    processing_mode: str | None = None
    processed_text: str | None = None
    status: str = "completed"
    character_count: int | None = None
    asr_provider: str | None = None
    asr_model: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str | datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class HistoryRecord:
    id: str
    created_at: str
    duration_seconds: float | None
    raw_text: str
    processing_mode: str | None
    processed_text: str | None
    final_text: str
    status: str
    character_count: int | None
    asr_provider: str | None
    asr_model: str | None


@dataclass(frozen=True)
class HistoryPage:
    records: tuple[HistoryRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class HistoryTotals:
    records: int
    characters: int
    duration_seconds: float


@dataclass(frozen=True)
class UsageSummary:
    days: int
    since: str
    providers: Mapping[str, int]
    models: Mapping[str, int]


class HistoryStore:
    """提供受迁移保护的本地识别历史仓库。"""

    _thread_locks_guard = threading.Lock()
    _thread_locks: dict[Path, threading.Lock] = {}

    def __init__(self, paths: AppPaths, *, busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS) -> None:
        if isinstance(busy_timeout_ms, bool) or busy_timeout_ms <= 0:
            raise ValueError("SQLite 忙等待超时值必须是正整数")
        self.path = paths.data / "history.sqlite3"
        self._lock_path = paths.state / "history" / "migration.lock"
        self._busy_timeout_ms = int(busy_timeout_ms)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._migrate()

    def insert(self, record: CompletedHistoryRecord) -> HistoryRecord:
        stored = _stored_record(record)
        values = tuple(getattr(stored, name) for name in CSV_HEADER)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO recognition_history (
                        id, created_at, duration_seconds, raw_text, processing_mode,
                        processed_text, final_text, status, character_count,
                        asr_provider, asr_model
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
        except sqlite3.Error as exc:
            raise HistoryError(f"无法写入识别历史：{exc}") from exc
        return stored

    def get(self, record_id: str) -> HistoryRecord | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT {', '.join(CSV_HEADER)} FROM recognition_history WHERE id = ?",
                    (record_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"无法读取识别历史：{exc}") from exc
        return None if row is None else _row_to_record(row)

    def query(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        from_date: str | datetime | None = None,
        to_date: str | datetime | None = None,
    ) -> HistoryPage:
        if isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("历史分页大小必须在 1 到 1000 之间")

        clauses: list[str] = []
        parameters: list[object] = []
        if cursor is not None:
            cursor_created_at, cursor_id = _decode_cursor(cursor)
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            parameters.extend((cursor_created_at, cursor_created_at, cursor_id))
        if from_date is not None:
            clauses.append("created_at >= ?")
            parameters.append(_timestamp_text(from_date, "起始日期"))
        if to_date is not None:
            clauses.append("created_at < ?")
            parameters.append(_timestamp_text(to_date, "结束日期"))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        statement = (
            f"SELECT {', '.join(CSV_HEADER)} FROM recognition_history{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        parameters.append(limit + 1)
        try:
            with self._connect() as connection:
                rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"无法查询识别历史：{exc}") from exc

        has_more = len(rows) > limit
        records = tuple(_row_to_record(row) for row in rows[:limit])
        next_cursor = None
        if has_more and records:
            last = records[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)
        return HistoryPage(records=records, next_cursor=next_cursor)

    def delete(self, record_id: str) -> bool:
        try:
            with self._connect() as connection:
                result = connection.execute(
                    "DELETE FROM recognition_history WHERE id = ?", (record_id,)
                )
                return result.rowcount == 1
        except sqlite3.Error as exc:
            raise HistoryError(f"无法删除识别历史：{exc}") from exc

    def delete_many(self, record_ids: Iterable[str]) -> int:
        ids = tuple(dict.fromkeys(record_ids))
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        try:
            with self._connect() as connection:
                result = connection.execute(
                    f"DELETE FROM recognition_history WHERE id IN ({placeholders})", ids
                )
                return result.rowcount
        except sqlite3.Error as exc:
            raise HistoryError(f"无法批量删除识别历史：{exc}") from exc

    def delete_all(self) -> int:
        try:
            with self._connect() as connection:
                result = connection.execute("DELETE FROM recognition_history")
                return result.rowcount
        except sqlite3.Error as exc:
            raise HistoryError(f"无法清空识别历史：{exc}") from exc

    def totals(self) -> HistoryTotals:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(character_count), 0),
                           COALESCE(SUM(duration_seconds), 0.0)
                    FROM recognition_history
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"无法统计识别历史：{exc}") from exc
        assert row is not None
        return HistoryTotals(
            records=int(row[0]), characters=int(row[1]), duration_seconds=float(row[2])
        )

    def usage_summary(self, days: int, *, now: datetime | None = None) -> UsageSummary:
        if days not in (1, 7, 30):
            raise ValueError("用量统计窗口只能是 1、7 或 30 天")
        current = _aware_utc(now or datetime.now(UTC), "当前时间")
        since = _timestamp_text(current - timedelta(days=days), "统计起始时间")
        try:
            with self._connect() as connection:
                providers = _usage_counts(connection, "asr_provider", since)
                models = _usage_counts(connection, "asr_model", since)
        except sqlite3.Error as exc:
            raise HistoryError(f"无法统计识别用量：{exc}") from exc
        return UsageSummary(days=days, since=since, providers=providers, models=models)

    def usage_summaries(self, *, now: datetime | None = None) -> tuple[UsageSummary, ...]:
        return tuple(self.usage_summary(days, now=now) for days in (1, 7, 30))

    def export_csv(
        self,
        destination: Path | TextIO,
        *,
        from_date: str | datetime | None = None,
        to_date: str | datetime | None = None,
    ) -> int:
        records = self._records_for_export(from_date=from_date, to_date=to_date)
        if hasattr(destination, "write"):
            _write_csv(destination, records)  # type: ignore[arg-type]
        else:
            path = Path(destination)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                with path.open("w", encoding="utf-8", newline="") as handle:
                    _write_csv(handle, records)
            except (OSError, UnicodeError) as exc:
                raise HistoryError(f"无法导出识别历史：{exc}") from exc
        return len(records)

    def export_csv_text(
        self,
        *,
        from_date: str | datetime | None = None,
        to_date: str | datetime | None = None,
    ) -> str:
        output = io.StringIO(newline="")
        self.export_csv(output, from_date=from_date, to_date=to_date)
        return output.getvalue()

    def _records_for_export(
        self,
        *,
        from_date: str | datetime | None,
        to_date: str | datetime | None,
    ) -> tuple[HistoryRecord, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if from_date is not None:
            clauses.append("created_at >= ?")
            parameters.append(_timestamp_text(from_date, "起始日期"))
        if to_date is not None:
            clauses.append("created_at < ?")
            parameters.append(_timestamp_text(to_date, "结束日期"))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT {', '.join(CSV_HEADER)} FROM recognition_history{where} "
                    "ORDER BY created_at DESC, id DESC",
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"无法读取导出历史：{exc}") from exc
        return tuple(_row_to_record(row) for row in rows)

    def _migrate(self) -> None:
        with self._migration_lock():
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version > _SCHEMA_VERSION:
                        raise HistoryError(f"历史数据库版本过新：{version}")
                    if version < 1:
                        connection.execute(
                            """
                            CREATE TABLE IF NOT EXISTS recognition_history (
                                id TEXT PRIMARY KEY,
                                created_at TEXT NOT NULL,
                                duration_seconds REAL,
                                raw_text TEXT NOT NULL,
                                processing_mode TEXT,
                                processed_text TEXT,
                                final_text TEXT NOT NULL,
                                status TEXT NOT NULL,
                                character_count INTEGER,
                                asr_provider TEXT,
                                asr_model TEXT
                            )
                            """
                        )
                        connection.execute("PRAGMA user_version = 1")
                    _verify_schema(connection)
                    connection.commit()
                os.chmod(self.path, 0o600)
            except HistoryError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise HistoryError(f"无法迁移历史数据库：{exc}") from exc

    @contextlib.contextmanager
    def _migration_lock(self) -> Iterator[None]:
        resolved = self._lock_path.resolve()
        with self._thread_locks_guard:
            thread_lock = self._thread_locks.setdefault(resolved, threading.Lock())
        with thread_lock:
            try:
                descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                raise HistoryError(f"无法打开历史迁移锁：{exc}") from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            except OSError as exc:
                raise HistoryError(f"无法锁定历史数据库迁移：{exc}") from exc
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()


def _stored_record(record: CompletedHistoryRecord) -> HistoryRecord:
    if not isinstance(record, CompletedHistoryRecord):
        raise TypeError("record 必须是 CompletedHistoryRecord")
    record_id = _required_text(record.id, "历史 ID")
    raw_text = _required_string(record.raw_text, "原始文本")
    final_text = _required_string(record.final_text, "最终文本")
    status = _required_text(record.status, "状态")
    duration = record.duration_seconds
    if duration is not None and (isinstance(duration, bool) or duration < 0):
        raise ValueError("识别时长不能为负数")
    characters = len(final_text) if record.character_count is None else record.character_count
    if isinstance(characters, bool) or characters < 0:
        raise ValueError("字符数不能为负数")
    return HistoryRecord(
        id=record_id,
        created_at=_timestamp_text(record.created_at, "创建时间"),
        duration_seconds=None if duration is None else float(duration),
        raw_text=raw_text,
        processing_mode=_optional_string(record.processing_mode, "处理模式"),
        processed_text=_optional_string(record.processed_text, "处理后文本"),
        final_text=final_text,
        status=status,
        character_count=int(characters),
        asr_provider=_optional_string(record.asr_provider, "ASR 提供方"),
        asr_model=_optional_string(record.asr_model, "ASR 模型"),
    )


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}必须是字符串")
    return value


def _required_text(value: object, label: str) -> str:
    text = _required_string(value, label)
    if not text:
        raise ValueError(f"{label}不能为空")
    return text


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _timestamp_text(value: str | datetime, label: str) -> str:
    if isinstance(value, datetime):
        parsed = _aware_utc(value, label)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label}不是有效的 RFC 3339 时间") from exc
        parsed = _aware_utc(parsed, label)
    else:
        raise TypeError(f"{label}必须是 datetime 或 RFC 3339 字符串")
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}必须包含时区")
    return value.astimezone(UTC)


def _row_to_record(row: sqlite3.Row) -> HistoryRecord:
    return HistoryRecord(**{name: row[name] for name in CSV_HEADER})


def _encode_cursor(created_at: str, record_id: str) -> str:
    payload = json.dumps([created_at, record_id], ensure_ascii=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not cursor:
        raise HistoryError("历史分页游标无效")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError
        created_at = _timestamp_text(payload[0], "游标时间")
        record_id = _required_text(payload[1], "游标 ID")
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoryError("历史分页游标无效") from exc
    return created_at, record_id


def _usage_counts(connection: sqlite3.Connection, column: str, since: str) -> dict[str, int]:
    if column not in {"asr_provider", "asr_model"}:
        raise AssertionError("不支持的用量统计字段")
    rows = connection.execute(
        f"""
        SELECT {column}, COUNT(*) AS uses
        FROM recognition_history
        WHERE created_at >= ? AND {column} IS NOT NULL AND {column} != ''
        GROUP BY {column}
        ORDER BY uses DESC, {column} ASC
        """,
        (since,),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _write_csv(handle: TextIO, records: tuple[HistoryRecord, ...]) -> None:
    writer = csv.writer(handle, dialect="excel", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for record in records:
        writer.writerow(tuple(getattr(record, name) for name in CSV_HEADER))


def _verify_schema(connection: sqlite3.Connection) -> None:
    expected = (
        ("id", "TEXT", 0, 1),
        ("created_at", "TEXT", 1, 0),
        ("duration_seconds", "REAL", 0, 0),
        ("raw_text", "TEXT", 1, 0),
        ("processing_mode", "TEXT", 0, 0),
        ("processed_text", "TEXT", 0, 0),
        ("final_text", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("character_count", "INTEGER", 0, 0),
        ("asr_provider", "TEXT", 0, 0),
        ("asr_model", "TEXT", 0, 0),
    )
    rows = connection.execute("PRAGMA table_info(recognition_history)").fetchall()
    actual = tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)
    if actual != expected:
        raise HistoryError("识别历史表结构与当前版本不兼容")
