from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


ArchiveType = Literal["tar.bz2", "file"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    version: str
    url: str
    sha256_sri: str | None
    size_bytes: int
    archive_type: ArchiveType
    top_level_directory: str | None
    allowed_members: tuple[str, ...]
    required_paths: tuple[str, ...]
    license_source: str
    license_status: str


_SENSEVOICE_MEMBERS = (
    "LICENSE",
    "README.md",
    "export-onnx.py",
    "model.int8.onnx",
    "test_wavs/en.wav",
    "test_wavs/ja.wav",
    "test_wavs/ko.wav",
    "test_wavs/yue.wav",
    "test_wavs/zh.wav",
    "tokens.txt",
)

_QWEN_MEMBERS = (
    "README.md",
    "conv_frontend.onnx",
    "decoder.int8.onnx",
    "encoder.int8.onnx",
    "test_wavs/README.md",
    "test_wavs/ar1.wav",
    "test_wavs/cantonese.wav",
    "test_wavs/codeswitch.wav",
    "test_wavs/de.wav",
    "test_wavs/es1.wav",
    "test_wavs/f1_noise.wav",
    "test_wavs/fast1.wav",
    "test_wavs/fr1.wav",
    "test_wavs/ja1.wav",
    "test_wavs/noise1-en.wav",
    "test_wavs/noise2.wav",
    "test_wavs/qiqiu1.wav",
    "test_wavs/raokouling.wav",
    "test_wavs/rap1.wav",
    "test_wavs/ru1.wav",
    "test_wavs/transcript.txt",
    "tokenizer/merges.txt",
    "tokenizer/tokenizer_config.json",
    "tokenizer/vocab.json",
)

_MODEL_SPECS = (
    ModelSpec(
        id="sensevoice-int8",
        version="2024-07-17",
        url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
        ),
        sha256_sri="sha256-fR76ITimWwtIjfN/i4nj2RpgZ25Bb1FblSNY2D39NH4=",
        size_bytes=163_002_883,
        archive_type="tar.bz2",
        top_level_directory="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        allowed_members=_SENSEVOICE_MEMBERS,
        required_paths=("model.int8.onnx", "tokens.txt"),
        license_source="https://github.com/FunAudioLLM/SenseVoice/blob/main/LICENSE",
        license_status="上游 SenseVoice 标注为 MIT；发布归档内许可证文本尚未独立核验。",
    ),
    ModelSpec(
        id="silero-vad",
        version="asr-models",
        url=("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"),
        sha256_sri="sha256-niRJ4Qh0ltjUyrqQfyPgvT942R+lUkebucI6wJy7H9Y=",
        size_bytes=643_854,
        archive_type="file",
        top_level_directory=None,
        allowed_members=("silero_vad.onnx",),
        required_paths=("silero_vad.onnx",),
        license_source="https://github.com/snakers4/silero-vad/blob/master/LICENSE",
        license_status="上游 Silero VAD 标注为 MIT；模型由 sherpa-onnx 发布页直接提供。",
    ),
    ModelSpec(
        id="qwen3-asr-0.6b-int8",
        version="2026-03-25",
        url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2"
        ),
        sha256_sri="sha256-OT+KFOL1+5Z0aqqzQpl6QGQQAfvVv5WSoICoMpF47pY=",
        size_bytes=878_702_423,
        archive_type="tar.bz2",
        top_level_directory="sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        allowed_members=_QWEN_MEMBERS,
        required_paths=(
            "conv_frontend.onnx",
            "encoder.int8.onnx",
            "decoder.int8.onnx",
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
            "tokenizer/vocab.json",
        ),
        license_source="https://github.com/QwenLM/Qwen3-ASR/blob/main/LICENSE",
        license_status=(
            "上游 Qwen3-ASR 标注为 Apache-2.0；转换后归档未附许可证，重新分发许可尚未核实。"
        ),
    ),
)

MODEL_CATALOG: Mapping[str, ModelSpec] = MappingProxyType({spec.id: spec for spec in _MODEL_SPECS})


def get_model_spec(model_id: str) -> ModelSpec:
    try:
        return MODEL_CATALOG[model_id]
    except KeyError as exc:
        raise KeyError(f"未知模型 ID：{model_id}") from exc
