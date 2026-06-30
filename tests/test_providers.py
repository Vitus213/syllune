from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from type4me_linux.config import ASRConfig
from type4me_linux.providers import (
    ASRProvider,
    FakeProvider,
    HybridProvider,
    Qwen3ASRClient,
    Qwen3SherpaProvider,
    RecognitionResult,
    SenseVoiceProvider,
    create_provider,
    parse_sherpa_output,
)


def test_parse_sherpa_output_accepts_colon_format() -> None:
    assert parse_sherpa_output("/tmp/a.wav: 你好 NixOS\n") == "你好 NixOS"


def test_parse_sherpa_output_accepts_json_line() -> None:
    assert parse_sherpa_output('{"text": "你好"}\n') == "你好"


def test_parse_sherpa_output_skips_bad_json_and_started_lines() -> None:
    output = "Started processing\n{not-json}\n/tmp/audio.wav\nplain result\n"

    assert parse_sherpa_output(output) == "plain result"


def test_parse_sherpa_output_returns_empty_string_for_empty_stdout() -> None:
    assert parse_sherpa_output("\n") == ""


def test_sensevoice_command_uses_model_dir(tmp_path: Path) -> None:
    provider = SenseVoiceProvider(ASRConfig(sensevoice_model_dir=tmp_path, language="zh"))

    command = provider._command(Path("/tmp/audio.wav"))

    assert "--model-type=sense-voice" in command
    assert f"--sense-voice-model={tmp_path / 'model.onnx'}" in command
    assert f"--tokens={tmp_path / 'tokens.txt'}" in command
    assert "--sense-voice-language=zh" in command


def test_sensevoice_reports_missing_model_files(tmp_path: Path) -> None:
    provider = SenseVoiceProvider(ASRConfig(sensevoice_model_dir=tmp_path))

    with pytest.raises(FileNotFoundError, match="SenseVoice model files missing"):
        provider.transcribe(tmp_path / "audio.wav")


def test_qwen3_sherpa_command_uses_model_dir_and_hotwords(tmp_path: Path) -> None:
    provider = Qwen3SherpaProvider(
        ASRConfig(
            qwen3_model_dir=tmp_path,
            language="zh",
            hotwords=("Qwen3-ASR", "SenseVoice"),
        )
    )

    command = provider._command(Path("/tmp/audio.wav"))

    assert f"--qwen3-asr-conv-frontend={tmp_path / 'conv_frontend.onnx'}" in command
    assert f"--qwen3-asr-encoder={tmp_path / 'encoder.onnx'}" in command
    assert f"--qwen3-asr-decoder={tmp_path / 'decoder.onnx'}" in command
    assert f"--qwen3-asr-tokenizer={tmp_path / 'tokenizer'}" in command
    assert "--qwen3-asr-hotwords=Qwen3-ASR,SenseVoice" in command
    assert "/tmp/audio.wav" in command


def test_qwen3_sherpa_command_omits_hotwords_when_not_configured(tmp_path: Path) -> None:
    provider = Qwen3SherpaProvider(ASRConfig(qwen3_model_dir=tmp_path))

    command = provider._command(Path("/tmp/audio.wav"))

    assert not any(item.startswith("--qwen3-asr-hotwords=") for item in command)


def test_qwen3_sherpa_checks_required_model_files(tmp_path: Path) -> None:
    provider = Qwen3SherpaProvider(ASRConfig(qwen3_model_dir=tmp_path))

    missing = provider.missing_model_files()

    assert missing == [
        tmp_path / "conv_frontend.onnx",
        tmp_path / "encoder.onnx",
        tmp_path / "decoder.onnx",
        tmp_path / "tokenizer",
    ]


def test_create_provider_can_select_local_qwen3(tmp_path: Path) -> None:
    provider = create_provider(ASRConfig(backend="qwen3-sherpa", qwen3_model_dir=tmp_path))

    assert isinstance(provider, Qwen3SherpaProvider)


def test_create_provider_selects_hybrid_http_qwen_when_configured(tmp_path: Path) -> None:
    provider = create_provider(
        ASRConfig(
            backend="hybrid",
            use_qwen_final=True,
            sensevoice_model_dir=tmp_path,
            qwen3_model_dir=tmp_path,
        )
    )

    assert isinstance(provider, HybridProvider)


def test_create_provider_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unsupported ASR backend"):
        create_provider(ASRConfig(backend="bogus"))


class _QwenHandler(BaseHTTPRequestHandler):
    payload: dict[str, object] | None = None
    response: dict[str, object] = {"transcript": "语音转写：热词：Qwen3-ASR 最终文本"}

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.__class__.payload = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps(self.__class__.response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_qwen3_asr_client_posts_audio_and_sanitizes_response(tmp_path: Path) -> None:
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"abc")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QwenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = Qwen3ASRClient(
            ASRConfig(
                qwen_endpoint=f"http://{host}:{port}/transcribe",
                hotwords=("Qwen3-ASR",),
            )
        )

        result = client.transcribe(wav_path, draft_text="草稿")

        assert result == RecognitionResult(text="最终文本", backend="qwen3-asr", draft_text="草稿")
        assert _QwenHandler.payload == {
            "audio_base64": "YWJj",
            "language": "zh",
            "hotwords": ["Qwen3-ASR"],
            "draft_text": "草稿",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_qwen3_asr_client_rejects_response_without_text(tmp_path: Path) -> None:
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"abc")
    _QwenHandler.response = {"unexpected": "value"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QwenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = Qwen3ASRClient(ASRConfig(qwen_endpoint=f"http://{host}:{port}/transcribe"))

        with pytest.raises(ValueError, match="must contain text or transcript"):
            client.transcribe(wav_path)
    finally:
        _QwenHandler.response = {"transcript": "语音转写：热词：Qwen3-ASR 最终文本"}
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


class _FinalQwen:
    def transcribe(self, wav_path: Path, draft_text: str | None = None) -> RecognitionResult:
        return RecognitionResult(text=f"final from {draft_text}", backend="qwen3-asr")


def test_hybrid_returns_final_text_with_draft(tmp_path: Path) -> None:
    provider = HybridProvider(FakeProvider("草稿"), _FinalQwen())  # type: ignore[arg-type]

    result = provider.transcribe(tmp_path / "audio.wav")

    assert result == RecognitionResult(text="final from 草稿", backend="hybrid", draft_text="草稿")


def test_asr_provider_protocol_stub_raises(tmp_path: Path) -> None:
    class _Stub(ASRProvider):
        pass

    with pytest.raises(NotImplementedError):
        _Stub().transcribe(tmp_path / "audio.wav")
