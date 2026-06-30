from __future__ import annotations

import shutil
from dataclasses import dataclass

from .config import Config
from .providers import Qwen3SherpaProvider, SenseVoiceProvider


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(config: Config) -> list[Check]:
    sensevoice = SenseVoiceProvider(config.asr)
    qwen3 = Qwen3SherpaProvider(config.asr)
    checks = [
        _command_check("sherpa-onnx-offline", config.asr.sensevoice_command),
        _command_check("pw-record", config.capture.command),
        _command_check("wtype", config.inject.wtype_command),
        _command_check("wl-copy", config.inject.wl_copy_command),
        Check("sensevoice model.onnx", sensevoice.model_path.exists(), str(sensevoice.model_path)),
        Check(
            "sensevoice tokens.txt", sensevoice.tokens_path.exists(), str(sensevoice.tokens_path)
        ),
        Check(
            "qwen3-asr conv_frontend.onnx",
            qwen3.conv_frontend_path.exists(),
            str(qwen3.conv_frontend_path),
        ),
        Check("qwen3-asr encoder.onnx", qwen3.encoder_path.exists(), str(qwen3.encoder_path)),
        Check("qwen3-asr decoder.onnx", qwen3.decoder_path.exists(), str(qwen3.decoder_path)),
        Check("qwen3-asr tokenizer", qwen3.tokenizer_path.exists(), str(qwen3.tokenizer_path)),
    ]
    return checks


def _command_check(name: str, command: str) -> Check:
    found = shutil.which(command)
    return Check(name, found is not None, found or f"{command} not found in PATH")
