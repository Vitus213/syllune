from __future__ import annotations

from pathlib import Path

import pytest

from type4me_linux.config import config_from_mapping


def test_config_expands_model_dir() -> None:
    cfg = config_from_mapping(
        {
            "asr": {
                "sensevoice_model_dir": "~/models/sensevoice",
                "qwen3_model_dir": "~/models/qwen3",
                "hotwords": ["Qwen"],
            }
        }
    )

    assert cfg.asr.sensevoice_model_dir == Path.home() / "models" / "sensevoice"
    assert cfg.asr.qwen3_model_dir == Path.home() / "models" / "qwen3"
    assert cfg.asr.hotwords == ("Qwen",)


def test_config_keeps_legacy_model_dir_as_sensevoice_alias() -> None:
    cfg = config_from_mapping({"asr": {"model_dir": "~/models/sensevoice"}})

    assert cfg.asr.sensevoice_model_dir == Path.home() / "models" / "sensevoice"


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown ASRConfig keys"):
        config_from_mapping({"asr": {"bogus": True}})
