from __future__ import annotations

import base64
import csv
import json
import sqlite3
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import type4me_linux.history as history_module
import pytest

from type4me_linux.history import CSV_HEADER, CompletedHistoryRecord, HistoryError, HistoryStore
from type4me_linux.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=None,
    )


def _record(
    record_id: str,
    created_at: datetime,
    *,
    raw_text: str | None = None,
    final_text: str | None = None,
    provider: str | None = "sensevoice-vad",
    model: str | None = "sensevoice-int8",
    duration: float | None = 1.0,
    characters: int | None = None,
) -> CompletedHistoryRecord:
    return CompletedHistoryRecord(
        id=record_id,
        created_at=created_at,
        duration_seconds=duration,
        raw_text=raw_text if raw_text is not None else f"原文 {record_id}",
        processing_mode="quick",
        processed_text=None,
        final_text=final_text if final_text is not None else f"结果 {record_id}",
        status="completed",
        character_count=characters,
        asr_provider=provider,
        asr_model=model,
    )


def test_migration_is_idempotent_concurrent_and_schema_is_exact(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _index: HistoryStore(paths), range(16)))

    HistoryStore(paths)
    database = paths.data / "history.sqlite3"
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        columns = connection.execute("PRAGMA table_info(recognition_history)").fetchall()
        tables = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        ).fetchall()

    assert len(stores) == 16
    assert version == 1
    assert journal_mode == "wal"
    assert [row[1] for row in columns] == list(CSV_HEADER)
    assert [(row[2], row[3], row[5]) for row in columns] == [
        ("TEXT", 0, 1),
        ("TEXT", 1, 0),
        ("REAL", 0, 0),
        ("TEXT", 1, 0),
        ("TEXT", 0, 0),
        ("TEXT", 0, 0),
        ("TEXT", 1, 0),
        ("TEXT", 1, 0),
        ("INTEGER", 0, 0),
        ("TEXT", 0, 0),
        ("TEXT", 0, 0),
    ]
    assert tables == [("recognition_history",)]
    assert database.stat().st_mode & 0o777 == 0o600


def test_connection_uses_configured_busy_timeout_and_wal(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path), busy_timeout_ms=12_345)

    with store._connect() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 12_345
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_completed_record_full_field_roundtrip_and_computed_character_count(
    tmp_path: Path,
) -> None:
    store = HistoryStore(_paths(tmp_path))
    created_at = datetime(2026, 7, 13, 8, 9, 10, 123456, tzinfo=UTC)
    inserted = store.insert(
        CompletedHistoryRecord(
            id="full-row",
            created_at=created_at,
            duration_seconds=2.75,
            raw_text="这是，原文。",
            processing_mode="voice-polish",
            processed_text="这是原文。",
            final_text="最终文本",
            status="processing-fallback",
            character_count=99,
            asr_provider="hybrid-fallback",
            asr_model="qwen3-asr-0.6b-int8",
        )
    )

    assert inserted.id == "full-row"
    assert inserted.created_at == "2026-07-13T08:09:10.123456Z"
    assert inserted.duration_seconds == 2.75
    assert inserted.raw_text == "这是，原文。"
    assert inserted.processing_mode == "voice-polish"
    assert inserted.processed_text == "这是原文。"
    assert inserted.final_text == "最终文本"
    assert inserted.status == "processing-fallback"
    assert inserted.character_count == 99
    assert inserted.asr_provider == "hybrid-fallback"
    assert inserted.asr_model == "qwen3-asr-0.6b-int8"
    assert store.get("full-row") == inserted

    computed = store.insert(
        CompletedHistoryRecord(
            id="computed",
            created_at=created_at + timedelta(seconds=1),
            raw_text="raw",
            final_text="中A文",
        )
    )
    assert computed.character_count == 3


def test_query_is_newest_first_with_stable_cursor_and_date_boundaries(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    base = datetime(2026, 7, 1, tzinfo=UTC)
    for record in (
        _record("a", base),
        _record("b", base + timedelta(days=1)),
        _record("c", base + timedelta(days=1)),
        _record("d", base + timedelta(days=2)),
        _record("e", base + timedelta(days=3)),
    ):
        store.insert(record)

    first = store.query(limit=2)
    second = store.query(limit=2, cursor=first.next_cursor)
    third = store.query(limit=2, cursor=second.next_cursor)

    assert [item.id for item in first.records] == ["e", "d"]
    assert first.next_cursor is not None
    assert [item.id for item in second.records] == ["c", "b"]
    assert second.next_cursor is not None
    assert [item.id for item in third.records] == ["a"]
    assert third.next_cursor is None
    assert len({item.id for page in (first, second, third) for item in page.records}) == 5

    bounded = store.query(
        limit=20,
        from_date=base + timedelta(days=1),
        to_date=base + timedelta(days=3),
    )
    assert [item.id for item in bounded.records] == ["d", "c", "b"]


def test_delete_one_many_and_all_are_idempotent(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    now = datetime(2026, 7, 13, tzinfo=UTC)
    for record_id in ("one", "two", "three", "four"):
        store.insert(_record(record_id, now))

    assert store.delete("one") is True
    assert store.delete("one") is False
    assert store.delete_many(["two", "two", "missing", "three"]) == 2
    assert store.delete_many([]) == 0
    assert [item.id for item in store.query().records] == ["four"]
    assert store.delete_all() == 1
    assert store.delete_all() == 0
    assert store.query().records == ()


def test_totals_and_provider_model_usage_for_exact_windows(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)
    rows = (
        _record(
            "recent",
            now - timedelta(hours=12),
            provider="sensevoice",
            model="sv",
            duration=1.5,
            characters=5,
        ),
        _record(
            "one-boundary",
            now - timedelta(days=1),
            provider="sensevoice",
            model="sv",
            duration=2.0,
            characters=7,
        ),
        _record(
            "week",
            now - timedelta(days=6),
            provider="qwen",
            model="q3",
            duration=None,
            characters=11,
        ),
        _record(
            "week-boundary",
            now - timedelta(days=7),
            provider="qwen",
            model="q3",
            duration=3.25,
            characters=13,
        ),
        _record(
            "month",
            now - timedelta(days=20),
            provider=None,
            model=None,
            duration=4.0,
            characters=17,
        ),
        _record(
            "too-old",
            now - timedelta(days=31),
            provider="old",
            model="old",
            duration=8.0,
            characters=19,
        ),
    )
    for row in rows:
        store.insert(row)

    totals = store.totals()
    assert totals.records == 6
    assert totals.characters == 72
    assert totals.duration_seconds == pytest.approx(18.75)

    one, seven, thirty = store.usage_summaries(now=now)
    assert (one.days, seven.days, thirty.days) == (1, 7, 30)
    assert one.providers == {"sensevoice": 2}
    assert one.models == {"sv": 2}
    assert seven.providers == {"qwen": 2, "sensevoice": 2}
    assert seven.models == {"q3": 2, "sv": 2}
    assert thirty.providers == {"qwen": 2, "sensevoice": 2}
    assert thirty.models == {"q3": 2, "sv": 2}


def test_all_values_are_parameterized_and_malformed_cursor_is_rejected(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    now = datetime(2026, 7, 13, tzinfo=UTC)
    hostile_id = "x'); DROP TABLE recognition_history; --"
    hostile_text = "'); DELETE FROM recognition_history; --"
    store.insert(
        _record(
            hostile_id,
            now,
            raw_text=hostile_text,
            final_text=hostile_text,
            provider=hostile_text,
            model=hostile_text,
        )
    )

    assert store.get(hostile_id) is not None
    assert store.delete("' OR 1=1 --") is False
    assert store.totals().records == 1
    with pytest.raises(HistoryError, match="游标无效"):
        store.query(cursor="not-valid-***")
    assert store.totals().records == 1
    assert store.delete(hostile_id) is True
    assert store.totals().records == 0


def test_csv_export_has_exact_header_rfc4180_quoting_and_utf8(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    now = datetime(2026, 7, 13, tzinfo=UTC)
    store.insert(
        CompletedHistoryRecord(
            id="csv-row",
            created_at=now,
            duration_seconds=1.25,
            raw_text='中文, "引号"\n第二行',
            processing_mode="voice-polish",
            processed_text="润色，成功",
            final_text="最终\n文本",
            status="completed",
            character_count=4,
            asr_provider="sensevoice-vad",
            asr_model="sensevoice-int8",
        )
    )
    destination = tmp_path / "导出.csv"

    assert store.export_csv(destination) == 1
    payload = destination.read_bytes()
    text = payload.decode("utf-8")
    assert text.startswith(",".join(CSV_HEADER) + "\r\n")
    assert '"中文, ""引号""\n第二行"' in text
    assert "\r\n" in text

    with destination.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(CSV_HEADER)
    assert rows[1] == [
        "csv-row",
        "2026-07-13T00:00:00.000000Z",
        "1.25",
        '中文, "引号"\n第二行',
        "voice-polish",
        "润色，成功",
        "最终\n文本",
        "completed",
        "4",
        "sensevoice-vad",
        "sensevoice-int8",
    ]


def test_existing_incompatible_schema_is_not_silently_replaced(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.data.mkdir(parents=True)
    with sqlite3.connect(paths.data / "history.sqlite3") as connection:
        connection.execute("CREATE TABLE recognition_history (id TEXT PRIMARY KEY)")

    with pytest.raises(HistoryError, match="表结构"):
        HistoryStore(paths)


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_busy_timeout_must_be_a_positive_integer(tmp_path: Path, timeout: int) -> None:
    with pytest.raises(ValueError, match="正整数"):
        HistoryStore(_paths(tmp_path), busy_timeout_ms=timeout)


@pytest.mark.parametrize("limit", [0, 1_001, True])
def test_query_rejects_invalid_page_sizes(tmp_path: Path, limit: int) -> None:
    store = HistoryStore(_paths(tmp_path))

    with pytest.raises(ValueError, match="1 到 1000"):
        store.query(limit=limit)


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        ("id", "", ValueError, "历史 ID不能为空"),
        ("id", 3, TypeError, "历史 ID必须是字符串"),
        ("raw_text", 3, TypeError, "原始文本必须是字符串"),
        ("final_text", None, TypeError, "最终文本必须是字符串"),
        ("status", "", ValueError, "状态不能为空"),
        ("duration_seconds", -0.1, ValueError, "识别时长不能为负数"),
        ("duration_seconds", True, ValueError, "识别时长不能为负数"),
        ("character_count", -1, ValueError, "字符数不能为负数"),
        ("character_count", False, ValueError, "字符数不能为负数"),
        ("processing_mode", 3, TypeError, "处理模式必须是字符串"),
        ("processed_text", 3, TypeError, "处理后文本必须是字符串"),
        ("asr_provider", 3, TypeError, "ASR 提供方必须是字符串"),
        ("asr_model", 3, TypeError, "ASR 模型必须是字符串"),
        ("created_at", datetime(2026, 1, 1), ValueError, "必须包含时区"),
        ("created_at", "not-a-time", ValueError, "不是有效的 RFC 3339"),
        ("created_at", 3, TypeError, "datetime 或 RFC 3339"),
    ],
)
def test_insert_validates_every_persisted_record_boundary(
    tmp_path: Path,
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    store = HistoryStore(_paths(tmp_path))
    valid = _record("valid", datetime(2026, 7, 13, tzinfo=UTC))
    invalid = replace(valid, **{field: value})

    with pytest.raises(error_type, match=message):
        store.insert(invalid)
    assert store.totals().records == 0


def test_insert_requires_completed_record_and_duplicate_id_is_reported(
    tmp_path: Path,
) -> None:
    store = HistoryStore(_paths(tmp_path))
    now = datetime(2026, 7, 13, tzinfo=UTC)

    with pytest.raises(TypeError, match="CompletedHistoryRecord"):
        store.insert(object())  # type: ignore[arg-type]

    store.insert(_record("duplicate", now))
    with pytest.raises(HistoryError, match="无法写入识别历史"):
        store.insert(_record("duplicate", now + timedelta(seconds=1)))
    assert store.totals().records == 1


def test_timestamp_offsets_normalize_to_utc_and_get_missing_returns_none(
    tmp_path: Path,
) -> None:
    store = HistoryStore(_paths(tmp_path))
    inserted = store.insert(
        replace(
            _record("offset", datetime(2026, 7, 13, tzinfo=UTC)),
            created_at="2026-07-13T08:30:00+08:00",
        )
    )

    assert inserted.created_at == "2026-07-13T00:30:00.000000Z"
    assert store.get("missing") is None


def test_usage_summary_rejects_unknown_window_and_naive_clock(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))

    with pytest.raises(ValueError, match="只能是 1、7 或 30 天"):
        store.usage_summary(2)
    with pytest.raises(ValueError, match="必须包含时区"):
        store.usage_summary(1, now=datetime(2026, 7, 13))


def test_csv_text_export_honors_half_open_date_filter(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    base = datetime(2026, 7, 1, tzinfo=UTC)
    for day in range(3):
        store.insert(_record(f"day-{day}", base + timedelta(days=day)))

    text = store.export_csv_text(
        from_date=base + timedelta(days=1),
        to_date=base + timedelta(days=2),
    )
    rows = list(csv.reader(text.splitlines()))

    assert rows[0] == list(CSV_HEADER)
    assert [row[0] for row in rows[1:]] == ["day-1"]


def test_csv_export_wraps_destination_io_failure(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    destination = tmp_path / "directory"
    destination.mkdir()

    with pytest.raises(HistoryError, match="无法导出识别历史"):
        store.export_csv(destination)


def test_future_database_version_is_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.data.mkdir(parents=True)
    with sqlite3.connect(paths.data / "history.sqlite3") as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(HistoryError, match="数据库版本过新"):
        HistoryStore(paths)


def test_database_errors_are_translated_for_each_public_operation(tmp_path: Path) -> None:
    store = HistoryStore(_paths(tmp_path))
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE recognition_history")

    now = datetime(2026, 7, 13, tzinfo=UTC)
    operations = (
        (lambda: store.get("x"), "无法读取识别历史"),
        (lambda: store.query(), "无法查询识别历史"),
        (lambda: store.delete("x"), "无法删除识别历史"),
        (lambda: store.delete_many(["x"]), "无法批量删除识别历史"),
        (store.delete_all, "无法清空识别历史"),
        (store.totals, "无法统计识别历史"),
        (lambda: store.usage_summary(1, now=now), "无法统计识别用量"),
        (store.export_csv_text, "无法读取导出历史"),
    )
    for operation, message in operations:
        with pytest.raises(HistoryError, match=message):
            operation()


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        base64.urlsafe_b64encode(json.dumps({"created_at": "x"}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps(["2026-07-13T00:00:00Z", ""]).encode()).decode(),
    ],
)
def test_cursor_structure_and_required_id_are_validated(
    tmp_path: Path,
    cursor: str,
) -> None:
    store = HistoryStore(_paths(tmp_path))

    with pytest.raises(HistoryError, match="游标无效"):
        store.query(cursor=cursor)


def test_migration_lock_open_failure_is_localized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(*_args: object, **_kwargs: object) -> int:
        raise OSError("权限不足")

    monkeypatch.setattr(history_module.os, "open", fail_open)

    with pytest.raises(HistoryError, match="无法打开历史迁移锁"):
        HistoryStore(_paths(tmp_path))


def test_migration_lock_acquisition_failure_is_localized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_flock(_descriptor: int, _operation: int) -> None:
        raise OSError("锁不可用")

    monkeypatch.setattr(history_module.fcntl, "flock", fail_flock)

    with pytest.raises(HistoryError, match="无法锁定历史数据库迁移"):
        HistoryStore(_paths(tmp_path))
