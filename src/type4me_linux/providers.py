from __future__ import annotations

import base64
import json
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ASRConfig
from .hotwords import sanitize_qwen_output


@dataclass(frozen=True)
class RecognitionResult:
    text: str
    backend: str
    draft_text: str | None = None


class ASRProvider(Protocol):
    def transcribe(self, wav_path: Path) -> RecognitionResult:
        raise NotImplementedError


class FakeProvider:
    def __init__(self, text: str = "测试语音输入") -> None:
        self.text = text

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        return RecognitionResult(text=self.text, backend="fake")


class SenseVoiceProvider:
    def __init__(self, config: ASRConfig) -> None:
        self.config = config

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        self._ensure_model_files()
        completed = subprocess.run(
            self._command(wav_path),
            check=True,
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
        )
        return RecognitionResult(text=parse_sherpa_output(completed.stdout), backend="sensevoice")

    def _ensure_model_files(self) -> None:
        missing = [path for path in [self.model_path, self.tokens_path] if not path.exists()]
        if missing:
            joined = ", ".join(str(path) for path in missing)
            raise FileNotFoundError(f"SenseVoice model files missing: {joined}")

    def _command(self, wav_path: Path) -> list[str]:
        return [
            self.config.sensevoice_command,
            "--model-type=sense-voice",
            f"--sense-voice-model={self.model_path}",
            f"--tokens={self.tokens_path}",
            f"--sense-voice-language={self.config.language}",
            "--sense-voice-use-itn=true",
            f"--provider={self.config.provider}",
            f"--num-threads={self.config.num_threads}",
            "--print-args=false",
            str(wav_path),
        ]

    @property
    def model_path(self) -> Path:
        return self.config.model_dir / "model.onnx"

    @property
    def tokens_path(self) -> Path:
        return self.config.model_dir / "tokens.txt"


class Qwen3ASRClient:
    def __init__(self, config: ASRConfig) -> None:
        self.config = config

    def transcribe(self, wav_path: Path, draft_text: str | None = None) -> RecognitionResult:
        payload = {
            "audio_base64": base64.b64encode(wav_path.read_bytes()).decode("ascii"),
            "language": self.config.language,
            "hotwords": list(self.config.hotwords),
            "draft_text": draft_text,
        }
        request = urllib.request.Request(
            self.config.qwen_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
        text = raw.get("text") or raw.get("transcript")
        if not isinstance(text, str):
            raise ValueError("Qwen3-ASR response must contain text or transcript")
        return RecognitionResult(
            text=sanitize_qwen_output(text, self.config.hotwords),
            backend="qwen3-asr",
            draft_text=draft_text,
        )


class HybridProvider:
    def __init__(self, sensevoice: ASRProvider, qwen: Qwen3ASRClient) -> None:
        self.sensevoice = sensevoice
        self.qwen = qwen

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        draft = self.sensevoice.transcribe(wav_path)
        try:
            final = self.qwen.transcribe(wav_path, draft_text=draft.text)
        except Exception:
            return RecognitionResult(text=draft.text, backend="hybrid-fallback", draft_text=draft.text)
        return RecognitionResult(text=final.text, backend="hybrid", draft_text=draft.text)


def create_provider(config: ASRConfig) -> ASRProvider:
    backend = config.backend.lower()
    if backend == "fake":
        return FakeProvider()
    if backend == "sensevoice":
        return SenseVoiceProvider(config)
    if backend == "qwen3-asr":
        return _QwenProvider(config)
    if backend == "hybrid":
        return HybridProvider(SenseVoiceProvider(config), Qwen3ASRClient(config))
    raise ValueError(f"unsupported ASR backend: {config.backend}")


class _QwenProvider:
    def __init__(self, config: ASRConfig) -> None:
        self.client = Qwen3ASRClient(config)

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        return self.client.transcribe(wav_path)


def parse_sherpa_output(stdout: str) -> str:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = payload.get("text") or payload.get("transcript")
            if isinstance(text, str):
                return text.strip()
        if not line.endswith(".wav") and not line.startswith("Started"):
            if ":" in line:
                return line.split(":", 1)[1].strip()
            return line
    return lines[-1]

