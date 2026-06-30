from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from .config import InjectConfig

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class InjectionResult:
    method: str
    ok: bool
    message: str = ""


class TextInjector:
    def __init__(self, config: InjectConfig, runner: Runner = subprocess.run) -> None:
        self.config = config
        self.runner = runner

    def inject(self, text: str) -> InjectionResult:
        if self.config.prefer == "clipboard":
            return self._clipboard(text)
        result = self._wtype(text)
        if result.ok or not self.config.clipboard_fallback:
            return result
        return self._clipboard(text)

    def _wtype(self, text: str) -> InjectionResult:
        if shutil.which(self.config.wtype_command) is None:
            return InjectionResult("wtype", False, "wtype not found")
        try:
            self.runner(
                [self.config.wtype_command, text],
                check=True,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:
            return InjectionResult("wtype", False, str(exc))
        return InjectionResult("wtype", True)

    def _clipboard(self, text: str) -> InjectionResult:
        if shutil.which(self.config.wl_copy_command) is None:
            return InjectionResult("clipboard", False, "wl-copy not found")
        try:
            self.runner(
                [self.config.wl_copy_command],
                input=text,
                check=True,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:
            return InjectionResult("clipboard", False, str(exc))
        return InjectionResult("clipboard", True)
