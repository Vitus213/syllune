from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .model_manager import ModelManager
from .paths import AppPaths, AppPathError


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    category: str = "runtime"
    allowed_missing_model: bool = False


ProbeResult = bool | str | tuple[bool, str]


def run_checks(
    config: Config,
    *,
    executable_probe: Callable[[str], str | None] = shutil.which,
    import_probe: Callable[[str], Any] = importlib.import_module,
    path_probe: Callable[[Path], ProbeResult] | None = None,
    model_probe: Callable[[str], Mapping[str, object]] | None = None,
    portal_probe: Callable[[], ProbeResult] | None = None,
    paths: AppPaths | None = None,
) -> list[Check]:
    checks = [
        _executable_check("pw-record", config.capture.command, executable_probe),
        _executable_check("wtype", config.inject.wtype_command, executable_probe),
        _executable_check("wl-copy", config.inject.wl_copy_command, executable_probe),
        _executable_check("wl-paste", "wl-paste", executable_probe),
        _import_check("sherpa_onnx", import_probe),
    ]

    try:
        app_paths = paths or AppPaths.from_environment()
    except AppPathError as exc:
        checks.append(Check("XDG 应用目录", False, str(exc), "xdg"))
        app_paths = None
    else:
        probe = path_probe or _writable_path_probe
        for label, root in _path_roots(app_paths):
            try:
                ok, detail = _normalize_probe(probe(root), success_detail=str(root))
            except Exception as exc:
                ok, detail = False, f"检查目录失败：{exc}"
            checks.append(Check(label, ok, detail, "xdg"))

    if model_probe is None and app_paths is not None:
        try:
            manager = ModelManager(app_paths)
            model_probe = manager.check
        except Exception as exc:

            def unavailable_model_probe(
                _model_id: str, _error: Exception = exc
            ) -> Mapping[str, object]:
                return _raise_model_probe_error(_error)

            model_probe = unavailable_model_probe

    model_ids = (
        ("SenseVoice 模型", config.asr.sensevoice_model_id),
        ("Silero VAD 模型", config.asr.vad_model_id),
        ("Qwen3-ASR 模型", config.asr.qwen3_model_id),
    )
    for label, model_id in model_ids:
        if model_probe is None:
            checks.append(Check(label, False, "XDG 应用目录不可用，无法检查模型。", "model"))
            continue
        checks.append(_model_check(label, model_id, model_probe))

    portal = portal_probe or _portal_capability_probe
    try:
        ok, detail = _normalize_probe(portal(), success_detail="GlobalShortcuts 可用。")
    except Exception as exc:
        ok, detail = False, f"无法查询 GlobalShortcuts：{exc}"
    checks.append(Check("全局快捷键门户", ok, detail, "portal"))
    return checks


def _executable_check(
    name: str,
    command: str,
    probe: Callable[[str], str | None],
) -> Check:
    try:
        found = probe(command)
    except Exception as exc:
        return Check(f"命令 {name}", False, f"检查命令失败：{exc}", "executable")
    ok = bool(found)
    detail = str(found) if ok else f"PATH 中未找到 {command}。"
    return Check(f"命令 {name}", ok, detail, "executable")


def _import_check(name: str, probe: Callable[[str], Any]) -> Check:
    try:
        result = probe(name)
        if isinstance(result, (bool, str, tuple)):
            ok, detail = _normalize_probe(result, success_detail="可以导入。")
        else:
            location = getattr(result, "__file__", None)
            ok, detail = True, str(location) if location else "可以导入。"
    except Exception as exc:
        return Check(f"Python 模块 {name}", False, f"无法导入：{exc}", "python")
    return Check(f"Python 模块 {name}", ok, detail, "python")


def _path_roots(paths: AppPaths) -> tuple[tuple[str, Path], ...]:
    roots = (
        ("XDG 配置目录", paths.config),
        ("XDG 数据目录", paths.data),
        ("XDG 缓存目录", paths.cache),
        ("XDG 状态目录", paths.state),
    )
    if paths.runtime is not None:
        return (*roots, ("XDG 运行时目录", paths.runtime))
    return roots


def _writable_path_probe(path: Path) -> tuple[bool, str]:
    try:
        descriptor, candidate = tempfile.mkstemp(prefix=".doctor-", dir=path)
        os.close(descriptor)
        Path(candidate).unlink()
    except OSError as exc:
        return False, f"目录不可写：{exc}"
    return True, str(path)


def _model_check(
    label: str,
    model_id: str,
    probe: Callable[[str], Mapping[str, object]],
) -> Check:
    try:
        result = probe(model_id)
        if not isinstance(result, Mapping):
            raise TypeError("模型探针必须返回映射。")
        ok = result.get("ok") is True
        allowed_missing = _is_uninstalled_payload(result)
        if ok:
            detail = str(result.get("path") or result.get("version") or f"模型 {model_id} 完整。")
        elif allowed_missing:
            detail = _model_failure_detail(result) or f"模型 {model_id} 尚未安装。"
        else:
            detail = _model_failure_detail(result) or f"模型 {model_id} 未通过完整性校验。"
    except Exception as exc:
        return Check(label, False, f"模型 {model_id} 检查失败：{exc}", "model")
    return Check(label, ok, detail, "model", allowed_missing_model=allowed_missing)


def _raise_model_probe_error(error: Exception) -> Mapping[str, object]:
    raise error


def _is_uninstalled_payload(result: Mapping[str, object]) -> bool:
    if result.get("installed") is not False:
        return False
    if any(result.get(key) for key in ("missing", "extra", "corrupt")):
        return False
    return result.get("errors") == ["模型尚未安装。"]


def _model_failure_detail(result: Mapping[str, object]) -> str:
    details: list[str] = []
    labels = (
        ("missing", "缺失文件"),
        ("extra", "多余文件"),
        ("corrupt", "损坏文件"),
        ("errors", "错误"),
    )
    for key, label in labels:
        values = result.get(key)
        if isinstance(values, (list, tuple)) and values:
            details.append(f"{label}：{'、'.join(str(value) for value in values)}")
    return "；".join(details)


def _portal_capability_probe() -> tuple[bool, str]:
    try:
        from .shortcuts import GioPortalTransport

        version = GioPortalTransport().get_global_shortcuts_version()
    except Exception as exc:
        return False, f"GlobalShortcuts 不可用：{exc}"
    if version < 1:
        return False, f"GlobalShortcuts 接口版本无效：{version}"
    return True, f"GlobalShortcuts 接口版本 {version}。"


def _normalize_probe(result: ProbeResult, *, success_detail: str) -> tuple[bool, str]:
    if isinstance(result, tuple):
        if len(result) != 2 or type(result[0]) is not bool or not isinstance(result[1], str):
            raise TypeError("探针必须返回 (bool, str)。")
        return result
    if type(result) is bool:
        return result, success_detail if result else "检查未通过。"
    if isinstance(result, str):
        return True, result
    raise TypeError("探针返回了不支持的结果。")
