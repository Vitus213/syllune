from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[bytes]]

_CLIPBOARD_ARGV = ["wl-paste", "--no-newline"]
_PRIMARY_ARGV = ["wl-paste", "--primary", "--no-newline"]


@dataclass(frozen=True)
class ClipboardSnapshot:
    clipboard: str
    selected: str
    warnings: tuple[str, ...] = ()


class ClipboardSnapshotService:
    """分别读取两个 Wayland 文本选区，避免相互耦合。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def snapshot(self) -> ClipboardSnapshot:
        clipboard, clipboard_warning = self._read("剪贴板", _CLIPBOARD_ARGV)
        selected, selected_warning = self._read("主选区", _PRIMARY_ARGV, warn_if_empty=True)
        warnings = tuple(
            warning for warning in (clipboard_warning, selected_warning) if warning is not None
        )
        return ClipboardSnapshot(
            clipboard=clipboard,
            selected=selected,
            warnings=warnings,
        )

    def _read(
        self,
        display_name: str,
        argv: list[str],
        *,
        warn_if_empty: bool = False,
    ) -> tuple[str, str | None]:
        try:
            completed = self._runner(
                argv,
                check=False,
                capture_output=True,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError:
            return "", f"无法读取{display_name}：未找到 wl-paste。"
        except subprocess.TimeoutExpired:
            return "", f"无法读取{display_name}：wl-paste 执行超时。"
        except subprocess.CalledProcessError as exc:
            return "", self._command_failure_warning(display_name, exc.returncode)
        except OSError:
            return "", f"无法读取{display_name}：wl-paste 读取失败。"

        if completed.returncode != 0:
            return "", self._command_failure_warning(display_name, completed.returncode)

        try:
            value = self._decode_stdout(completed.stdout)
        except (TypeError, UnicodeDecodeError):
            return "", f"无法读取{display_name}：内容不是有效的 UTF-8 文本。"

        if warn_if_empty and not value:
            return "", "主选区为空。"
        return value, None

    @staticmethod
    def _decode_stdout(stdout: Any) -> str:
        if not isinstance(stdout, bytes):
            raise TypeError("wl-paste 的标准输出必须是字节数据")
        return stdout.decode("utf-8", errors="strict")

    @staticmethod
    def _command_failure_warning(display_name: str, returncode: int) -> str:
        return f"无法读取{display_name}：wl-paste 退出状态为 {returncode}。"
