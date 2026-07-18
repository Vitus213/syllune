from __future__ import annotations

from dataclasses import asdict

import pytest

from type4me_linux.config import (
    ASRConfig,
    CaptureConfig,
    Config,
    DaemonConfig,
    HistoryConfig,
    InjectConfig,
    ProcessingConfig,
    config_from_mapping,
    load_config,
)


def test_exact_defaults() -> None:
    assert Config() == Config(
        asr=ASRConfig(
            batch_backend="hybrid",
            streaming_backend="sensevoice-vad",
            final_backend="qwen3-sherpa",
            sensevoice_model_id="sensevoice-int8",
            vad_model_id="silero-vad",
            qwen3_model_id="qwen3-asr-0.6b-int8",
            language="zh",
            provider="cpu",
            num_threads=4,
            vad_threshold=0.2,
            vad_min_speech_seconds=0.2,
            vad_min_silence_seconds=0.5,
            vad_max_speech_seconds=20.0,
        ),
        capture=CaptureConfig(
            command="pw-record",
            sample_rate=16000,
            channels=1,
            format="s16",
            chunk_millis=200,
        ),
        inject=InjectConfig(
            prefer="wtype",
            wtype_command="wtype",
            wl_copy_command="wl-copy",
            clipboard_fallback=True,
            timeout_seconds=10.0,
        ),
        processing=ProcessingConfig(
            provider="none",
            base_url="",
            model="",
            api_key_env="",
            timeout_seconds=30.0,
        ),
        history=HistoryConfig(enabled=True),
        daemon=DaemonConfig(host="127.0.0.1", port=8766),
    )
    assert set(asdict(Config())) == {
        "asr",
        "capture",
        "inject",
        "processing",
        "history",
        "daemon",
    }


def test_all_strict_sections_accept_model_ids_and_supported_values() -> None:
    config = config_from_mapping(
        {
            "asr": {
                "batch_backend": "sensevoice",
                "streaming_backend": "sensevoice-vad",
                "final_backend": "sensevoice",
                "sensevoice_model_id": "sensevoice-int8",
                "vad_model_id": "silero-vad",
                "qwen3_model_id": "qwen3-asr-0.6b-int8",
                "language": "yue",
                "provider": "cuda",
                "num_threads": 8,
                "vad_threshold": 0.3,
                "vad_min_speech_seconds": 0.1,
                "vad_min_silence_seconds": 0.4,
                "vad_max_speech_seconds": 10.0,
            },
            "capture": {
                "command": "/run/current-system/sw/bin/pw-record",
                "sample_rate": 48000,
                "channels": 2,
                "format": "s16",
                "chunk_millis": 500,
            },
            "inject": {
                "prefer": "clipboard",
                "wtype_command": "wtype",
                "wl_copy_command": "wl-copy",
                "clipboard_fallback": False,
                "timeout_seconds": 2,
            },
            "processing": {
                "provider": "openai-compatible",
                "base_url": "https://example.invalid/v1",
                "model": "local-model",
                "api_key_env": "TYPE4ME_API_KEY",
                "timeout_seconds": 5.5,
            },
            "history": {"enabled": False},
            "daemon": {"host": "::1", "port": 9000},
        }
    )

    assert config.asr.sensevoice_model_id == "sensevoice-int8"
    assert config.processing.api_key_env == "TYPE4ME_API_KEY"
    assert config.daemon.port == 9000


@pytest.mark.parametrize(
    "legacy",
    [
        "model_dir",
        "sensevoice_model_dir",
        "qwen3_model_dir",
        "sensevoice_command",
        "qwen3_command",
        "qwen_endpoint",
        "use_qwen_final",
        "hotwords",
        "backend",
        "timeout_seconds",
    ],
)
def test_rejects_every_legacy_asr_key_without_aliases(legacy: str) -> None:
    with pytest.raises(ValueError, match=r"配置节 \[asr\] 包含未知键"):
        config_from_mapping({"asr": {legacy: "legacy"}})


@pytest.mark.parametrize("section", ["snippets", "hotwords", "models", "unknown"])
def test_rejects_unknown_and_static_vocabulary_sections(section: str) -> None:
    with pytest.raises(ValueError, match="配置包含未知顶层节"):
        config_from_mapping({section: {}})


def test_rejects_removed_notify_command() -> None:
    with pytest.raises(ValueError, match=r"配置节 \[inject\] 包含未知键：notify_command"):
        config_from_mapping({"inject": {"notify_command": "notify-send"}})


@pytest.mark.parametrize(
    ("mapping", "error_type", "message"),
    [
        ({"asr": []}, TypeError, r"配置节 \[asr\] 必须是 TOML 表"),
        ({"asr": {"num_threads": True}}, TypeError, "asr.num_threads 必须是整数"),
        ({"asr": {"num_threads": 0}}, ValueError, "asr.num_threads 必须在"),
        ({"asr": {"vad_threshold": "0.2"}}, TypeError, "asr.vad_threshold 必须是数字"),
        ({"asr": {"vad_threshold": 0}}, ValueError, "asr.vad_threshold 必须大于"),
        ({"asr": {"provider": "rocm"}}, ValueError, "asr.provider 的值无效"),
        ({"asr": {"sensevoice_model_id": "../model"}}, ValueError, "不是有效的模型 ID"),
        (
            {"asr": {"vad_min_speech_seconds": 2.0, "vad_max_speech_seconds": 1.0}},
            ValueError,
            "vad_max_speech_seconds 不得小于",
        ),
        ({"capture": {"sample_rate": 0}}, ValueError, "capture.sample_rate 必须在"),
        ({"capture": {"format": "f32"}}, ValueError, "capture.format 的值无效"),
        ({"inject": {"clipboard_fallback": 1}}, TypeError, "必须是布尔值"),
        ({"inject": {"prefer": "xdotool"}}, ValueError, "inject.prefer 的值无效"),
        ({"processing": {"provider": "openai"}}, ValueError, "processing.provider 的值无效"),
        ({"processing": {"api_key_env": "BAD-NAME"}}, ValueError, "不是有效的环境变量名"),
        ({"history": {"enabled": "true"}}, TypeError, "history.enabled 必须是布尔值"),
        ({"daemon": {"port": 65536}}, ValueError, "daemon.port 必须在"),
    ],
)
def test_nested_types_ranges_and_enums_are_validated(
    mapping: dict[str, object], error_type: type[Exception], message: str
) -> None:
    with pytest.raises(error_type, match=message):
        config_from_mapping(mapping)


def test_load_config_reports_toml_error_in_chinese(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config_path = tmp_path / "config.toml"
    config_path.write_text("[asr\n", encoding="utf-8")

    with pytest.raises(ValueError, match="无法读取配置文件"):
        load_config(config_path)
