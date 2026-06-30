from __future__ import annotations

import subprocess

import pytest

from type4me_linux.config import InjectConfig
from type4me_linux.inject import TextInjector


def test_injector_falls_back_to_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_which(command: str) -> str | None:
        return f"/bin/{command}"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "wtype":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("shutil.which", fake_which)
    injector = TextInjector(InjectConfig(), runner=fake_run)

    result = injector.inject("你好")

    assert result.ok
    assert result.method == "clipboard"
    assert calls == [["wtype", "你好"], ["wl-copy"]]
