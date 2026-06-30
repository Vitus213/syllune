from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from type4me_linux.config import ASRConfig, Config, DaemonConfig
from type4me_linux.daemon import make_server
from type4me_linux.inject import InjectionResult
from type4me_linux.providers import RecognitionResult


class _Provider:
    def transcribe(self, wav_path: Path) -> RecognitionResult:
        return RecognitionResult(text=f"transcribed {wav_path.name}", backend="test")


class _Injector:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def inject(self, text: str) -> InjectionResult:
        self.texts.append(text)
        return InjectionResult(method="test", ok=True, message="typed")


def _json_request(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_daemon_handles_health_inject_and_transcribe(tmp_path: Path) -> None:
    injector = _Injector()
    config = Config(asr=ASRConfig(backend="fake"), daemon=DaemonConfig(host="127.0.0.1", port=0))
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
            "message": "typed",
        }
        assert injector.texts == ["你好"]

        wav_path = tmp_path / "sample.wav"
        wav_path.write_bytes(b"RIFF")
        assert _json_request(f"{base}/transcribe", {"path": str(wav_path)}) == {
            "text": "transcribed sample.wav",
            "backend": "test",
            "draft_text": None,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
