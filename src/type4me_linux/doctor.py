from __future__ import annotations

import shutil
from dataclasses import dataclass

from .config import Config
from .providers import SenseVoiceProvider


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(config: Config) -> list[Check]:
    sensevoice = SenseVoiceProvider(config.asr)
    checks = [
        _command_check("sherpa-onnx-offline", config.asr.sensevoice_command),
        _command_check("pw-record", config.capture.command),
        _command_check("wtype", config.inject.wtype_command),
        _command_check("wl-copy", config.inject.wl_copy_command),
        Check("sensevoice model.onnx", sensevoice.model_path.exists(), str(sensevoice.model_path)),
        Check("sensevoice tokens.txt", sensevoice.tokens_path.exists(), str(sensevoice.tokens_path)),
    ]
    return checks


def _command_check(name: str, command: str) -> Check:
    found = shutil.which(command)
    return Check(name, found is not None, found or f"{command} not found in PATH")

