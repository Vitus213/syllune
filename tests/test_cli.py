from __future__ import annotations

from type4me_linux.cli import main
from type4me_linux.doctor import Check


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
