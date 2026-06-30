from __future__ import annotations

from pathlib import Path

import pytest

from type4me_linux.config import config_from_mapping


def test_config_expands_model_dir() -> None:
    cfg = config_from_mapping({"asr": {"model_dir": "~/models/sensevoice", "hotwords": ["Qwen"]}})

    assert cfg.asr.model_dir == Path.home() / "models" / "sensevoice"
    assert cfg.asr.hotwords == ("Qwen",)


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown ASRConfig keys"):
        config_from_mapping({"asr": {"bogus": True}})

