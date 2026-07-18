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
    _write_executable(
        bin_dir / "wl-paste",
        f"""\
        #!{sys.executable}
        import sys

        if "--primary" in sys.argv:
            print("", end="")
        else:
            print("", end="")
        """,
    )

    monkeypatch.setenv("TYPE4ME_FAKE_LOG_DIR", str(log_dir))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path))
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv(variable, str(root))
    return tmp_path


@pytest.fixture
def fake_config(fake_runtime: Path) -> Path:
    vocabulary_dir = fake_runtime / "data" / "type4me-linux" / "vocabulary"
    vocabulary_dir.mkdir(parents=True)
    (vocabulary_dir / "hotwords.json").write_text("[]", encoding="utf-8")
    (vocabulary_dir / "snippets.json").write_text(
        '{"测试语音输入": "me@example.com"}', encoding="utf-8"
    )

    bin_dir = fake_runtime / "bin"
    config_path = fake_runtime / "config.toml"
    config_path.write_text(
        f"""
        [asr]
        batch_backend = "fake"
        streaming_backend = "sensevoice-vad"
        final_backend = "qwen3-sherpa"
        sensevoice_model_id = "sensevoice-int8"
        vad_model_id = "silero-vad"
        qwen3_model_id = "qwen3-asr-0.6b-int8"
        language = "zh"
        provider = "cpu"
        num_threads = 4
        vad_threshold = 0.2
        vad_min_speech_seconds = 0.2
        vad_min_silence_seconds = 0.5
        vad_max_speech_seconds = 20.0

        [capture]
        command = "{bin_dir / "pw-record"}"
        sample_rate = 16000
        channels = 1
        format = "s16"
        chunk_millis = 200

        [inject]
        prefer = "wtype"
        wtype_command = "{bin_dir / "wtype"}"
        wl_copy_command = "{bin_dir / "wl-copy"}"
        clipboard_fallback = true
        timeout_seconds = 10.0

        [processing]
        provider = "none"
        base_url = ""
        model = ""
        api_key_env = ""
        timeout_seconds = 30.0

        [history]
        enabled = true

        [daemon]
        host = "127.0.0.1"
        port = 8766
        """,
        encoding="utf-8",
    )
    return config_path
