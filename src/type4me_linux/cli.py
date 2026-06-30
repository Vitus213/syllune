from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import Config, load_config
from .daemon import serve
from .doctor import run_checks
from .inject import TextInjector
from .pipeline import VoiceInputPipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="type4me-linux")
    parser.add_argument("--config", help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check runtime dependencies and model files")
    doctor.add_argument(
        "--allow-missing-models",
        action="store_true",
        help="Return success when commands exist but model files are not downloaded yet",
    )
    doctor.set_defaults(func=_doctor)

    transcribe = sub.add_parser("transcribe", help="Transcribe a wav file")
    transcribe.add_argument("wav", type=Path)
    transcribe.add_argument(
        "--backend",
        choices=["fake", "sensevoice", "qwen3-sherpa", "qwen3-asr", "hybrid"],
    )
    transcribe.add_argument("--inject", action="store_true", help="Inject recognized text")
    transcribe.add_argument("--json", action="store_true", help="Print structured JSON")
    transcribe.set_defaults(func=_transcribe)

    inject = sub.add_parser("inject", help="Inject text into the active Wayland client")
    inject.add_argument("text")
    inject.set_defaults(func=_inject)

    record = sub.add_parser("record", help="Record fixed-duration audio, transcribe, and inject")
    record.add_argument("--seconds", type=float, default=5.0)
    record.add_argument(
        "--backend",
        choices=["fake", "sensevoice", "qwen3-sherpa", "qwen3-asr", "hybrid"],
    )
    record.add_argument("--no-inject", action="store_true")
    record.set_defaults(func=_record)

    daemon = sub.add_parser("daemon", help="Run local HTTP daemon")
    daemon.set_defaults(func=_daemon)

    args = parser.parse_args(argv)
    config = load_config(args.config)
    return args.func(args, config)


def _with_backend(config: Config, backend: str | None) -> Config:
    if not backend:
        return config
    return replace(config, asr=replace(config.asr, backend=backend))


def _doctor(args: argparse.Namespace, config: Config) -> int:
    checks = run_checks(config)
    for check in checks:
        marker = "ok" if check.ok else "missing"
        print(f"{marker:7} {check.name}: {check.detail}")
    if args.allow_missing_models:
        command_checks = [
            check
            for check in checks
            if not check.name.startswith("sensevoice ")
            and not check.name.startswith("qwen3-asr ")
        ]
        return 0 if all(check.ok for check in command_checks) else 1
    return 0 if all(check.ok for check in checks) else 1


def _transcribe(args: argparse.Namespace, config: Config) -> int:
    config = _with_backend(config, args.backend)
    result = VoiceInputPipeline(config).run_once(audio_path=args.wav, inject=args.inject)
    if args.json:
        print(
            json.dumps(
                {
                    "text": result.recognition.text,
                    "backend": result.recognition.backend,
                    "draft_text": result.recognition.draft_text,
                    "injection": None
                    if result.injection is None
                    else {
                        "ok": result.injection.ok,
                        "method": result.injection.method,
                        "message": result.injection.message,
                    },
                },
                ensure_ascii=False,
            )
        )
    else:
        print(result.recognition.text)
    return 0


def _inject(args: argparse.Namespace, config: Config) -> int:
    result = TextInjector(config.inject).inject(args.text)
    if not result.ok:
        print(result.message, file=sys.stderr)
        return 1
    return 0


def _record(args: argparse.Namespace, config: Config) -> int:
    config = _with_backend(config, args.backend)
    result = VoiceInputPipeline(config).run_once(
        record_seconds=args.seconds,
        inject=not args.no_inject,
    )
    print(result.recognition.text)
    return 0 if result.injection is None or result.injection.ok else 1


def _daemon(args: argparse.Namespace, config: Config) -> int:
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
