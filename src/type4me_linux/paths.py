from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class AppPathError(RuntimeError):
    """应用目录不可安全使用。"""


@dataclass(frozen=True)
class AppPaths:
    config: Path
    data: Path
    cache: Path
    state: Path
    runtime: Path | None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> AppPaths:
        env = os.environ if environment is None else environment
        home = Path(env["HOME"]).expanduser() if env.get("HOME") else Path.home()

        paths = cls(
            config=_xdg_root(env, "XDG_CONFIG_HOME", home / ".config") / "type4me-linux",
            data=_xdg_root(env, "XDG_DATA_HOME", home / ".local" / "share") / "type4me-linux",
            cache=_xdg_root(env, "XDG_CACHE_HOME", home / ".cache") / "type4me-linux",
            state=_xdg_root(env, "XDG_STATE_HOME", home / ".local" / "state") / "type4me-linux",
            runtime=(
                Path(env["XDG_RUNTIME_DIR"]).expanduser() / "type4me-linux"
                if env.get("XDG_RUNTIME_DIR")
                else None
            ),
        )

        for path in (paths.config, paths.data, paths.cache, paths.state):
            _ensure_private_directory(path, runtime=False)
        if paths.runtime is not None:
            _ensure_private_directory(paths.runtime, runtime=True)
        return paths


def _xdg_root(env: Mapping[str, str], variable: str, default: Path) -> Path:
    value = env.get(variable)
    return Path(value).expanduser() if value else default


def _ensure_private_directory(path: Path, *, runtime: bool) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(path)
        if metadata.st_uid != os.geteuid():
            raise PermissionError(f"目录不属于当前用户：{path}")
        path.chmod(0o700)
    except (OSError, ValueError) as exc:
        kind = "运行时目录" if runtime else "应用目录"
        raise AppPathError(f"无法创建或保护{kind}“{path}”：{exc}") from exc
