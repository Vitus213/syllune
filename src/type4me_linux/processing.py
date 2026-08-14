from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from concurrent.futures import CancelledError as FutureCancelledError
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .modes import Mode, render_template

ProcessingStatus = Literal[
    "bypassed",
    "success",
    "missing-secret",
    "http-error",
    "malformed-response",
    "timeout",
]
ProviderKind = Literal["none", "openai-compatible", "ollama"]
CancellationCheck = Callable[[], bool | None]


class ProcessingCancelled(Exception):
    """调用方明确取消了文本处理。"""


@dataclass(frozen=True, slots=True)
class TextProcessRequest:
    text: str
    mode: Mode
    selected: str = ""
    clipboard: str = ""
    cancelled: CancellationCheck | None = None

    def rendered_prompt(self) -> str:
        return render_template(
            self.mode.prompt,
            text=self.text,
            selected=self.selected,
            clipboard=self.clipboard,
        )


@dataclass(frozen=True, slots=True)
class TextProcessResult:
    text: str
    status: ProcessingStatus
    provider: ProviderKind
    warning: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {"bypassed", "success"}

    @property
    def final_text(self) -> str:
        return self.text


@runtime_checkable
class TextProcessor(Protocol):
    def process(self, request: TextProcessRequest) -> TextProcessResult: ...


@runtime_checkable
class HttpClient(Protocol):
    def post(
        self,
        *,
        url: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any: ...


class UrllibHttpClient:
    """使用标准库发送 JSON 请求的最小 HTTP 客户端。"""

    def post(
        self,
        *,
        url: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=dict(headers), method="POST")
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read()


class OpenAICompatibleProcessor:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        api_key_env: str = "",
        timeout_seconds: float = 30.0,
        http_client: HttpClient | Callable[..., Any] | None = None,
        environment: Mapping[str, str] | Callable[[str], str | None] | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client or UrllibHttpClient()
        self._environment = os.environ if environment is None else environment
        self._cancelled = cancelled

    def process(self, request: TextProcessRequest) -> TextProcessResult:
        return _process_chat(
            request=request,
            provider="openai-compatible",
            endpoint=f"{self.base_url}/chat/completions",
            model=self.model,
            api_key=self.api_key,
            api_key_env=self.api_key_env,
            timeout_seconds=self.timeout_seconds,
            http_client=self._http_client,
            environment=self._environment,
            processor_cancelled=self._cancelled,
            response_text=_openai_response_text,
            extra_payload={},
        )


class OllamaProcessor:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        api_key_env: str = "",
        timeout_seconds: float = 30.0,
        http_client: HttpClient | Callable[..., Any] | None = None,
        environment: Mapping[str, str] | Callable[[str], str | None] | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client or UrllibHttpClient()
        self._environment = os.environ if environment is None else environment
        self._cancelled = cancelled

    def process(self, request: TextProcessRequest) -> TextProcessResult:
        return _process_chat(
            request=request,
            provider="ollama",
            endpoint=f"{self.base_url}/api/chat",
            model=self.model,
            api_key=self.api_key,
            api_key_env=self.api_key_env,
            timeout_seconds=self.timeout_seconds,
            http_client=self._http_client,
            environment=self._environment,
            processor_cancelled=self._cancelled,
            response_text=_ollama_response_text,
            extra_payload={"stream": False},
        )


class _MalformedResponse(ValueError):
    pass


def _process_chat(
    *,
    request: TextProcessRequest,
    provider: Literal["openai-compatible", "ollama"],
    endpoint: str,
    model: str,
    api_key: str,
    api_key_env: str,
    timeout_seconds: float,
    http_client: HttpClient | Callable[..., Any],
    environment: Mapping[str, str] | Callable[[str], str | None],
    processor_cancelled: CancellationCheck | None,
    response_text: Callable[[Any], str],
    extra_payload: Mapping[str, object],
) -> TextProcessResult:
    _raise_if_cancelled(processor_cancelled, request.cancelled)
    if request.mode.id == "quick":
        return TextProcessResult(request.text, "bypassed", "none")

    secret = api_key.strip() or (_environment_value(environment, api_key_env) if api_key_env else None)
    if not secret and (api_key_env or not api_key):
        hint = f"环境变量 {api_key_env} 未设置" if api_key_env else "配置项 processing.api_key 为空"
        return _fallback(
            request,
            provider,
            "missing-secret",
            f"{hint}，已保留原始文本。",
        )

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": request.rendered_prompt()}],
        **extra_payload,
    }
    try:
        raw = _http_post(
            http_client,
            url=endpoint,
            payload=payload,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        _raise_if_cancelled(processor_cancelled, request.cancelled)
        result = response_text(_decode_response(raw))
        _raise_if_cancelled(processor_cancelled, request.cancelled)
    except (ProcessingCancelled, FutureCancelledError):
        raise
    except (TimeoutError, socket.timeout) as exc:
        return _fallback(
            request,
            provider,
            "timeout",
            f"文本处理请求超时，已保留原始文本：{exc}",
        )
    except _MalformedResponse as exc:
        return _fallback(
            request,
            provider,
            "malformed-response",
            f"文本处理响应格式无效，已保留原始文本：{exc}",
        )
    except urllib.error.HTTPError as exc:
        return _fallback(
            request,
            provider,
            "http-error",
            f"文本处理请求失败，已保留原始文本：{exc}",
        )
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
        status = "timeout" if timed_out else "http-error"
        kind = "超时" if timed_out else "失败"
        return _fallback(
            request,
            provider,
            status,
            f"文本处理请求{kind}，已保留原始文本：{exc}",
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _fallback(
            request,
            provider,
            "malformed-response",
            f"文本处理响应格式无效，已保留原始文本：{exc}",
        )
    except Exception as exc:
        return _fallback(
            request,
            provider,
            "http-error",
            f"文本处理请求失败，已保留原始文本：{exc}",
        )
    return TextProcessResult(result, "success", provider)


def _fallback(
    request: TextProcessRequest,
    provider: Literal["openai-compatible", "ollama"],
    status: Literal["missing-secret", "http-error", "malformed-response", "timeout"],
    warning: str,
) -> TextProcessResult:
    return TextProcessResult(request.text, status, provider, warning)


def _raise_if_cancelled(*checks: CancellationCheck | None) -> None:
    for check in checks:
        if check is not None and check():
            raise ProcessingCancelled("文本处理已取消")


def _environment_value(
    environment: Mapping[str, str] | Callable[[str], str | None], name: str
) -> str | None:
    if callable(environment):
        return environment(name)
    return environment.get(name)


def _http_post(client: HttpClient | Callable[..., Any], **kwargs: Any) -> Any:
    post = getattr(client, "post", None)
    if post is not None:
        return post(**kwargs)
    if callable(client):
        return client(**kwargs)
    raise TypeError("HTTP 客户端必须可调用或提供 post 方法")


def _decode_response(raw: Any) -> Any:
    if isinstance(raw, Mapping):
        return raw
    json_method = getattr(raw, "json", None)
    if callable(json_method):
        return json_method()
    if hasattr(raw, "read"):
        raw = raw.read()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _MalformedResponse("响应不是 UTF-8") from exc
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _MalformedResponse("响应不是有效 JSON") from exc
    raise _MalformedResponse("响应不是 JSON 对象")


def _openai_response_text(response: Any) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _MalformedResponse("缺少 choices[0].message.content") from exc
    return _nonempty_content(content)


def _ollama_response_text(response: Any) -> str:
    try:
        content = response["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise _MalformedResponse("缺少 message.content") from exc
    return _nonempty_content(content)


def _nonempty_content(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        raise _MalformedResponse("处理结果必须是非空字符串")
    return content
