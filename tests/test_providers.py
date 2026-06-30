from __future__ import annotations

from pathlib import Path

from type4me_linux.config import ASRConfig
from type4me_linux.providers import (
    FakeProvider,
    HybridProvider,
    Qwen3ASRClient,
    RecognitionResult,
    SenseVoiceProvider,
    parse_sherpa_output,
)


def test_parse_sherpa_output_accepts_colon_format() -> None:
    assert parse_sherpa_output("/tmp/a.wav: 你好 NixOS\n") == "你好 NixOS"


def test_parse_sherpa_output_accepts_json_line() -> None:
    assert parse_sherpa_output('{"text": "你好"}\n') == "你好"


def test_sensevoice_command_uses_model_dir(tmp_path: Path) -> None:
    provider = SenseVoiceProvider(ASRConfig(model_dir=tmp_path, language="zh"))

    command = provider._command(Path("/tmp/audio.wav"))

    assert "--model-type=sense-voice" in command
    assert f"--sense-voice-model={tmp_path / 'model.onnx'}" in command
    assert f"--tokens={tmp_path / 'tokens.txt'}" in command
    assert "--sense-voice-language=zh" in command


class _FailingQwen(Qwen3ASRClient):
    def __init__(self) -> None:
        pass

    def transcribe(self, wav_path: Path, draft_text: str | None = None) -> RecognitionResult:
        raise RuntimeError("qwen unavailable")


def test_hybrid_falls_back_to_sensevoice(tmp_path: Path) -> None:
    provider = HybridProvider(FakeProvider("草稿"), _FailingQwen())

    result = provider.transcribe(tmp_path / "audio.wav")

    assert result.text == "草稿"
    assert result.backend == "hybrid-fallback"

