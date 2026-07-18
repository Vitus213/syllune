from __future__ import annotations

from pathlib import Path

import pytest

from type4me_linux.cli import main


@pytest.mark.e2e
def test_record_to_injection_end_to_end_with_fake_runtime(
    fake_runtime: Path,
    fake_config: Path,
    capsys,
) -> None:
    code = main(
        [
            "--config",
            str(fake_config),
            "record",
            "--seconds",
            "0.25",
            "--backend",
            "fake",
        ]
    )

    assert code == 0
    assert capsys.readouterr().out == "me@example.com\n"
    assert (fake_runtime / "logs" / "wtype.txt").read_text(encoding="utf-8") == "me@example.com"

    recorder_args = (fake_runtime / "logs" / "pw-record.args").read_text(encoding="utf-8")
    assert "--rate\n16000" in recorder_args
    assert "--channels\n1" in recorder_args
    assert "--duration\n0.25" in recorder_args
