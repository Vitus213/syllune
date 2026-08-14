from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

import numpy as np
import pytest

from type4me_linux.cloud_asr import (
    CloudASRAuthenticationError,
    CloudASRClient,
    CloudASRProvider,
    CloudASRRequestError,
    CloudASRResponseError,
    CloudVadStreamer,
    SYSTEM_TRANSCRIBE_PROMPT,
    needs_system_prompt,
)
from type4me_linux.config import ASRConfig, CloudConfig
from type4me_linux.providers import RecognitionResult, create_provider


@dataclass
class FakeResponse:
    status: int
    body: bytes

    def read(self) -> bytes:
        return self.body


def _ok_response(text: str = "你好，世界。") -> FakeResponse:
    payload = {
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": [{"text": text}], "role": "assistant"},
                }
            ]
        },
        "usage": {"audio_tokens": 10, "total_tokens": 12},
    }
    return FakeResponse(200, json.dumps(payload).encode("utf-8"))


def _make_client(
    *,
    responses: list[FakeResponse] | None = None,
    urlopen: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = lambda _: None,
    **kwargs: Any,
) -> CloudASRClient:
    return CloudASRClient(
        base_url="https://dashscope.example.com",
        api_key="test-key",
        model="qwen3-asr-flash-2026-02-10",
        timeout_seconds=5.0,
        urlopen=urlopen,
        sleep=sleep,
        **kwargs,
    )


def test_transcribe_sends_correct_request_shape() -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _ok_response()

    client = _make_client(urlopen=fake_urlopen)
    text = client.transcribe_audio(b"\x00\x01\x02\x03", prompt=None)

    assert text == "你好，世界。"
    assert captured["url"].endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert captured["method"] == "POST"
    assert captured["timeout"] == 5.0
    header_map = {k.lower(): v for k, v in captured["headers"].items()}
    assert header_map["authorization"] == "Bearer test-key"
    assert header_map["content-type"] == "application/json"
    body = captured["body"]
    assert body["model"] == "qwen3-asr-flash-2026-02-10"
    content = body["input"]["messages"][0]["content"]
    assert content[0]["audio"].startswith("data:audio/wav;base64,")
    assert len(content) == 1


def test_transcribe_with_prompt_appends_text_item() -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _ok_response()

    client = _make_client(urlopen=fake_urlopen)
    client.transcribe_audio(b"data", prompt="请转写")

    content = captured["body"]["input"]["messages"][0]["content"]
    assert content[1] == {"text": "请转写"}


def test_transcribe_with_system_prompt_prepends_system_message() -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _ok_response()

    client = _make_client(urlopen=fake_urlopen)
    client.transcribe_audio(b"data", prompt="请转写", system_prompt=SYSTEM_TRANSCRIBE_PROMPT)

    messages = captured["body"]["input"]["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM_TRANSCRIBE_PROMPT}
    assert messages[1]["role"] == "user"


def test_transcribe_parses_annotations_and_returns_text() -> None:
    assert (
        _make_client(urlopen=lambda *args, **kwargs: _ok_response()).transcribe_audio(b"x")
        == "你好，世界。"
    )


def test_empty_text_raises_response_error() -> None:
    client = _make_client(urlopen=lambda *args, **kwargs: _ok_response("   "))
    with pytest.raises(CloudASRResponseError, match="转写文本为空"):
        client.transcribe_audio(b"x")


def test_malformed_body_raises_response_error() -> None:
    client = _make_client(urlopen=lambda *args, **kwargs: FakeResponse(200, b"not json"))
    with pytest.raises(CloudASRResponseError):
        client.transcribe_audio(b"x")


def test_missing_choices_raises_response_error() -> None:
    body = json.dumps({"output": {"choices": []}}).encode()
    client = _make_client(urlopen=lambda *args, **kwargs: FakeResponse(200, body))
    with pytest.raises(CloudASRResponseError):
        client.transcribe_audio(b"x")


def test_authentication_status_fails_without_retry() -> None:
    calls: list[int] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(1)
        return FakeResponse(401, b'{"code":"Unauthorized"}')

    client = _make_client(urlopen=fake_urlopen)
    with pytest.raises(CloudASRAuthenticationError):
        client.transcribe_audio(b"x")
    assert len(calls) == 1


def test_retryable_status_retries_then_succeeds() -> None:
    calls: list[int] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(1)
        if len(calls) < 3:
            return FakeResponse(500, b"internal")
        return _ok_response("重试成功")

    client = _make_client(urlopen=fake_urlopen)
    assert client.transcribe_audio(b"x") == "重试成功"
    assert len(calls) == 3


def test_retry_exhausted_raises_request_error() -> None:
    calls: list[int] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(1)
        return FakeResponse(503, b"unavailable")

    client = _make_client(urlopen=fake_urlopen, max_attempts=3)
    with pytest.raises(CloudASRRequestError, match="503"):
        client.transcribe_audio(b"x")
    assert len(calls) == 3


def test_rate_limit_backs_off() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(1)
        if len(calls) == 1:
            return FakeResponse(429, b"rate limited")
        return _ok_response("成功")

    client = _make_client(urlopen=fake_urlopen, sleep=fake_sleep)
    assert client.transcribe_audio(b"x") == "成功"
    assert len(calls) == 2
    assert sleeps and sleeps[0] > 0


def test_network_error_retries_then_raises() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(1)
        raise URLError("connection refused")

    client = _make_client(urlopen=fake_urlopen, sleep=lambda s: sleeps.append(s), max_attempts=2)
    with pytest.raises(CloudASRRequestError, match="connection refused"):
        client.transcribe_audio(b"x")
    assert len(calls) == 2
    assert sleeps


def test_http_error_raised_as_urlopen_wrapped() -> None:
    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        raise HTTPError("https://x", 502, "bad gateway", {}, None)

    client = _make_client(urlopen=fake_urlopen, max_attempts=1)
    with pytest.raises(CloudASRRequestError, match="502"):
        client.transcribe_audio(b"x")


def test_plain_four_xx_fails_fast() -> None:
    calls: list[int] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(1)
        return FakeResponse(400, b'{"code":"BadRequest"}')

    client = _make_client(urlopen=fake_urlopen)
    with pytest.raises(CloudASRRequestError, match="400"):
        client.transcribe_audio(b"x")
    assert len(calls) == 1


def test_needs_system_prompt_only_for_omni_models() -> None:
    assert needs_system_prompt("qwen3-asr-flash-2026-02-10") is False
    assert needs_system_prompt("qwen3-omni-flash") is True
    assert needs_system_prompt("qwen3.5-omni-flash") is True


def test_missing_api_key_env_raises_authentication_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TYPE4ME_CLOUD_TEST_KEY", raising=False)
    from type4me_linux.cloud_asr import resolve_api_key

    with pytest.raises(CloudASRAuthenticationError, match="TYPE4ME_CLOUD_TEST_KEY"):
        resolve_api_key("TYPE4ME_CLOUD_TEST_KEY")


def test_blank_api_key_env_raises_authentication_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TYPE4ME_CLOUD_TEST_KEY", "   ")
    from type4me_linux.cloud_asr import resolve_api_key

    with pytest.raises(CloudASRAuthenticationError):
        resolve_api_key("TYPE4ME_CLOUD_TEST_KEY")


# ---------------------------------------------------------------- provider


class _ScriptedClient:
    def __init__(self, text: str = "云端转写文本") -> None:
        self.text = text
        self.calls: list[dict] = []

    def transcribe_audio(
        self,
        wav_bytes: bytes,
        *,
        prompt: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        self.calls.append({"wav": wav_bytes, "prompt": prompt, "system": system_prompt})
        return self.text


def _provider(
    client: _ScriptedClient,
    *,
    model: str = "qwen3-asr-flash-2026-02-10",
) -> CloudASRProvider:
    return CloudASRProvider(
        CloudConfig(model=model, api_key_env="TYPE4ME_CLOUD_TEST_KEY"),
        client_factory=lambda **_: client,
    )


def test_provider_batch_transcribe_reads_wav_and_marks_backend(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TYPE4ME_CLOUD_TEST_KEY", "secret")
    wav = tmp_path / "clip.wav"
    import wave

    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)

    client = _ScriptedClient("你好，世界。")
    provider = _provider(client)
    result = provider.transcribe(wav)

    assert isinstance(result, RecognitionResult)
    assert result.backend == "cloud"
    assert result.text == "你好，世界。"
    assert client.calls[0]["wav"] == b"\x00\x00" * 1600
    assert client.calls[0]["prompt"] is None
    assert client.calls[0]["system"] is None


def test_provider_transcribe_samples_encodes_pcm16(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TYPE4ME_CLOUD_TEST_KEY", "secret")
    import numpy as np

    samples = np.zeros(320, dtype=np.float32)
    samples[0] = 0.5
    client = _ScriptedClient("样本转写")
    provider = _provider(client)

    assert provider.transcribe_samples(samples) == "样本转写"
    pcm = client.calls[0]["wav"]
    assert pcm[:2] == int(0.5 * 32768).to_bytes(2, "little", signed=True)


def test_provider_omni_model_sends_transcribe_persona(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TYPE4ME_CLOUD_TEST_KEY", "secret")
    import numpy as np

    client = _ScriptedClient()
    provider = _provider(client, model="qwen3.5-omni-flash")
    provider.transcribe_samples(np.zeros(320, dtype=np.float32))

    assert client.calls[0]["prompt"] == "请转写"
    assert client.calls[0]["system"] == SYSTEM_TRANSCRIBE_PROMPT


def test_provider_missing_key_raises_before_request(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TYPE4ME_CLOUD_TEST_KEY", raising=False)
    import numpy as np

    client = _ScriptedClient()
    provider = _provider(client)
    with pytest.raises(CloudASRAuthenticationError):
        provider.transcribe_samples(np.zeros(320, dtype=np.float32))
    assert client.calls == []


def test_provider_wav_sample_rate_mismatch_rejected(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TYPE4ME_CLOUD_TEST_KEY", "secret")
    import wave

    wav = tmp_path / "bad.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 1600)

    provider = _provider(_ScriptedClient())
    with pytest.raises(ValueError, match="采样率必须为 16000"):
        provider.transcribe(wav)


def test_create_provider_cloud_branch(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TYPE4ME_CLOUD_TEST_KEY", "secret")
    config = ASRConfig(batch_backend="cloud")
    provider = create_provider(config, cloud=CloudConfig())
    assert isinstance(provider, CloudASRProvider)


def test_create_provider_cloud_without_config_raises() -> None:
    from type4me_linux.providers import create_provider

    config = ASRConfig(batch_backend="cloud")
    with pytest.raises(ValueError, match="cloud"):
        create_provider(config)


# ---------------------------------------------------------------- streamer


class _Segment:
    def __init__(self, samples: np.ndarray) -> None:
        self.samples = samples


class _Vad:
    def __init__(self) -> None:
        self.windows: list[np.ndarray] = []
        self.active: list[np.ndarray] = []
        self.completed: list[_Segment] = []

    def accept_waveform(self, samples: np.ndarray) -> None:
        assert samples.dtype == np.float32
        assert samples.size == 512
        self.windows.append(samples.copy())
        self.active.append(samples.copy())

    @property
    def is_speech_detected(self) -> bool:
        return bool(self.active)

    @property
    def current_segment(self) -> _Segment:
        values = np.concatenate(self.active) if self.active else np.empty(0, dtype=np.float32)
        return _Segment(values)

    def complete(self) -> None:
        if self.active:
            self.completed.append(self.current_segment)
            self.active.clear()

    def empty(self) -> bool:
        return not self.completed

    @property
    def front(self) -> _Segment:
        return self.completed[0]

    def pop(self) -> None:
        self.completed.pop(0)

    def flush(self) -> None:
        self.complete()


class _CloudDecoder:
    def __init__(self, *, fail_on: str = "") -> None:
        self.inputs: list[np.ndarray] = []
        self.fail_on = fail_on

    def transcribe_samples(self, samples: np.ndarray) -> str:
        self.inputs.append(samples.copy())
        if float(samples.max(initial=0.0)) > 0.04:
            if self.fail_on == "乙":
                raise CloudASRRequestError("模拟失败")
            return "乙"
        return "甲"


def _pcm(value: int, samples: int = 3_200) -> bytes:
    return np.full(samples, value, dtype="<i2").tobytes()


def _make_cloud_streamer(
    decoder: _CloudDecoder,
    tmp_path: Path,
    vad: _Vad | None = None,
) -> CloudVadStreamer:
    vad_model = tmp_path / "silero_vad.onnx"
    vad_model.write_bytes(b"model")
    vad = vad or _Vad()
    return CloudVadStreamer(
        ASRConfig(),
        decoder,  # type: ignore[arg-type]
        vad_model_dir=vad_model,
        vad_factory=lambda **_kwargs: vad,
    )


def test_cloud_vad_emits_partial_confirmed_and_final_with_cloud_backend(
    tmp_path: Path,
) -> None:
    vad = _Vad()
    decoder = _CloudDecoder()
    streamer = _make_cloud_streamer(decoder, tmp_path, vad)

    first = streamer.accept_chunk(_pcm(1_000))
    vad.complete()
    confirmed = streamer.accept_chunk(b"")
    second = streamer.accept_chunk(_pcm(2_000))
    final = streamer.flush()

    assert [item.partial_text for item in first] == ["甲"]
    assert [(item.confirmed_segments, item.partial_text) for item in confirmed] == [(("甲",), "")]
    assert [(item.confirmed_segments, item.partial_text) for item in second] == [(("甲",), "乙")]
    assert final.confirmed_segments == ("甲", "乙")
    assert final.authoritative_text == "甲乙"
    assert final.is_final is True
    assert final.backend == "cloud-vad"
    assert streamer.flush() is final
    assert streamer.failed_segment_count == 0
    assert len(decoder.inputs) >= 2


def test_cloud_vad_skips_failed_segment_and_counts(tmp_path: Path) -> None:
    vad = _Vad()
    decoder = _CloudDecoder(fail_on="乙")
    streamer = _make_cloud_streamer(decoder, tmp_path, vad)

    streamer.accept_chunk(_pcm(1_000))
    vad.complete()
    streamer.accept_chunk(b"")  # 确认段落 "甲"
    streamer.accept_chunk(_pcm(2_000))  # partial "乙" -> 失败跳过
    vad.complete()
    final = streamer.flush()

    assert streamer.failed_segment_count >= 1
    assert streamer.last_error is not None
    assert final.confirmed_segments == ("甲",)
    assert final.authoritative_text == "甲"
    assert final.backend == "cloud-vad"


def test_cloud_vad_requires_vad_model_file(tmp_path: Path) -> None:
    decoder = _CloudDecoder()
    with pytest.raises(FileNotFoundError, match="Silero VAD 模型文件缺失"):
        CloudVadStreamer(
            ASRConfig(),
            decoder,  # type: ignore[arg-type]
            vad_model_dir=tmp_path / "missing",
        )
