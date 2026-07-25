from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar


_T = TypeVar("_T")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def default_config_path() -> Path:
    return _xdg_config_home() / "type4me-linux" / "config.toml"


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


@dataclass(frozen=True)
class ASRConfig:
    batch_backend: str = "hybrid"
    streaming_backend: str = "sensevoice-vad"
    final_backend: str = "qwen3-sherpa"
    sensevoice_model_id: str = "sensevoice-int8"
    vad_model_id: str = "silero-vad"
    qwen3_model_id: str = "qwen3-asr-0.6b-int8"
    language: str = "zh"
    provider: str = "cpu"
    num_threads: int = 4
    vad_threshold: float = 0.2
    vad_min_speech_seconds: float = 0.2
    vad_min_silence_seconds: float = 0.5
    vad_max_speech_seconds: float = 20.0
    qwen3_max_segment_seconds: float = 12.0


@dataclass(frozen=True)
class CaptureConfig:
    command: str = "pw-record"
    sample_rate: int = 16000
    channels: int = 1
    format: str = "s16"
    chunk_millis: int = 200


@dataclass(frozen=True)
class InjectConfig:
    prefer: str = "wtype"
    wtype_command: str = "wtype"
    wl_copy_command: str = "wl-copy"
    clipboard_fallback: bool = True
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ProcessingConfig:
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class HistoryConfig:
    enabled: bool = True


@dataclass(frozen=True)
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8766


@dataclass(frozen=True)
class Config:
    asr: ASRConfig = field(default_factory=ASRConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)


def load_config(path: str | Path | None = None) -> Config:
    config_path = expand_path(path) if path else default_config_path()
    if not config_path.exists():
        return Config()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法读取配置文件“{config_path}”：{exc}") from exc
    return config_from_mapping(raw)


def config_from_mapping(raw: Mapping[str, Any]) -> Config:
    if not isinstance(raw, Mapping):
        raise TypeError("配置根必须是 TOML 表。")
    allowed_sections = {"asr", "capture", "inject", "processing", "history", "daemon"}
    unknown_sections = sorted(set(raw) - allowed_sections)
    if unknown_sections:
        raise ValueError(f"配置包含未知顶层节：{', '.join(unknown_sections)}")

    config = Config(
        asr=_build_section(ASRConfig, raw.get("asr", {}), "asr"),
        capture=_build_section(CaptureConfig, raw.get("capture", {}), "capture"),
        inject=_build_section(InjectConfig, raw.get("inject", {}), "inject"),
        processing=_build_section(ProcessingConfig, raw.get("processing", {}), "processing"),
        history=_build_section(HistoryConfig, raw.get("history", {}), "history"),
        daemon=_build_section(DaemonConfig, raw.get("daemon", {}), "daemon"),
    )
    _validate_config(config)
    return config


def _build_section(section_type: Callable[..., _T], raw: object, section_name: str) -> _T:
    if not isinstance(raw, Mapping):
        raise TypeError(f"配置节 [{section_name}] 必须是 TOML 表。")
    allowed = {item.name for item in fields(section_type)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"配置节 [{section_name}] 包含未知键：{', '.join(unknown)}")
    return section_type(**raw)


def _validate_config(config: Config) -> None:
    asr = config.asr
    _enum("asr.batch_backend", asr.batch_backend, {"fake", "sensevoice", "qwen3-sherpa", "hybrid"})
    _enum("asr.streaming_backend", asr.streaming_backend, {"sensevoice-vad"})
    _enum("asr.final_backend", asr.final_backend, {"none", "sensevoice", "qwen3-sherpa"})
    _model_id("asr.sensevoice_model_id", asr.sensevoice_model_id)
    _model_id("asr.vad_model_id", asr.vad_model_id)
    _model_id("asr.qwen3_model_id", asr.qwen3_model_id)
    _enum("asr.language", asr.language, {"auto", "zh", "en", "ja", "ko", "yue"})
    _enum("asr.provider", asr.provider, {"cpu", "cuda"})
    _integer("asr.num_threads", asr.num_threads, minimum=1, maximum=256)
    _number("asr.vad_threshold", asr.vad_threshold, minimum=0.0, maximum=1.0, strict_minimum=True)
    _number(
        "asr.vad_min_speech_seconds", asr.vad_min_speech_seconds, minimum=0.0, strict_minimum=True
    )
    _number(
        "asr.vad_min_silence_seconds", asr.vad_min_silence_seconds, minimum=0.0, strict_minimum=True
    )
    _number(
        "asr.vad_max_speech_seconds", asr.vad_max_speech_seconds, minimum=0.0, strict_minimum=True
    )
    if asr.vad_max_speech_seconds < asr.vad_min_speech_seconds:
        raise ValueError("配置项 asr.vad_max_speech_seconds 不得小于 asr.vad_min_speech_seconds。")
    _number(
        "asr.qwen3_max_segment_seconds",
        asr.qwen3_max_segment_seconds,
        minimum=0.0,
        maximum=12.0,
        strict_minimum=True,
    )

    capture = config.capture
    _nonempty("capture.command", capture.command)
    _integer("capture.sample_rate", capture.sample_rate, minimum=8000, maximum=192000)
    _integer("capture.channels", capture.channels, minimum=1, maximum=8)
    _enum("capture.format", capture.format, {"s16"})
    _integer("capture.chunk_millis", capture.chunk_millis, minimum=10, maximum=5000)

    inject = config.inject
    _enum("inject.prefer", inject.prefer, {"wtype", "clipboard"})
    _nonempty("inject.wtype_command", inject.wtype_command)
    _nonempty("inject.wl_copy_command", inject.wl_copy_command)
    _boolean("inject.clipboard_fallback", inject.clipboard_fallback)
    _number("inject.timeout_seconds", inject.timeout_seconds, minimum=0.0, strict_minimum=True)

    processing = config.processing
    _enum("processing.provider", processing.provider, {"none", "openai-compatible", "ollama"})
    _string("processing.base_url", processing.base_url)
    _string("processing.model", processing.model)
    _string("processing.api_key_env", processing.api_key_env)
    if processing.api_key_env and not _ENVIRONMENT_NAME.fullmatch(processing.api_key_env):
        raise ValueError("配置项 processing.api_key_env 不是有效的环境变量名。")
    _number(
        "processing.timeout_seconds", processing.timeout_seconds, minimum=0.0, strict_minimum=True
    )

    _boolean("history.enabled", config.history.enabled)
    _nonempty("daemon.host", config.daemon.host)
    _integer("daemon.port", config.daemon.port, minimum=1, maximum=65535)


def _string(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"配置项 {name} 必须是字符串。")


def _nonempty(name: str, value: object) -> None:
    _string(name, value)
    if not value.strip():
        raise ValueError(f"配置项 {name} 不得为空。")


def _enum(name: str, value: object, choices: set[str]) -> None:
    _string(name, value)
    if value not in choices:
        raise ValueError(
            f"配置项 {name} 的值无效：{value}；可选值为 {', '.join(sorted(choices))}。"
        )


def _model_id(name: str, value: object) -> None:
    _nonempty(name, value)
    if not _MODEL_ID.fullmatch(value):
        raise ValueError(f"配置项 {name} 不是有效的模型 ID。")


def _boolean(name: str, value: object) -> None:
    if type(value) is not bool:
        raise TypeError(f"配置项 {name} 必须是布尔值。")


def _integer(name: str, value: object, *, minimum: int, maximum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"配置项 {name} 必须是整数。")
    if not minimum <= value <= maximum:
        raise ValueError(f"配置项 {name} 必须在 {minimum} 到 {maximum} 之间。")


def _number(
    name: str,
    value: object,
    *,
    minimum: float,
    maximum: float | None = None,
    strict_minimum: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"配置项 {name} 必须是数字。")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"配置项 {name} 必须是有限数字。")
    below = number <= minimum if strict_minimum else number < minimum
    if below or (maximum is not None and number > maximum):
        interval = f"大于 {minimum}" if strict_minimum else f"不小于 {minimum}"
        if maximum is not None:
            interval += f"且不大于 {maximum}"
        raise ValueError(f"配置项 {name} 必须{interval}。")
