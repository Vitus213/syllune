from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from type4me_linux.model_catalog import MODEL_CATALOG, get_model_spec


def test_catalog_contains_exact_pinned_sources() -> None:
    assert set(MODEL_CATALOG) == {
        "sensevoice-int8",
        "silero-vad",
        "qwen3-asr-0.6b-int8",
        "streaming-paraformer-bilingual-zh-en",
    }
    expected = {
        "sensevoice-int8": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
            "sha256-fR76ITimWwtIjfN/i4nj2RpgZ25Bb1FblSNY2D39NH4=",
            163_002_883,
        ),
        "silero-vad": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
            "sha256-niRJ4Qh0ltjUyrqQfyPgvT942R+lUkebucI6wJy7H9Y=",
            643_854,
        ),
        "qwen3-asr-0.6b-int8": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
            "sha256-OT+KFOL1+5Z0aqqzQpl6QGQQAfvVv5WSoICoMpF47pY=",
            878_702_423,
        ),
        "streaming-paraformer-bilingual-zh-en": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2",
            "sha256-VGKh/OQmk96uVyrx6MRocSSxKqhf5h/00xaLtSgOIF8=",
            1_047_319_737,
        ),
    }
    for model_id, values in expected.items():
        spec = MODEL_CATALOG[model_id]
        assert (spec.url, spec.sha256_sri, spec.size_bytes) == values
        assert spec.required_paths
        assert set(spec.required_paths) <= set(spec.allowed_members)
        assert spec.license_source.startswith("https://")
        assert spec.license_status


def test_catalog_records_exact_runtime_layouts() -> None:
    sensevoice = MODEL_CATALOG["sensevoice-int8"]
    assert sensevoice.archive_type == "tar.bz2"
    assert sensevoice.top_level_directory == (
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    )
    assert sensevoice.required_paths == ("model.int8.onnx", "tokens.txt")

    vad = MODEL_CATALOG["silero-vad"]
    assert vad.archive_type == "file"
    assert vad.top_level_directory is None
    assert vad.allowed_members == vad.required_paths == ("silero_vad.onnx",)

    qwen = MODEL_CATALOG["qwen3-asr-0.6b-int8"]
    assert qwen.archive_type == "tar.bz2"
    assert qwen.top_level_directory == "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
    assert qwen.required_paths == (
        "conv_frontend.onnx",
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "tokenizer/merges.txt",
        "tokenizer/tokenizer_config.json",
        "tokenizer/vocab.json",
    )
    assert "test_wavs/transcript.txt" in qwen.allowed_members

    streaming = MODEL_CATALOG["streaming-paraformer-bilingual-zh-en"]
    assert streaming.archive_type == "tar.bz2"
    assert streaming.top_level_directory == "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
    assert streaming.required_paths == (
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "tokens.txt",
    )


def test_model_specs_and_catalog_are_immutable() -> None:
    spec = MODEL_CATALOG["silero-vad"]
    with pytest.raises(FrozenInstanceError):
        spec.version = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        MODEL_CATALOG["other"] = spec  # type: ignore[index]


def test_unknown_catalog_id_has_localized_error() -> None:
    with pytest.raises(KeyError, match="未知模型 ID"):
        get_model_spec("missing")


def test_model_spec_is_slots_based() -> None:
    assert not hasattr(MODEL_CATALOG["silero-vad"], "__dict__")
