from __future__ import annotations

import stat
from pathlib import Path

import pytest

from type4me_linux.paths import AppPathError, AppPaths


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_from_environment_uses_exact_xdg_defaults_and_private_modes(tmp_path: Path) -> None:
    paths = AppPaths.from_environment({"HOME": str(tmp_path)})

    assert paths == AppPaths(
        config=tmp_path / ".config" / "type4me-linux",
        data=tmp_path / ".local" / "share" / "type4me-linux",
        cache=tmp_path / ".cache" / "type4me-linux",
        state=tmp_path / ".local" / "state" / "type4me-linux",
        runtime=None,
    )
    for path in (paths.config, paths.data, paths.cache, paths.state):
        assert path.is_dir()
        assert _mode(path) == 0o700


def test_from_environment_honors_all_xdg_roots_and_runtime(tmp_path: Path) -> None:
    roots = {
        "XDG_CONFIG_HOME": tmp_path / "配置",
        "XDG_DATA_HOME": tmp_path / "数据",
        "XDG_CACHE_HOME": tmp_path / "缓存",
        "XDG_STATE_HOME": tmp_path / "状态",
        "XDG_RUNTIME_DIR": tmp_path / "运行时",
    }
    paths = AppPaths.from_environment({name: str(path) for name, path in roots.items()})

    assert paths.config == roots["XDG_CONFIG_HOME"] / "type4me-linux"
    assert paths.data == roots["XDG_DATA_HOME"] / "type4me-linux"
    assert paths.cache == roots["XDG_CACHE_HOME"] / "type4me-linux"
    assert paths.state == roots["XDG_STATE_HOME"] / "type4me-linux"
    assert paths.runtime == roots["XDG_RUNTIME_DIR"] / "type4me-linux"
    for path in (paths.config, paths.data, paths.cache, paths.state, paths.runtime):
        assert path is not None
        assert _mode(path) == 0o700


def test_existing_application_directory_is_tightened_to_private_mode(tmp_path: Path) -> None:
    config = tmp_path / "config" / "type4me-linux"
    config.mkdir(parents=True, mode=0o755)
    config.chmod(0o755)

    paths = AppPaths.from_environment(
        {
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(config.parent),
        }
    )

    assert paths.config == config
    assert _mode(config) == 0o700


def test_directory_not_owned_by_current_user_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_uid = (tmp_path / ".").stat().st_uid
    monkeypatch.setattr("type4me_linux.paths.os.geteuid", lambda: actual_uid + 1)

    with pytest.raises(AppPathError, match="目录不属于当前用户"):
        AppPaths.from_environment({"HOME": str(tmp_path)})


def test_runtime_creation_failure_has_chinese_diagnostic(tmp_path: Path) -> None:
    runtime_base = tmp_path / "runtime-is-a-file"
    runtime_base.write_text("不可用", encoding="utf-8")

    with pytest.raises(AppPathError, match="无法创建或保护运行时目录") as raised:
        AppPaths.from_environment(
            {
                "HOME": str(tmp_path / "home"),
                "XDG_RUNTIME_DIR": str(runtime_base),
            }
        )

    assert "type4me-linux" in str(raised.value)
