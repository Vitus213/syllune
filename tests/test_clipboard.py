from __future__ import annotations

import subprocess
from typing import Any

import pytest

from type4me_linux.clipboard import ClipboardSnapshot, ClipboardSnapshotService


def _completed(
    argv: list[str], stdout: bytes, returncode: int = 0
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")


def test_snapshot_uses_exact_argv_order_and_timeout() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        stdout = b"clipboard text" if "--primary" not in argv else "主选区文本".encode()
        return _completed(argv, stdout)

    result = ClipboardSnapshotService(timeout_seconds=1.25, runner=runner).snapshot()

    assert result == ClipboardSnapshot(
        clipboard="clipboard text",
        selected="主选区文本",
        warnings=(),
    )
    assert calls == [
        (
            ["wl-paste", "--no-newline"],
            {"check": False, "capture_output": True, "timeout": 1.25},
        ),
        (
            ["wl-paste", "--primary", "--no-newline"],
            {"check": False, "capture_output": True, "timeout": 1.25},
        ),
    ]


def test_missing_executable_is_recoverable_and_localized() -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("wl-paste")

    result = ClipboardSnapshotService(runner=runner).snapshot()

    assert result.clipboard == ""
    assert result.selected == ""
    assert result.warnings == (
        "无法读取剪贴板：未找到 wl-paste。",
        "无法读取主选区：未找到 wl-paste。",
    )


def test_empty_primary_selection_does_not_discard_clipboard() -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        stdout = "保留的剪贴板".encode() if "--primary" not in argv else b""
        return _completed(argv, stdout)

    result = ClipboardSnapshotService(runner=runner).snapshot()

    assert result == ClipboardSnapshot(
        clipboard="保留的剪贴板",
        selected="",
        warnings=("主选区为空。",),
    )


@pytest.mark.parametrize(
    ("failure", "warning"),
    [
        (
            subprocess.TimeoutExpired(["wl-paste", "--no-newline"], 2.0),
            "无法读取剪贴板：wl-paste 执行超时。",
        ),
        (
            _completed(["wl-paste", "--no-newline"], b"ignored", returncode=7),
            "无法读取剪贴板：wl-paste 退出状态为 7。",
        ),
        (
            _completed(["wl-paste", "--no-newline"], b"\xff"),
            "无法读取剪贴板：内容不是有效的 UTF-8 文本。",
        ),
        (
            OSError("read failed"),
            "无法读取剪贴板：wl-paste 读取失败。",
        ),
    ],
    ids=("timeout", "nonzero", "decode", "read"),
)
def test_clipboard_failure_only_clears_clipboard(
    failure: BaseException | subprocess.CompletedProcess[bytes], warning: str
) -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "--primary" in argv:
            return _completed(argv, "仍可读取的主选区".encode())
        if isinstance(failure, BaseException):
            raise failure
        return failure

    result = ClipboardSnapshotService(runner=runner).snapshot()

    assert result == ClipboardSnapshot(
        clipboard="",
        selected="仍可读取的主选区",
        warnings=(warning,),
    )


def test_primary_command_failure_only_clears_primary() -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "--primary" in argv:
            raise subprocess.CalledProcessError(9, argv)
        return _completed(argv, "仍可读取的剪贴板".encode())

    result = ClipboardSnapshotService(runner=runner).snapshot()

    assert result == ClipboardSnapshot(
        clipboard="仍可读取的剪贴板",
        selected="",
        warnings=("无法读取主选区：wl-paste 退出状态为 9。",),
    )


def test_unreadable_stdout_is_recoverable() -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "--primary" in argv:
            return _completed(argv, b"selection")
        return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=b"")

    result = ClipboardSnapshotService(runner=runner).snapshot()

    assert result.clipboard == ""
    assert result.selected == "selection"
    assert result.warnings == ("无法读取剪贴板：内容不是有效的 UTF-8 文本。",)
