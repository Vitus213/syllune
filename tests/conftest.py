from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    _write_executable(
        bin_dir / "pw-record",
        f"""\
        #!{sys.executable}
        from pathlib import Path
        import os
        import sys

        Path(os.environ["TYPE4ME_FAKE_LOG_DIR"], "pw-record.args").write_text(
            "\\n".join(sys.argv[1:]),
            encoding="utf-8",
        )
        Path(sys.argv[-1]).write_bytes(b"RIFF fake wav")
        """,
    )
    _write_executable(
        bin_dir / "sherpa-onnx-offline",
        f"""\
        #!{sys.executable}
        from pathlib import Path
        import json
        import os
        import sys

        Path(os.environ["TYPE4ME_FAKE_LOG_DIR"], "sherpa.args").write_text(
            "\\n".join(sys.argv[1:]),
            encoding="utf-8",
        )
        text = os.environ.get("TYPE4ME_FAKE_ASR_TEXT", "我的邮箱")
        print(json.dumps({{"text": text}}, ensure_ascii=False))
        """,
    )
    _write_executable(
        bin_dir / "wtype",
        f"""\
        #!{sys.executable}
        from pathlib import Path
        import os
        import sys

        Path(os.environ["TYPE4ME_FAKE_LOG_DIR"], "wtype.txt").write_text(
            sys.argv[1],
            encoding="utf-8",
        )
        """,
    )
    _write_executable(
        bin_dir / "wl-copy",
        f"""\
        #!{sys.executable}
        from pathlib import Path
        import os
        import sys

        Path(os.environ["TYPE4ME_FAKE_LOG_DIR"], "wl-copy.txt").write_text(
            sys.stdin.read(),
            encoding="utf-8",
        )
        """,
    )

    monkeypatch.setenv("TYPE4ME_FAKE_LOG_DIR", str(log_dir))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return tmp_path


@pytest.fixture
def fake_config(fake_runtime: Path) -> Path:
    sensevoice_dir = fake_runtime / "models" / "sensevoice"
    sensevoice_dir.mkdir(parents=True)
    (sensevoice_dir / "model.onnx").write_text("model", encoding="utf-8")
    (sensevoice_dir / "tokens.txt").write_text("tokens", encoding="utf-8")

    qwen_dir = fake_runtime / "models" / "qwen3"
    qwen_dir.mkdir(parents=True)
    for name in ["conv_frontend.onnx", "encoder.onnx", "decoder.onnx"]:
        (qwen_dir / name).write_text(name, encoding="utf-8")
    (qwen_dir / "tokenizer").mkdir()

    bin_dir = fake_runtime / "bin"
    config_path = fake_runtime / "config.toml"
    config_path.write_text(
        f"""
        [asr]
        backend = "sensevoice"
        language = "zh"
        sensevoice_model_dir = "{sensevoice_dir}"
        qwen3_model_dir = "{qwen_dir}"
        sensevoice_command = "{bin_dir / "sherpa-onnx-offline"}"
        qwen3_command = "{bin_dir / "sherpa-onnx-offline"}"
        timeout_seconds = 10.0

        [capture]
        command = "{bin_dir / "pw-record"}"

        [inject]
        prefer = "wtype"
        wtype_command = "{bin_dir / "wtype"}"
        wl_copy_command = "{bin_dir / "wl-copy"}"
        clipboard_fallback = true
        timeout_seconds = 10.0

        [snippets]
        "我的邮箱" = "me@example.com"
        """,
        encoding="utf-8",
    )
    return config_path
