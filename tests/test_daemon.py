from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import type4me_linux.daemon as daemon_module

from type4me_linux.config import ASRConfig, Config, DaemonConfig
from type4me_linux.daemon import make_server
from type4me_linux.inject import InjectionResult
from type4me_linux.providers import RecognitionResult


class _Provider:
    def transcribe(self, wav_path: Path) -> RecognitionResult:
        return RecognitionResult(text=f"已转写 {wav_path.name}", backend="test")


class _Injector:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def inject(self, text: str) -> InjectionResult:
        self.texts.append(text)
        return InjectionResult(method="test", ok=True, message="已注入")


def _json_request(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return json.loads(response.read().decode("utf-8"))


def _raw_json_request(
    url: str,
    *,
    data: bytes,
    method: str = "POST",
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_daemon_preserves_health_inject_and_transcribe_keys(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))
    injector = _Injector()
    config = Config(
        asr=ASRConfig(batch_backend="fake"),
        daemon=DaemonConfig(host="127.0.0.1", port=0),
    )
    server = make_server(config, provider=_Provider(), injector=injector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"

        assert _json_request(f"{base}/health") == {"ok": True}
        assert _json_request(f"{base}/inject", {"text": "你好"}) == {
            "ok": True,
            "method": "test",
            "message": "已注入",
        }
        assert injector.texts == ["你好"]

        wav_path = tmp_path / "sample.wav"
        wav_path.write_bytes(b"RIFF")
        assert _json_request(f"{base}/transcribe", {"path": str(wav_path)}) == {
            "text": "已转写 sample.wav",
            "backend": "test",
            "draft_text": None,
        }
        assert _json_request(f"{base}/missing") == {
            "ok": False,
            "message": "找不到请求的接口",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_daemon_rejects_malformed_and_empty_transcribe_payloads(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))
    injector = _Injector()
    config = Config(
        asr=ASRConfig(batch_backend="fake"),
        daemon=DaemonConfig(host="127.0.0.1", port=0),
    )
    server = make_server(config, provider=_Provider(), injector=injector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"

        status, payload = _raw_json_request(f"{base}/transcribe", data=b"")
        assert status == 400
        assert payload["ok"] is False
        assert "'path'" in str(payload["message"])

        for raw, detail in (
            (b"{", "请求内容无效"),
            (b"[]", "JSON 顶层必须是对象"),
            (b"\xff", "请求内容无效"),
        ):
            status, payload = _raw_json_request(f"{base}/inject", data=raw)
            assert status == 400
            assert payload["ok"] is False
            assert detail in str(payload["message"])

        status, payload = _raw_json_request(f"{base}/inject", data=b"")
        assert (status, payload) == (
            200,
            {"ok": True, "method": "test", "message": "已注入"},
        )
        assert injector.texts == [""]

        status, payload = _raw_json_request(f"{base}/missing", data=b"{}")
        assert status == 404
        assert payload == {"ok": False, "message": "找不到请求的接口"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_daemon_converts_pipeline_failure_to_500(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))
    config = Config(
        asr=ASRConfig(batch_backend="fake"),
        daemon=DaemonConfig(host="127.0.0.1", port=0),
    )
    server = make_server(config, provider=_Provider(), injector=_Injector())
    monkeypatch.setattr(
        server.pipeline,
        "run_once",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("识别器离线")),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        status, payload = _raw_json_request(
            f"http://{host}:{port}/transcribe",
            data=json.dumps({"path": str(tmp_path / "audio.wav")}).encode("utf-8"),
        )
        assert status == 500
        assert payload == {"ok": False, "message": "请求处理失败：识别器离线"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_make_server_forwards_address_and_dependencies(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = _Provider()
    injector = _Injector()
    config = Config(daemon=DaemonConfig(host="127.0.0.9", port=4321))
    captured: list[tuple[object, ...]] = []
    sentinel = object()

    def fake_server(address, received_config, *, provider, injector):  # type: ignore[no-untyped-def]
        captured.append((address, received_config, provider, injector))
        return sentinel

    monkeypatch.setattr(daemon_module, "VoiceInputServer", fake_server)

    assert daemon_module.make_server(config, provider=provider, injector=injector) is sentinel
    assert captured == [(("127.0.0.9", 4321), config, provider, injector)]


def test_serve_always_closes_server_after_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    class _Server:
        def serve_forever(self) -> None:
            calls.append("serve")
            raise RuntimeError("监听失败")

        def server_close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(daemon_module, "make_server", lambda config: _Server())

    with pytest.raises(RuntimeError, match="监听失败"):
        daemon_module.serve(Config())
    assert calls == ["serve", "close"]
