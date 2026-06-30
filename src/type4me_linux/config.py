from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def default_config_path() -> Path:
    return _xdg_config_home() / "type4me-linux" / "config.toml"


def default_sensevoice_model_dir() -> Path:
    return _xdg_data_home() / "type4me-linux" / "models" / "sensevoice-small"


def default_qwen3_model_dir() -> Path:
    return _xdg_data_home() / "type4me-linux" / "models" / "qwen3-asr-0.6b"


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


@dataclass(frozen=True)
class ASRConfig:
    backend: str = "sensevoice"
    language: str = "zh"
    sensevoice_model_dir: Path = field(default_factory=default_sensevoice_model_dir)
    qwen3_model_dir: Path = field(default_factory=default_qwen3_model_dir)
    sensevoice_command: str = "sherpa-onnx-offline"
    qwen3_command: str = "sherpa-onnx-offline"
    qwen_endpoint: str = "http://127.0.0.1:8765/transcribe"
    use_qwen_final: bool = False
    provider: str = "cpu"
    num_threads: int = 4
    hotwords: tuple[str, ...] = ()
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class CaptureConfig:
    command: str = "pw-record"
    sample_rate: int = 16000
    channels: int = 1
    format: str = "s16"


@dataclass(frozen=True)
class InjectConfig:
    prefer: str = "wtype"
    wtype_command: str = "wtype"
    wl_copy_command: str = "wl-copy"
    notify_command: str = "notify-send"
    clipboard_fallback: bool = True
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8766


@dataclass(frozen=True)
class Config:
    asr: ASRConfig = field(default_factory=ASRConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    snippets: dict[str, str] = field(default_factory=dict)


def load_config(path: str | Path | None = None) -> Config:
    config_path = expand_path(path) if path else default_config_path()
    if not config_path.exists():
        return Config()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    return config_from_mapping(raw)


def config_from_mapping(raw: dict[str, Any]) -> Config:
    asr = _asr_config(raw.get("asr", {}))
    capture = _dataclass_from_mapping(CaptureConfig(), raw.get("capture", {}))
    inject = _dataclass_from_mapping(InjectConfig(), raw.get("inject", {}))
    daemon = _dataclass_from_mapping(DaemonConfig(), raw.get("daemon", {}))
    snippets = {str(k): str(v) for k, v in raw.get("snippets", {}).items()}
    return Config(asr=asr, capture=capture, inject=inject, daemon=daemon, snippets=snippets)


def _asr_config(raw: dict[str, Any]) -> ASRConfig:
    normalized = dict(raw)
    legacy_model_dir = normalized.pop("model_dir", None)
    cfg = _dataclass_from_mapping(ASRConfig(), normalized)
    sensevoice_model_dir = (
        expand_path(normalized["sensevoice_model_dir"])
        if "sensevoice_model_dir" in normalized
        else expand_path(legacy_model_dir)
        if legacy_model_dir is not None
        else cfg.sensevoice_model_dir
    )
    qwen3_model_dir = (
        expand_path(normalized["qwen3_model_dir"])
        if "qwen3_model_dir" in normalized
        else cfg.qwen3_model_dir
    )
    hotwords = tuple(str(item) for item in raw.get("hotwords", cfg.hotwords))
    return replace(
        cfg,
        sensevoice_model_dir=sensevoice_model_dir,
        qwen3_model_dir=qwen3_model_dir,
        hotwords=hotwords,
    )


def _dataclass_from_mapping(instance: Any, raw: dict[str, Any]) -> Any:
    if not isinstance(raw, dict):
        raise TypeError(f"expected mapping for {type(instance).__name__}")
    allowed = instance.__dataclass_fields__.keys()
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown {type(instance).__name__} keys: {joined}")
    return replace(instance, **raw)
