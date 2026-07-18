from __future__ import annotations

import json
from pathlib import Path

import pytest

from type4me_linux.cli import main


def _configure_runtime(fake_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for variable, name in (
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_DATA_HOME", "xdg-data"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("XDG_STATE_HOME", "xdg-state"),
    ):
        monkeypatch.setenv(variable, str(fake_runtime / name))
    vocabulary = fake_runtime / "xdg-data" / "type4me-linux" / "vocabulary"
    vocabulary.mkdir(parents=True)
    vocabulary.joinpath("snippets.json").write_text(
        '{"测试语音输入": "集成文本"}', encoding="utf-8"
    )
    config_path = fake_runtime / "current-config.toml"
    config_path.write_text(
        f"""
        [asr]
        batch_backend = "fake"

        [capture]
        command = "{fake_runtime / "bin" / "pw-record"}"

        [inject]
        prefer = "wtype"
        wtype_command = "{fake_runtime / "bin" / "wtype"}"
        wl_copy_command = "{fake_runtime / "bin" / "wl-copy"}"
        clipboard_fallback = true
        timeout_seconds = 10.0
        """,
        encoding="utf-8",
    )
    return config_path


@pytest.mark.integration
def test_transcribe_injects_final_text_and_preserves_json_contract(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    config_path = _configure_runtime(fake_runtime, monkeypatch)
    wav_path = fake_runtime / "input.wav"
    wav_path.write_bytes(b"RIFF")

    code = main(
        [
            "--config",
            str(config_path),
            "transcribe",
            str(wav_path),
            "--backend",
            "fake",
            "--inject",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload == {
        "text": "集成文本",
        "backend": "fake",
        "draft_text": None,
        "injection": {"ok": True, "method": "wtype", "message": ""},
    }
    assert (fake_runtime / "logs" / "wtype.txt").read_text(encoding="utf-8") == "集成文本"


@pytest.mark.integration
def test_record_keeps_batch_run_once_contract(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    config_path = _configure_runtime(fake_runtime, monkeypatch)

    code = main(
        [
            "--config",
            str(config_path),
            "record",
            "--seconds",
            "0.25",
            "--backend",
            "fake",
            "--no-inject",
        ]
    )

    assert code == 0
    assert capsys.readouterr().out == "集成文本\n"
    arguments = (fake_runtime / "logs" / "pw-record.args").read_text(encoding="utf-8")
    assert "--duration\n0.25" in arguments
    assert not (fake_runtime / "logs" / "wtype.txt").exists()


@pytest.mark.integration
def test_product_data_commands_persist_and_serialize_end_to_end(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    config_path = _configure_runtime(fake_runtime, monkeypatch)
    prefix = ["--config", str(config_path)]

    assert main([*prefix, "mode", "add", "会议", "--prompt", "整理：{text}"]) == 0
    created = json.loads(capsys.readouterr().out)
    mode_id = created["id"]
    assert created["name"] == "会议"
    assert created["builtin"] is False

    assert (
        main(
            [
                *prefix,
                "mode",
                "update",
                mode_id,
                "--name",
                "会议纪要",
                "--processing-label",
                "整理中",
                "--sort-order",
                "9",
            ]
        )
        == 0
    )
    updated = json.loads(capsys.readouterr().out)
    assert (updated["name"], updated["processing_label"], updated["sort_order"]) == (
        "会议纪要",
        "整理中",
        9,
    )

    assert main([*prefix, "mode", "reload"]) == 0
    reloaded = json.loads(capsys.readouterr().out)
    assert any(item["id"] == mode_id and item["name"] == "会议纪要" for item in reloaded)

    assert main([*prefix, "mode", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed][-1] == mode_id

    assert main([*prefix, "mode", "remove", mode_id]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == mode_id

    assert main([*prefix, "vocabulary", "hotwords", "add", "Type4Me"]) == 0
    assert json.loads(capsys.readouterr().out) == ["Type4Me"]
    assert main([*prefix, "vocabulary", "hotwords", "update", "Type4Me", "Type Four Me"]) == 0
    assert json.loads(capsys.readouterr().out) == ["Type Four Me"]
    assert main([*prefix, "vocabulary", "hotwords", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == ["Type Four Me"]
    assert main([*prefix, "vocabulary", "hotwords", "remove", "Type Four Me"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert main([*prefix, "vocabulary", "snippets", "add", "邮箱", "me@example.com"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                *prefix,
                "vocabulary",
                "snippets",
                "update",
                "邮箱",
                "mail@example.com",
                "--new-trigger",
                "电子邮箱",
            ]
        )
        == 0
    )
    snippets = json.loads(capsys.readouterr().out)
    assert snippets["电子邮箱"] == "mail@example.com"
    assert "邮箱" not in snippets
    assert main([*prefix, "vocabulary", "snippets", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["电子邮箱"] == "mail@example.com"
    assert main([*prefix, "vocabulary", "snippets", "remove", "电子邮箱"]) == 0
    assert "电子邮箱" not in json.loads(capsys.readouterr().out)
    assert main([*prefix, "vocabulary", "reload"]) == 0
    vocabulary = json.loads(capsys.readouterr().out)
    assert vocabulary["hotwords"] == []
    assert vocabulary["snippets"] == {"测试语音输入": "集成文本"}

    wav_path = fake_runtime / "history.wav"
    wav_path.write_bytes(b"RIFF")
    assert main([*prefix, "transcribe", str(wav_path), "--backend", "fake"]) == 0
    assert capsys.readouterr().out == "集成文本\n"

    assert main([*prefix, "history", "list", "--limit", "1"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert len(page["records"]) == 1
    assert page["records"][0]["raw_text"] == "测试语音输入"
    assert page["records"][0]["final_text"] == "集成文本"

    assert main([*prefix, "history", "totals"]) == 0
    totals = json.loads(capsys.readouterr().out)
    assert totals["records"] == 1
    assert totals["characters"] == len("集成文本")

    assert main([*prefix, "history", "usage", "--days", "1"]) == 0
    usage = json.loads(capsys.readouterr().out)
    assert usage["days"] == 1
    assert usage["providers"] == {"fake": 1}

    destination = fake_runtime / "exports" / "history.csv"
    assert main([*prefix, "history", "export", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "exported": 1,
        "path": str(destination),
    }
    assert "集成文本" in destination.read_text(encoding="utf-8")

    assert main([*prefix, "history", "delete", "--all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": 1}
