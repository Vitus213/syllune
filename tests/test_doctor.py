from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from type4me_linux.config import Config
from type4me_linux.doctor import run_checks
from type4me_linux.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    roots = AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=tmp_path / "runtime",
    )
    for root in (roots.config, roots.data, roots.cache, roots.state, roots.runtime):
        assert root is not None
        root.mkdir()
    return roots


def _healthy_model(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "installed": True,
        "ok": True,
        "version": "v1",
        "path": f"/models/{model_id}",
        "missing": [],
        "extra": [],
        "corrupt": [],
        "errors": [],
    }


def test_every_required_probe_runs_with_stable_model_ids(tmp_path: Path) -> None:
    executable_calls: list[str] = []
    import_calls: list[str] = []
    path_calls: list[Path] = []
    model_calls: list[str] = []
    portal_calls: list[None] = []

    def executable_probe(command: str) -> str:
        executable_calls.append(command)
        return f"/bin/{command}"

    def import_probe(name: str) -> object:
        import_calls.append(name)
        return SimpleNamespace(__file__="/python/sherpa_onnx/__init__.py")

    def path_probe(path: Path) -> tuple[bool, str]:
        path_calls.append(path)
        return True, f"可写：{path}"

    def model_probe(model_id: str) -> dict[str, object]:
        model_calls.append(model_id)
        return _healthy_model(model_id)

    def portal_probe() -> tuple[bool, str]:
        portal_calls.append(None)
        return True, "GlobalShortcuts 接口版本 2。"

    paths = _paths(tmp_path)
    checks = run_checks(
        Config(),
        executable_probe=executable_probe,
        import_probe=import_probe,
        path_probe=path_probe,
        model_probe=model_probe,
        portal_probe=portal_probe,
        paths=paths,
    )

    assert executable_calls == ["pw-record", "wtype", "wl-copy", "wl-paste"]
    assert import_calls == ["sherpa_onnx"]
    assert path_calls == [paths.config, paths.data, paths.cache, paths.state, paths.runtime]
    assert model_calls == ["sensevoice-int8", "silero-vad", "qwen3-asr-0.6b-int8"]
    assert portal_calls == [None]
    assert len(checks) == 14
    assert all(check.ok for check in checks)
    assert {check.category for check in checks} == {
        "executable",
        "python",
        "xdg",
        "model",
        "portal",
    }


def test_missing_payload_is_the_only_allowable_model_failure(tmp_path: Path) -> None:
    def model_probe(model_id: str) -> dict[str, object]:
        if model_id == "sensevoice-int8":
            return {
                "installed": False,
                "ok": False,
                "missing": [],
                "extra": [],
                "corrupt": [],
                "errors": ["模型尚未安装。"],
            }
        if model_id == "silero-vad":
            return {
                "installed": True,
                "ok": False,
                "missing": ["silero_vad.onnx"],
                "extra": [],
                "corrupt": [],
                "errors": [],
            }
        return {
            "installed": False,
            "ok": False,
            "missing": [],
            "extra": [],
            "corrupt": [],
            "errors": ["current 指针损坏。"],
        }

    checks = run_checks(
        Config(),
        executable_probe=lambda command: f"/bin/{command}",
        import_probe=lambda name: True,
        path_probe=lambda path: True,
        model_probe=model_probe,
        portal_probe=lambda: True,
        paths=_paths(tmp_path),
    )
    models = {check.name: check for check in checks if check.category == "model"}

    assert not models["SenseVoice 模型"].ok
    assert models["SenseVoice 模型"].allowed_missing_model
    assert "模型尚未安装" in models["SenseVoice 模型"].detail
    assert not models["Silero VAD 模型"].ok
    assert not models["Silero VAD 模型"].allowed_missing_model
    assert "缺失文件：silero_vad.onnx" in models["Silero VAD 模型"].detail
    assert not models["Qwen3-ASR 模型"].ok
    assert not models["Qwen3-ASR 模型"].allowed_missing_model
    assert "current 指针损坏" in models["Qwen3-ASR 模型"].detail


def test_non_model_probe_failures_are_localized_and_not_allowable(tmp_path: Path) -> None:
    def executable_probe(command: str) -> str | None:
        return None if command == "wtype" else f"/bin/{command}"

    def import_probe(name: str) -> object:
        raise ModuleNotFoundError(name)

    def path_probe(path: Path) -> tuple[bool, str]:
        if path.name == "cache":
            return False, "目录不可写。"
        return True, str(path)

    checks = run_checks(
        Config(),
        executable_probe=executable_probe,
        import_probe=import_probe,
        path_probe=path_probe,
        model_probe=_healthy_model,
        portal_probe=lambda: (False, "GlobalShortcuts 不可用。"),
        paths=_paths(tmp_path),
    )
    by_name = {check.name: check for check in checks}

    assert by_name["命令 wtype"].detail == "PATH 中未找到 wtype。"
    assert "无法导入" in by_name["Python 模块 sherpa_onnx"].detail
    assert by_name["XDG 缓存目录"].detail == "目录不可写。"
    assert by_name["全局快捷键门户"].detail == "GlobalShortcuts 不可用。"
    assert all(not check.allowed_missing_model for check in checks)


def test_probe_exceptions_become_failed_checks_instead_of_aborting(tmp_path: Path) -> None:
    def fail_path(path: Path) -> bool:
        raise PermissionError("拒绝访问")

    def fail_model(model_id: str) -> dict[str, object]:
        raise RuntimeError("清单损坏")

    checks = run_checks(
        Config(),
        executable_probe=lambda command: f"/bin/{command}",
        import_probe=lambda name: (False, "绑定不可用。"),
        path_probe=fail_path,
        model_probe=fail_model,
        portal_probe=lambda: (_ for _ in ()).throw(RuntimeError("没有门户")),
        paths=_paths(tmp_path),
    )

    assert len(checks) == 14
    assert "绑定不可用" in checks[4].detail
    assert all("检查目录失败" in check.detail for check in checks[5:10])
    assert all("检查失败" in check.detail for check in checks[10:13])
    assert "无法查询 GlobalShortcuts" in checks[13].detail
