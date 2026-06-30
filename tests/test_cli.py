from __future__ import annotations

import json
from pathlib import Path

from type4me_linux.cli import main
from type4me_linux.doctor import Check
from type4me_linux.inject import InjectionResult
from type4me_linux.providers import RecognitionResult


def test_doctor_can_allow_missing_models(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.run_checks",
        lambda config: [
            Check("wtype", True, "/bin/wtype"),
            Check("sensevoice model.onnx", False, "/missing/model.onnx"),
            Check("qwen3-asr encoder.onnx", False, "/missing/encoder.onnx"),
        ],
    )

    code = main(["doctor", "--allow-missing-models"])

    output = capsys.readouterr().out
    assert code == 0
    assert "sensevoice model.onnx" in output


def test_transcribe_prints_json_with_fake_backend(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"fake")

    code = main(["transcribe", str(wav_path), "--backend", "fake", "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "text": "测试语音输入",
        "backend": "fake",
        "draft_text": None,
        "injection": None,
    }


def test_record_uses_recorder_provider_and_injector(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: dict[str, object] = {}

    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            calls["backend"] = config.asr.backend

        def run_once(self, *, record_seconds: float, inject: bool):  # type: ignore[no-untyped-def]
            calls["record_seconds"] = record_seconds
            calls["inject"] = inject
            return type(
                "Result",
                (),
                {
                    "recognition": RecognitionResult("recorded text", "test"),
                    "injection": InjectionResult("test", True),
                },
            )()

    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)

    code = main(["record", "--seconds", "1.5", "--backend", "fake"])

    assert code == 0
    assert capsys.readouterr().out == "recorded text\n"
    assert calls == {"backend": "fake", "record_seconds": 1.5, "inject": True}


def test_inject_returns_failure_for_failed_output(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    class _Injector:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def inject(self, text: str) -> InjectionResult:
            return InjectionResult("wtype", False, f"failed: {text}")

    monkeypatch.setattr("type4me_linux.cli.TextInjector", _Injector)

    code = main(["inject", "hello"])

    assert code == 1
    assert capsys.readouterr().err == "failed: hello\n"
