from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .inject import TextInjector
from .pipeline import VoiceInputPipeline
from .providers import ASRProvider


class VoiceInputServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        config: Config,
        provider: ASRProvider | None = None,
        injector: TextInjector | None = None,
    ) -> None:
        self.config = config
        self.pipeline = VoiceInputPipeline(config, provider=provider, injector=injector)
        super().__init__(server_address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: VoiceInputServer

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json({"ok": True})
            return
        self._json({"ok": False, "message": "找不到请求的接口"}, status=404)

    def do_POST(self) -> None:
        try:
            payload = self._payload()
            if self.path == "/inject":
                text = str(payload.get("text", ""))
                result = self.server.pipeline.injector.inject(text)
                self._json(
                    {
                        "ok": result.ok,
                        "method": result.method,
                        "message": result.message,
                    }
                )
                return
            if self.path == "/transcribe":
                audio_path = Path(str(payload["path"]))
                result = self.server.pipeline.run_once(audio_path=audio_path, inject=False)
                self._json(
                    {
                        "text": result.recognition.text,
                        "backend": result.recognition.backend,
                        "draft_text": result.recognition.draft_text,
                    }
                )
                return
            self._json({"ok": False, "message": "找不到请求的接口"}, status=404)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "message": f"请求内容无效：{exc}"}, status=400)
        except Exception as exc:
            self._json({"ok": False, "message": f"请求处理失败：{exc}"}, status=500)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("JSON 顶层必须是对象")
        return value

    def _json(self, payload: dict[str, object], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(config: Config) -> None:
    server = make_server(config)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def make_server(
    config: Config,
    provider: ASRProvider | None = None,
    injector: TextInjector | None = None,
) -> VoiceInputServer:
    return VoiceInputServer(
        (config.daemon.host, config.daemon.port),
        config,
        provider=provider,
        injector=injector,
    )
