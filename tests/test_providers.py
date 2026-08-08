from __future__ import annotations

import wave
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from type4me_linux.config import ASRConfig
from type4me_linux.providers import (
    FakeProvider,
    HybridProvider,
    Qwen3SherpaProvider,
    RecognitionResult,
    SenseVoiceProvider,
    SenseVoiceVadStreamer,
    create_provider,
)


class _Stream:
    def __init__(self, text: str) -> None:
        self.accepted: list[tuple[int, np.ndarray]] = []
        self.result = SimpleNamespace(text=text)

    def accept_waveform(self, sample_rate: int, samples: np.ndarray) -> None:
        self.accepted.append((sample_rate, samples))


class _Recognizer:
    def __init__(self, texts: list[str]) -> None:
        self._texts = iter(texts)
        self.streams: list[_Stream] = []

    def create_stream(self) -> _Stream:
        stream = _Stream(next(self._texts))
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: _Stream) -> None:
        assert stream in self.streams


def _write_wav(path: Path, values: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(values.astype("<i2").tobytes())


def _sensevoice_model(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "model.int8.onnx").write_bytes(b"model")
    (tmp_path / "tokens.txt").write_text("tokens", encoding="utf-8")
    return tmp_path


def _qwen_model(tmp_path: Path) -> Path:
    for name in ("conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx"):
        (tmp_path / name).write_bytes(b"model")
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    for name in ("merges.txt", "tokenizer_config.json", "vocab.json"):
        (tokenizer / name).write_text("{}", encoding="utf-8")
    return tmp_path


def test_sensevoice_factory_receives_verified_arguments_and_loads_wave(tmp_path: Path) -> None:
    model_dir = _sensevoice_model(tmp_path / "sensevoice")
    model_dir.mkdir(exist_ok=True)
    wav_path = tmp_path / "audio.wav"
    _write_wav(wav_path, np.array([-32768, 0, 16384], dtype=np.int16))
    calls: list[dict[str, Any]] = []
    recognizer = _Recognizer([" 你好 NixOS "])

    def factory(**kwargs: Any) -> _Recognizer:
        calls.append(kwargs)
        return recognizer

    provider = SenseVoiceProvider(
        ASRConfig(language="zh", provider="cpu", num_threads=3),
        model_dir=model_dir,
        recognizer_factory=factory,
    )

    assert calls == []
    assert provider.transcribe(wav_path) == RecognitionResult("你好 NixOS", "sensevoice")
    assert calls == [
        {
            "model": str(model_dir / "model.int8.onnx"),
            "tokens": str(model_dir / "tokens.txt"),
            "language": "zh",
            "use_itn": True,
            "num_threads": 3,
            "provider": "cpu",
        }
    ]
    rate, samples = recognizer.streams[0].accepted[0]
    assert rate == 16_000
    assert samples.dtype == np.float32
    assert samples.tolist() == pytest.approx([-1.0, 0.0, 0.5])


def test_sensevoice_uses_a_fresh_stream_for_every_decode(tmp_path: Path) -> None:
    model_dir = tmp_path / "sensevoice"
    model_dir.mkdir()
    _sensevoice_model(model_dir)
    recognizer = _Recognizer(["一", "二"])
    provider = SenseVoiceProvider(
        ASRConfig(),
        model_dir=model_dir,
        recognizer_factory=lambda **kwargs: recognizer,
    )

    assert provider.transcribe_samples(np.zeros(512, dtype=np.float32)) == "一"
    assert provider.transcribe_samples(np.ones(512, dtype=np.float32)) == "二"
    assert len(recognizer.streams) == 2
    assert recognizer.streams[0] is not recognizer.streams[1]


def test_qwen_factory_arguments_and_private_sanitation(tmp_path: Path) -> None:
    model_dir = tmp_path / "qwen"
    model_dir.mkdir()
    _qwen_model(model_dir)
    wav_path = tmp_path / "audio.wav"
    _write_wav(wav_path, np.zeros(32, dtype=np.int16))
    calls: list[dict[str, Any]] = []
    recognizer = _Recognizer(["转写：热词：Qwen 最终   文本"])

    def factory(**kwargs: Any) -> _Recognizer:
        calls.append(kwargs)
        return recognizer

    provider = Qwen3SherpaProvider(
        ASRConfig(provider="cuda", num_threads=2),
        model_dir=model_dir,
        hotwords=("Qwen", "SenseVoice"),
        recognizer_factory=factory,
    )

    assert provider.transcribe(wav_path) == RecognitionResult("最终 文本", "qwen3-sherpa")
    assert calls == [
        {
            "conv_frontend": str(model_dir / "conv_frontend.onnx"),
            "encoder": str(model_dir / "encoder.int8.onnx"),
            "decoder": str(model_dir / "decoder.int8.onnx"),
            "tokenizer": str(model_dir / "tokenizer"),
            "hotwords": "Qwen,SenseVoice",
            "max_new_tokens": 512,
            "num_threads": 2,
            "provider": "cuda",
        }
    ]


def test_qwen_splits_long_audio_before_decoder_context_limit(tmp_path: Path) -> None:
    model_dir = tmp_path / "qwen"
    model_dir.mkdir()
    _qwen_model(model_dir)
    wav_path = tmp_path / "long.wav"
    _write_wav(wav_path, np.zeros(16_001, dtype=np.int16))
    recognizer = _Recognizer(["第一段", "第二段"])
    provider = Qwen3SherpaProvider(
        ASRConfig(qwen3_max_segment_seconds=1.0),
        model_dir=model_dir,
        recognizer_factory=lambda **_kwargs: recognizer,
    )

    assert provider.transcribe(wav_path) == RecognitionResult("第一段第二段", "qwen3-sherpa")
    assert [stream.accepted[0][1].size for stream in recognizer.streams] == [16_000, 1]


def test_provider_reports_missing_int8_runtime_files_in_chinese(tmp_path: Path) -> None:
    provider = SenseVoiceProvider(ASRConfig(), model_dir=tmp_path)

    with pytest.raises(FileNotFoundError, match="模型文件缺失"):
        provider.transcribe_samples(np.zeros(1, dtype=np.float32))


class _Segment:
    def __init__(self, samples: np.ndarray) -> None:
        self.samples = samples


class _Vad:
    def __init__(self) -> None:
        self.windows: list[np.ndarray] = []
        self.active: list[np.ndarray] = []
        self.completed: list[_Segment] = []

    def accept_waveform(self, samples: np.ndarray) -> None:
        assert samples.dtype == np.float32
        assert samples.size == 512
        self.windows.append(samples.copy())
        self.active.append(samples.copy())

    @property
    def is_speech_detected(self) -> bool:
        return bool(self.active)

    @property
    def current_segment(self) -> _Segment:
        values = np.concatenate(self.active) if self.active else np.empty(0, dtype=np.float32)
        return _Segment(values)

    def complete(self) -> None:
        if self.active:
            self.completed.append(self.current_segment)
            self.active.clear()

    def empty(self) -> bool:
        return not self.completed

    @property
    def front(self) -> _Segment:
        return self.completed[0]

    def pop(self) -> None:
        self.completed.pop(0)

    def flush(self) -> None:
        self.complete()


class _SegmentRecognizer:
    def __init__(self) -> None:
        self.inputs: list[np.ndarray] = []

    def transcribe_samples(self, samples: np.ndarray) -> str:
        self.inputs.append(samples.copy())
        return "乙" if float(samples.max(initial=0.0)) > 0.04 else "甲"


def _pcm(value: int, samples: int = 3_200) -> bytes:
    return np.full(samples, value, dtype="<i2").tobytes()


def test_vad_streamer_emits_changed_partial_confirmed_and_final_in_order(
    tmp_path: Path,
) -> None:
    vad_model = tmp_path / "silero_vad.onnx"
    vad_model.write_bytes(b"model")
    vad = _Vad()
    vad_args: list[dict[str, Any]] = []

    def vad_factory(**kwargs: Any) -> _Vad:
        vad_args.append(kwargs)
        return vad

    recognizer = _SegmentRecognizer()
    streamer = SenseVoiceVadStreamer(
        ASRConfig(),
        recognizer,  # type: ignore[arg-type]
        vad_model_dir=vad_model,
        vad_factory=vad_factory,
    )

    first = streamer.accept_chunk(_pcm(1_000))
    unchanged = streamer.accept_chunk(b"")
    vad.complete()
    confirmed = streamer.accept_chunk(b"")
    second = streamer.accept_chunk(_pcm(2_000))
    final = streamer.flush()

    assert [item.partial_text for item in first] == ["甲"]
    assert unchanged == ()
    assert [(item.confirmed_segments, item.partial_text) for item in confirmed] == [(("甲",), "")]
    assert [(item.confirmed_segments, item.partial_text) for item in second] == [(("甲",), "乙")]
    assert final.confirmed_segments == ("甲", "乙")
    assert final.partial_text == ""
    assert final.authoritative_text == "甲乙"
    assert final.is_final is True
    assert final.backend == "sensevoice-vad"
    assert streamer.flush() is final
    assert all(window.size == 512 for window in vad.windows)
    assert vad_args == [
        {
            "model": str(vad_model),
            "threshold": 0.2,
            "min_silence_duration": 0.5,
            "min_speech_duration": 0.2,
            "window_size": 512,
            "max_speech_duration": 20.0,
            "sample_rate": 16_000,
        }
    ]


def test_vad_partial_cadence_bounds_decoding_and_confirms_completed_segments(
    tmp_path: Path,
) -> None:
    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"model")
    vad = _Vad()
    recognizer = _SegmentRecognizer()
    streamer = SenseVoiceVadStreamer(
        ASRConfig(partial_interval_millis=200),
        recognizer,  # type: ignore[arg-type]
        vad_model_dir=model,
        vad_factory=lambda **_kwargs: vad,
    )

    assert streamer.accept_chunk(_pcm(1_000, 3_199)) == ()
    assert recognizer.inputs == []
    assert [item.partial_text for item in streamer.accept_chunk(_pcm(1_000, 1))] == ["甲"]
    assert len(recognizer.inputs) == 1

    assert streamer.accept_chunk(_pcm(1_000, 3_199)) == ()
    assert len(recognizer.inputs) == 1
    assert streamer.accept_chunk(_pcm(1_000, 1)) == ()
    assert len(recognizer.inputs) == 2

    assert streamer.accept_chunk(_pcm(1_000, 6_400)) == ()
    assert len(recognizer.inputs) == 3

    vad.complete()
    confirmed = streamer.accept_chunk(b"")
    assert len(recognizer.inputs) == 4
    assert [(item.confirmed_segments, item.partial_text) for item in confirmed] == [(("甲",), "")]


def test_vad_partial_cadence_uses_configured_interval(tmp_path: Path) -> None:
    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"model")
    recognizer = _SegmentRecognizer()
    streamer = SenseVoiceVadStreamer(
        ASRConfig(partial_interval_millis=400),
        recognizer,  # type: ignore[arg-type]
        vad_model_dir=model,
        vad_factory=lambda **_kwargs: _Vad(),
    )

    assert streamer.accept_chunk(_pcm(1_000, 3_200)) == ()
    assert recognizer.inputs == []
    assert [item.partial_text for item in streamer.accept_chunk(_pcm(1_000, 3_200))] == ["甲"]
    assert len(recognizer.inputs) == 1


def test_create_provider_has_no_http_qwen_backend() -> None:
    assert isinstance(create_provider(ASRConfig(batch_backend="fake")), FakeProvider)
    with pytest.raises(ValueError, match="不支持的 ASR 后端"):
        create_provider(ASRConfig(batch_backend="qwen3-asr"))


def test_qwen_reports_all_missing_runtime_files(tmp_path: Path) -> None:
    provider = Qwen3SherpaProvider(ASRConfig(), model_dir=tmp_path)
    wav_path = tmp_path / "audio.wav"
    _write_wav(wav_path, np.zeros(1, dtype=np.int16))

    missing = provider.missing_model_files()

    assert {path.relative_to(tmp_path).as_posix() for path in missing} == {
        "conv_frontend.onnx",
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "tokenizer/merges.txt",
        "tokenizer/tokenizer_config.json",
        "tokenizer/vocab.json",
    }
    with pytest.raises(FileNotFoundError, match="Qwen3-ASR 模型文件缺失"):
        provider.transcribe(wav_path)


@pytest.mark.parametrize(
    ("sample_rate", "channels", "sample_width", "message"),
    [
        (8_000, 1, 2, "采样率"),
        (16_000, 2, 2, "单声道"),
        (16_000, 1, 1, "PCM16"),
    ],
)
def test_sensevoice_rejects_incompatible_wav_format_before_model_loading(
    tmp_path: Path,
    sample_rate: int,
    channels: int,
    sample_width: int,
    message: str,
) -> None:
    wav_path = tmp_path / f"invalid-{sample_rate}-{channels}-{sample_width}.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * sample_width * channels * 8)
    factory_called = False

    def factory(**_kwargs: Any) -> _Recognizer:
        nonlocal factory_called
        factory_called = True
        return _Recognizer(["不应识别"])

    provider = SenseVoiceProvider(
        ASRConfig(),
        model_dir=tmp_path,
        recognizer_factory=factory,
    )

    with pytest.raises(ValueError, match=message):
        provider.transcribe(wav_path)
    assert factory_called is False


def test_recognizer_result_must_expose_text(tmp_path: Path) -> None:
    class MissingTextRecognizer:
        def create_stream(self) -> Any:
            stream = _Stream("")
            stream.result = SimpleNamespace()
            return stream

        def decode_stream(self, stream: Any) -> None:
            return None

    provider = SenseVoiceProvider(
        ASRConfig(),
        model_dir=_sensevoice_model(tmp_path),
        recognizer_factory=lambda **_kwargs: MissingTextRecognizer(),
    )

    with pytest.raises(ValueError, match="识别结果缺少文本"):
        provider.transcribe_samples(np.zeros(4, dtype=np.float32))


def test_hybrid_provider_prefers_qwen_and_falls_back_to_draft(tmp_path: Path) -> None:
    class Provider:
        def __init__(self, result: RecognitionResult | Exception) -> None:
            self.result = result
            self.paths: list[Path] = []

        def transcribe(self, wav_path: Path) -> RecognitionResult:
            self.paths.append(wav_path)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    wav_path = tmp_path / "audio.wav"
    draft = Provider(RecognitionResult("草稿", "sensevoice"))
    final = Provider(RecognitionResult("终稿", "qwen3-sherpa"))

    assert HybridProvider(draft, final).transcribe(wav_path) == RecognitionResult(
        "终稿", "hybrid", "草稿"
    )
    assert draft.paths == [wav_path]
    assert final.paths == [wav_path]

    fallback = HybridProvider(
        Provider(RecognitionResult("可用草稿", "sensevoice")),
        Provider(RuntimeError("校准失败")),
    ).transcribe(wav_path)
    assert fallback == RecognitionResult("可用草稿", "hybrid-fallback", "可用草稿")


@pytest.mark.parametrize(
    ("backend", "provider_type"),
    [
        ("sensevoice", SenseVoiceProvider),
        ("qwen3-sherpa", Qwen3SherpaProvider),
        ("hybrid", HybridProvider),
    ],
)
def test_create_provider_constructs_each_local_backend(
    backend: str,
    provider_type: type[object],
) -> None:
    provider = create_provider(
        ASRConfig(batch_backend=backend),
        model_resolver=lambda _model_id: Path("/unused"),
        sensevoice_factory=lambda **_kwargs: _Recognizer(["草稿"]),
        qwen_factory=lambda **_kwargs: _Recognizer(["终稿"]),
        numpy_module=np,
    )

    assert isinstance(provider, provider_type)


def test_vad_rejects_missing_model_odd_pcm_and_writes_after_flush(tmp_path: Path) -> None:
    recognizer = _SegmentRecognizer()
    with pytest.raises(FileNotFoundError, match="Silero VAD 模型文件缺失"):
        SenseVoiceVadStreamer(
            ASRConfig(),
            recognizer,  # type: ignore[arg-type]
            vad_model_dir=tmp_path / "missing",
            vad_factory=lambda **_kwargs: _Vad(),
        )

    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"model")
    streamer = SenseVoiceVadStreamer(
        ASRConfig(),
        recognizer,  # type: ignore[arg-type]
        vad_model_dir=model,
        vad_factory=lambda **_kwargs: _Vad(),
    )
    with pytest.raises(ValueError, match="2 字节的倍数"):
        streamer.accept_chunk(b"\x00")
    streamer.flush()
    with pytest.raises(RuntimeError, match="已经结束"):
        streamer.accept_chunk(b"\x00\x00")


def test_vad_buffers_and_zero_pads_subwindow_on_flush(tmp_path: Path) -> None:
    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"model")
    vad = _Vad()
    recognizer = _SegmentRecognizer()
    streamer = SenseVoiceVadStreamer(
        ASRConfig(),
        recognizer,  # type: ignore[arg-type]
        vad_model_dir=model,
        vad_factory=lambda **_kwargs: vad,
    )

    assert streamer.accept_chunk(np.full(100, 1000, dtype="<i2").tobytes()) == ()
    assert vad.windows == []
    final = streamer.flush()

    assert len(vad.windows) == 1
    assert vad.windows[0][:100].tolist() == pytest.approx([1000 / 32768] * 100)
    assert np.count_nonzero(vad.windows[0][100:]) == 0
    assert final.confirmed_segments == ("甲",)


def test_vad_ignores_blank_completed_segments_and_callable_speech_state(
    tmp_path: Path,
) -> None:
    class CallableVad(_Vad):
        def is_speech_detected(self) -> bool:  # type: ignore[override]
            return False

    class BlankRecognizer:
        def transcribe_samples(self, samples: np.ndarray) -> str:
            return "   "

    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"model")
    vad = CallableVad()
    streamer = SenseVoiceVadStreamer(
        ASRConfig(),
        BlankRecognizer(),  # type: ignore[arg-type]
        vad_model_dir=model,
        vad_factory=lambda **_kwargs: vad,
    )

    assert streamer.accept_chunk(_pcm(1_000)) == ()
    vad.complete()
    assert streamer.accept_chunk(b"") == ()
    assert streamer.flush().confirmed_segments == ()


def test_model_resolvers_are_lazy_and_cached_for_both_providers(tmp_path: Path) -> None:
    sense_dir = _sensevoice_model(tmp_path / "sense")
    qwen_dir = tmp_path / "qwen"
    qwen_dir.mkdir()
    _qwen_model(qwen_dir)
    calls: list[str] = []

    def resolve(model_id: str) -> Path:
        calls.append(model_id)
        return sense_dir if model_id == "sensevoice-int8" else qwen_dir

    sense = SenseVoiceProvider(ASRConfig(), model_resolver=resolve)
    qwen = Qwen3SherpaProvider(ASRConfig(), model_resolver=resolve)
    assert calls == []

    assert sense.missing_model_files() == []
    assert sense.model_dir is sense_dir
    assert qwen.missing_model_files() == []
    assert qwen.model_dir is qwen_dir
    assert calls == ["sensevoice-int8", "qwen3-asr-0.6b-int8"]


def test_default_sherpa_factories_delegate_to_binding_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[Any]] = {"sense": [], "qwen": [], "silero": [], "vad": []}
    sense_recognizer = _Recognizer(["默认 SenseVoice"])
    qwen_recognizer = _Recognizer(["默认 Qwen"])
    vad = _Vad()

    class OfflineRecognizer:
        @staticmethod
        def from_sense_voice(**kwargs: Any) -> _Recognizer:
            calls["sense"].append(kwargs)
            return sense_recognizer

        @staticmethod
        def from_qwen3_asr(**kwargs: Any) -> _Recognizer:
            calls["qwen"].append(kwargs)
            return qwen_recognizer

    class SileroVadModelConfig:
        def __init__(self, **kwargs: Any) -> None:
            calls["silero"].append(kwargs)

    class VadModelConfig:
        def __init__(self, **kwargs: Any) -> None:
            calls["vad"].append(kwargs)

    fake_binding = SimpleNamespace(
        OfflineRecognizer=OfflineRecognizer,
        SileroVadModelConfig=SileroVadModelConfig,
        VadModelConfig=VadModelConfig,
        VoiceActivityDetector=lambda config, buffer_size_in_seconds: vad,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_binding)

    sense_dir = _sensevoice_model(tmp_path / "sense")
    sense = SenseVoiceProvider(
        ASRConfig(),
        model_dir=sense_dir,
        numpy_module=np,
    )
    assert sense.transcribe_samples(np.zeros(8, dtype=np.float32)) == "默认 SenseVoice"

    qwen_dir = tmp_path / "qwen"
    qwen_dir.mkdir()
    _qwen_model(qwen_dir)
    wav_path = tmp_path / "audio.wav"
    _write_wav(wav_path, np.zeros(8, dtype=np.int16))
    qwen = Qwen3SherpaProvider(
        ASRConfig(),
        model_dir=qwen_dir,
        numpy_module=np,
    )
    assert qwen.transcribe(wav_path).text == "默认 Qwen"

    vad_model = tmp_path / "silero_vad.onnx"
    vad_model.write_bytes(b"model")
    streamer = SenseVoiceVadStreamer(
        ASRConfig(),
        sense,
        vad_model_dir=vad_model,
        numpy_module=np,
    )

    assert streamer.confirmed_segments == ()
    assert calls["sense"] and calls["qwen"]
    assert calls["silero"] == [
        {
            "model": str(vad_model),
            "threshold": 0.2,
            "min_silence_duration": 0.5,
            "min_speech_duration": 0.2,
            "window_size": 512,
            "max_speech_duration": 20.0,
        }
    ]
    assert len(calls["vad"]) == 1
