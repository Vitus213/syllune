from __future__ import annotations

import base64
import json

import time
import urllib.error
import urllib.request
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import CloudConfig
from .events import RecognitionTranscript
from .providers import RecognitionResult, SenseVoiceVadStreamer

GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

# Omni 系模型是对话模型：不加固定提示时，问句会触发“聊天回答”而非转写。
SYSTEM_TRANSCRIBE_PROMPT = (
    "你是语音转写引擎，必须逐字转写用户提供的音频内容，"
    "严格输出转写文本本身，不要回答、分析、解释或添加任何额外内容。"
)
SYSTEM_PROMPT_MODELS = {"qwen3-omni-flash", "qwen3.5-omni-flash"}


class CloudASRError(RuntimeError):
    """云端语音识别失败。"""


class CloudASRAuthenticationError(CloudASRError):
    """API 密钥缺失或无效。"""


class CloudASRRequestError(CloudASRError):
    """HTTP / 网络 / 超时错误（重试后仍失败）。"""


class CloudASRResponseError(CloudASRError):
    """响应结构不符合预期或文本为空。"""


def needs_system_prompt(model: str) -> bool:
    return model in SYSTEM_PROMPT_MODELS


class CloudASRClient:
    """调用百炼多模态生成接口完成语音转写（音频以 base64 直传，无需上传）。

    所有网络交互经 stdlib urllib；重试仅覆盖可恢复错误（429、5xx、网络异常），
    4xx 立即失败，401/403 归为鉴权错误。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.5,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep

    def transcribe_audio(
        self,
        wav_bytes: bytes,
        *,
        prompt: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        payload = self._build_payload(wav_bytes, prompt, system_prompt)
        encoded = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}{GENERATION_PATH}"

        last_error: CloudASRError | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._post(url, encoded)
            except (urllib.error.URLError, OSError) as exc:
                last_error = CloudASRRequestError(
                    f"云端语音识别网络请求失败：{exc}（尝试 {attempt + 1}/{self.max_attempts}）"
                )
                if attempt + 1 < self.max_attempts:
                    self._backoff(attempt)
                continue

            status = getattr(response, "status", None)
            body = response.read()
            if status == 200:
                return self._extract_text(body)
            if status in (401, 403):
                raise CloudASRAuthenticationError(f"云端语音识别密钥无效（HTTP {status}）。")
            if status == 429 or (status is not None and status >= 500):
                last_error = CloudASRRequestError(
                    f"云端语音识别服务暂时不可用（HTTP {status}，"
                    f"尝试 {attempt + 1}/{self.max_attempts}）。"
                )
                if attempt + 1 < self.max_attempts:
                    self._backoff(attempt)
                continue
            raise CloudASRRequestError(f"云端语音识别请求被拒绝（HTTP {status}）：{body[:200]!r}")

        assert last_error is not None
        raise last_error

    def _post(self, url: str, encoded: bytes) -> Any:
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "type4me-linux/0.1",
            },
        )
        return self._urlopen(request, timeout=self.timeout_seconds)

    def _build_payload(
        self,
        wav_bytes: bytes,
        prompt: str | None,
        system_prompt: str | None,
    ) -> dict[str, Any]:
        encoded = base64.b64encode(wav_bytes).decode("ascii")
        content: list[dict[str, str]] = [{"audio": f"data:audio/wav;base64,{encoded}"}]
        if prompt:
            content.append({"text": prompt})
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})
        return {"model": self.model, "input": {"messages": messages}}

    def _extract_text(self, body: bytes) -> str:
        try:
            parsed = json.loads(body.decode("utf-8"))
            content = parsed["output"]["choices"][0]["message"].get("content") or []
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise CloudASRResponseError(f"云端语音识别响应格式无效：{body[:200]!r}") from exc
        if not content:
            # 静音/空音频时云端返回空 content，视为"没有转写文本"而非错误。
            return ""
        try:
            text = content[0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudASRResponseError(f"云端语音识别响应格式无效：{body[:200]!r}") from exc
        if not isinstance(text, str) or not text.strip():
            raise CloudASRResponseError("云端语音识别返回的转写文本为空。")
        return text.strip()

    def _backoff(self, attempt: int) -> None:
        self._sleep(self.backoff_base_seconds * (2**attempt))


def _float32_to_pcm16(samples: Any, np: Any) -> bytes:
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (clipped * np.float32(32768.0)).astype("<i2")
    return pcm.tobytes()


def _load_wav_pcm16_bytes(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getframerate() != 16000:
            raise ValueError(f"WAV 采样率必须为 16000 Hz（实际 {wav_file.getframerate()}）")
        if wav_file.getnchannels() != 1:
            raise ValueError("WAV 必须是单声道")
        if wav_file.getsampwidth() != 2 or wav_file.getcomptype() != "NONE":
            raise ValueError("WAV 必须是未压缩的 PCM16")
        return wav_file.readframes(wav_file.getnframes())


class CloudASRProvider:
    """以 `ASRProvider` 协议封装的云端批量转写（backend 恒为 "cloud"）。

    凭据直接读取配置节 `[cloud].api_key`（与 omp models.yml 的 apiKey 字段
    一致），不依赖环境变量。
    """

    def __init__(
        self,
        cloud: CloudConfig,
        *,
        client_factory: Callable[..., CloudASRClient] | None = None,
        numpy_module: Any | None = None,
    ) -> None:
        self.cloud = cloud
        self._client_factory = client_factory or self._default_client_factory
        self._numpy_module = numpy_module

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        pcm16 = _load_wav_pcm16_bytes(wav_path)
        return RecognitionResult(
            text=self.transcribe_pcm16(pcm16),
            backend="cloud",
        )

    def transcribe_samples(self, samples: Any) -> str:
        pcm16 = _float32_to_pcm16(samples, self._numpy())
        return self.transcribe_pcm16(pcm16)

    def transcribe_pcm16(self, pcm16: bytes) -> str:
        api_key = self.cloud.api_key.strip()
        if not api_key:
            raise CloudASRAuthenticationError(
                "未配置云端语音识别密钥：请在配置节 [cloud] 设置 api_key。"
            )
        client = self._client_factory(
            base_url=self.cloud.base_url,
            api_key=api_key,
            model=self.cloud.model,
            timeout_seconds=self.cloud.timeout_seconds,
        )
        is_omni = needs_system_prompt(self.cloud.model)
        return client.transcribe_audio(
            pcm16,
            prompt="请转写" if is_omni else None,
            system_prompt=SYSTEM_TRANSCRIBE_PROMPT if is_omni else None,
        )

    def _default_client_factory(self, **kwargs: Any) -> CloudASRClient:
        return CloudASRClient(**kwargs)

    def _numpy(self) -> Any:
        if self._numpy_module is None:
            import numpy

            self._numpy_module = numpy
        return self._numpy_module


class CloudVadStreamer(SenseVoiceVadStreamer):
    """本地 Silero VAD 分段，逐段云端转写（backend 恒为 "cloud-vad"）。

    单段云端失败不会中断会话：该段被跳过并计入 failed_segment_count，
    authoritative_text 只包含成功片段。复用基类的分段/确认/partial 骨架，
    仅替换解码器与 backend 标签。
    """

    def __init__(
        self,
        config: Any,
        cloud: Any,
        *,
        vad_model_dir: Path | None = None,
        model_resolver: Any = None,
        vad_factory: Any = None,
        numpy_module: Any | None = None,
    ) -> None:
        super().__init__(
            config,
            cloud,
            vad_model_dir=vad_model_dir,
            model_resolver=model_resolver,
            vad_factory=vad_factory,
            numpy_module=numpy_module,
        )
        self.failed_segment_count = 0
        self.last_error: Exception | None = None

    def _decode_samples(self, samples: Any) -> str:
        np = self._numpy()
        values = np.asarray(samples, dtype=np.float32)
        if values.size == 0:
            return ""
        try:
            return self.sensevoice.transcribe_samples(values).strip()
        except Exception as exc:  # noqa: BLE001 - 单段失败跳过，不中断会话
            self.failed_segment_count += 1
            self.last_error = exc
            return ""

    def _transcript(self, *, partial: str, final: bool) -> RecognitionTranscript:
        authoritative = "".join(self._confirmed)
        return RecognitionTranscript(
            confirmed_segments=tuple(self._confirmed),
            partial_text=partial,
            authoritative_text=authoritative,
            is_final=final,
            backend="cloud-vad",
        )
