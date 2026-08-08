from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

from type4me_linux.config import load_config
from type4me_linux.model_manager import ModelManager
from type4me_linux.paths import AppPaths
from type4me_linux.providers import SenseVoiceProvider


@pytest.mark.real_asr
def test_installed_sensevoice_transcribes_pcm16_silence(tmp_path: Path) -> None:
    if os.environ.get("TYPE4ME_REAL_ASR") != "1":
        pytest.skip("需要 TYPE4ME_REAL_ASR=1 才运行真实 ASR 冒烟测试")

    wav_path = tmp_path / "silence.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * 16_000)

    manager = ModelManager(AppPaths.from_environment())
    provider = SenseVoiceProvider(load_config().asr, model_resolver=manager.resolve)
    result = provider.transcribe(wav_path)

    assert result.backend == "sensevoice"
    assert isinstance(result.text, str)
