from __future__ import annotations

import urllib.error
from concurrent.futures import CancelledError as FutureCancelledError
from typing import Any

import pytest

from type4me_linux.modes import BUILTIN_MODES, Mode
from type4me_linux.processing import (
    OllamaProcessor,
    OpenAICompatibleProcessor,
    ProcessingCancelled,
    TextProcessRequest,
)


class RecordingHttpClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _mode(prompt: str = "处理{text}，选择{selected}，剪贴板{clipboard}") -> Mode:
    return Mode(
        id="11111111-1111-4111-8111-111111111111",
        name="测试模式",
        prompt=prompt,
        processing_label="处理中",
        builtin=False,
        sort_order=100,
    )


def test_openai_payload_endpoint_and_runtime_secret_resolution() -> None:
    environment: dict[str, str] = {}
    client = RecordingHttpClient({"choices": [{"message": {"content": "第一次结果"}}]})
    processor = OpenAICompatibleProcessor(
        base_url="https://api.example.test/v1/",
        model="test-model",
        api_key_env="TYPE4ME_TEST_KEY",
        timeout_seconds=7.5,
        http_client=client,
        environment=environment,
    )
    request = TextProcessRequest(
        text="正文含{clipboard}",
        selected="选中含{text}",
        clipboard="剪贴板含{selected}",
        mode=_mode(),
    )

    missing = processor.process(request)
    assert missing.text == request.text
    assert missing.status == "missing-secret"
    assert missing.warning and "TYPE4ME_TEST_KEY" in missing.warning
    assert client.calls == []

    environment["TYPE4ME_TEST_KEY"] = "first-secret"
    first = processor.process(request)
    assert first.text == "第一次结果"
    assert first.status == "success"
    assert client.calls[0] == {
        "url": "https://api.example.test/v1/chat/completions",
        "payload": {
            "model": "test-model",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "处理正文含{clipboard}，选择选中含{text}，剪贴板剪贴板含{selected}"
                    ),
                }
            ],
        },
        "headers": {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer first-secret",
        },
        "timeout_seconds": 7.5,
    }

    environment["TYPE4ME_TEST_KEY"] = "second-secret"
    client.response = '{"choices":[{"message":{"content":"第二次结果"}}]}'.encode()
    second = processor.process(request)
    assert second.text == "第二次结果"
    assert client.calls[1]["headers"]["Authorization"] == "Bearer second-secret"
    assert "first-secret" not in repr(processor)
    assert "second-secret" not in repr(processor)


def test_ollama_payload_and_endpoint_without_required_secret() -> None:
    client = RecordingHttpClient({"message": {"content": "Ollama 结果"}})
    processor = OllamaProcessor(
        base_url="http://127.0.0.1:11434/",
        model="qwen3:8b",
        http_client=client,
        environment={},
    )

    result = processor.process(TextProcessRequest(text="原始文本", mode=_mode("{text}")))

    assert result.text == "Ollama 结果"
    assert result.provider == "ollama"
    assert client.calls == [
        {
            "url": "http://127.0.0.1:11434/api/chat",
            "payload": {
                "model": "qwen3:8b",
                "messages": [{"role": "user", "content": "原始文本"}],
                "stream": False,
            },
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            "timeout_seconds": 30.0,
        }
    ]


def test_quick_mode_bypasses_provider_environment_and_http() -> None:
    client = RecordingHttpClient(AssertionError("不应发送请求"))

    def forbidden_environment(name: str) -> str | None:
        raise AssertionError("不应读取密钥")

    processor = OpenAICompatibleProcessor(
        base_url="https://example.test/v1",
        model="unused",
        api_key_env="SECRET",
        http_client=client,
        environment=forbidden_environment,
    )
    quick = BUILTIN_MODES[0]

    result = processor.process(TextProcessRequest(text="片段替换后的原文", mode=quick))

    assert result.text == "片段替换后的原文"
    assert result.status == "bypassed"
    assert result.provider == "none"
    assert result.warning is None
    assert client.calls == []


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (TimeoutError("超时"), "timeout"),
        (urllib.error.URLError(TimeoutError("连接超时")), "timeout"),
        (OSError("连接失败"), "http-error"),
        ({"choices": []}, "malformed-response"),
        (b"not-json", "malformed-response"),
        ({"choices": [{"message": {"content": ""}}]}, "malformed-response"),
    ],
)
def test_openai_failures_preserve_raw_snippet_corrected_text(
    response: Any, expected_status: str
) -> None:
    processor = OpenAICompatibleProcessor(
        base_url="https://example.test/v1",
        model="model",
        http_client=RecordingHttpClient(response),
        environment={},
    )
    request = TextProcessRequest(text="me@example.com", mode=_mode("{text}"))

    result = processor.process(request)

    assert result.text == "me@example.com"
    assert result.status == expected_status
    assert result.succeeded is False
    assert result.warning and "已保留原始文本" in result.warning


def test_ollama_malformed_response_preserves_input() -> None:
    processor = OllamaProcessor(
        base_url="http://localhost:11434",
        model="model",
        http_client=RecordingHttpClient({"response": "错误字段"}),
    )

    result = processor.process(TextProcessRequest(text="保留我", mode=_mode()))

    assert result.text == "保留我"
    assert result.status == "malformed-response"


def test_cancellation_before_and_after_http_propagates() -> None:
    before = OpenAICompatibleProcessor(
        base_url="https://example.test/v1",
        model="model",
        http_client=RecordingHttpClient({"choices": [{"message": {"content": "不应到达"}}]}),
    )
    with pytest.raises(ProcessingCancelled, match="文本处理已取消"):
        before.process(TextProcessRequest(text="原文", mode=_mode(), cancelled=lambda: True))

    state = {"cancelled": False}

    class CancellingClient:
        def post(self, **kwargs: Any) -> Any:
            state["cancelled"] = True
            return {"choices": [{"message": {"content": "不应返回"}}]}

    after = OpenAICompatibleProcessor(
        base_url="https://example.test/v1",
        model="model",
        http_client=CancellingClient(),
    )
    with pytest.raises(ProcessingCancelled, match="文本处理已取消"):
        after.process(
            TextProcessRequest(
                text="原文",
                mode=_mode(),
                cancelled=lambda: state["cancelled"],
            )
        )


def test_transport_cancellation_exception_is_never_converted_to_fallback() -> None:
    processor = OllamaProcessor(
        base_url="http://localhost:11434",
        model="model",
        http_client=RecordingHttpClient(ProcessingCancelled("调用方取消")),
    )

    with pytest.raises(ProcessingCancelled, match="调用方取消"):
        processor.process(TextProcessRequest(text="原文", mode=_mode()))


def test_standard_future_cancellation_is_never_converted_to_fallback() -> None:
    processor = OllamaProcessor(
        base_url="http://localhost:11434",
        model="model",
        http_client=RecordingHttpClient(FutureCancelledError()),
    )

    with pytest.raises(FutureCancelledError):
        processor.process(TextProcessRequest(text="原文", mode=_mode()))
