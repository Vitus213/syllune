from __future__ import annotations

import json
from pathlib import Path

import pytest

from type4me_linux.cli import main


@pytest.mark.integration
def test_doctor_passes_with_fake_runtime(fake_runtime: Path, fake_config: Path, capsys) -> None:
    code = main(["--config", str(fake_config), "doctor"])

    output = capsys.readouterr().out
    assert code == 0
    assert "ok      sherpa-onnx-offline:" in output
    assert "ok      pw-record:" in output
    assert "ok      wtype:" in output
    assert "ok      wl-copy:" in output
    assert "ok      sensevoice model.onnx:" in output
    assert "ok      qwen3-asr tokenizer:" in output


@pytest.mark.integration
def test_transcribe_injects_sensevoice_text_with_fake_runtime(
    fake_runtime: Path,
    fake_config: Path,
    capsys,
) -> None:
    wav_path = fake_runtime / "input.wav"
    wav_path.write_bytes(b"RIFF")

    code = main(
        [
            "--config",
            str(fake_config),
            "transcribe",
            str(wav_path),
            "--backend",
            "sensevoice",
            "--inject",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["text"] == "me@example.com"
    assert payload["backend"] == "sensevoice"
    assert payload["injection"] == {"ok": True, "method": "wtype", "message": ""}
    assert (fake_runtime / "logs" / "wtype.txt").read_text(encoding="utf-8") == "me@example.com"
    assert "--model-type=sense-voice" in (fake_runtime / "logs" / "sherpa.args").read_text(
        encoding="utf-8"
    )
