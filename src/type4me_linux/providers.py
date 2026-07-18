from __future__ import annotations

import re
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import ASRConfig
from .events import RecognitionTranscript

SAMPLE_RATE = 16_000
VAD_WINDOW_SAMPLES = 512
PARTIAL_INTERVAL_SAMPLES = 3_200


@dataclass(frozen=True)
class RecognitionResult:
    text: str
    backend: str
    draft_text: str | None = None


class ASRProvider(Protocol):
    def transcribe(self, wav_path: Path) -> RecognitionResult:
        raise NotImplementedError


class OfflineStream(Protocol):
    result: Any

    def accept_waveform(self, sample_rate: int, samples: Any) -> None: ...


class OfflineRecognizer(Protocol):
    def create_stream(self) -> OfflineStream: ...

    def decode_stream(self, stream: OfflineStream) -> None: ...


ModelResolver = Callable[[str], Path]
RecognizerFactory = Callable[..., OfflineRecognizer]
VadFactory = Callable[..., Any]


class FakeProvider:
    def __init__(self, text: str = "测试语音输入") -> None:
        self.text = text

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        return RecognitionResult(text=self.text, backend="fake")


class SenseVoiceProvider:
    def __init__(
        self,
        config: ASRConfig,
        *,
        model_dir: Path | None = None,
        model_resolver: ModelResolver | None = None,
        recognizer_factory: RecognizerFactory | None = None,
        numpy_module: Any | None = None,
    ) -> None:
        self.config = config
        self._model_dir = model_dir
        self._model_resolver = model_resolver or _default_model_resolver
        self._recognizer_factory = recognizer_factory or _sensevoice_recognizer_factory
        self._numpy_module = numpy_module
        self._recognizer: OfflineRecognizer | None = None

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        samples = _load_wav_pcm16(wav_path, self._numpy())
        return RecognitionResult(
            text=self.transcribe_samples(samples),
            backend="sensevoice",
        )

    def transcribe_samples(self, samples: Any) -> str:
        recognizer = self._get_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        recognizer.decode_stream(stream)
        return _result_text(stream.result)

    def missing_model_files(self) -> list[Path]:
        return [path for path in (self.model_path, self.tokens_path) if not path.is_file()]

    @property
    def model_dir(self) -> Path:
        if self._model_dir is None:
            self._model_dir = self._model_resolver(self.config.sensevoice_model_id)
        return self._model_dir

    @property
    def model_path(self) -> Path:
        return self.model_dir / "model.int8.onnx"

    @property
    def tokens_path(self) -> Path:
        return self.model_dir / "tokens.txt"

    def _get_recognizer(self) -> OfflineRecognizer:
        if self._recognizer is None:
            self._ensure_model_files()
            self._recognizer = self._recognizer_factory(
                model=str(self.model_path),
                tokens=str(self.tokens_path),
                language=self.config.language,
                use_itn=True,
                num_threads=self.config.num_threads,
                provider=self.config.provider,
            )
        return self._recognizer

    def _ensure_model_files(self) -> None:
        missing = self.missing_model_files()
        if missing:
            joined = "、".join(str(path) for path in missing)
            raise FileNotFoundError(f"SenseVoice 模型文件缺失：{joined}")

    def _numpy(self) -> Any:
        if self._numpy_module is None:
            self._numpy_module = _import_numpy()
        return self._numpy_module


class Qwen3SherpaProvider:
    def __init__(
        self,
        config: ASRConfig,
        *,
        model_dir: Path | None = None,
        hotwords: Iterable[str] = (),
        model_resolver: ModelResolver | None = None,
        recognizer_factory: RecognizerFactory | None = None,
        numpy_module: Any | None = None,
    ) -> None:
        self.config = config
        self.hotwords = tuple(word.strip() for word in hotwords if word.strip())
        self._model_dir = model_dir
        self._model_resolver = model_resolver or _default_model_resolver
        self._recognizer_factory = recognizer_factory or _qwen_recognizer_factory
        self._numpy_module = numpy_module
        self._recognizer: OfflineRecognizer | None = None

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        samples = _load_wav_pcm16(wav_path, self._numpy())
        recognizer = self._get_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        recognizer.decode_stream(stream)
        return RecognitionResult(
            text=_sanitize_qwen_output(_result_text(stream.result), self.hotwords),
            backend="qwen3-sherpa",
        )

    def missing_model_files(self) -> list[Path]:
        required = (
            self.conv_frontend_path,
            self.encoder_path,
            self.decoder_path,
            self.tokenizer_path / "merges.txt",
            self.tokenizer_path / "tokenizer_config.json",
            self.tokenizer_path / "vocab.json",
        )
        return [path for path in required if not path.is_file()]

    @property
    def model_dir(self) -> Path:
        if self._model_dir is None:
            self._model_dir = self._model_resolver(self.config.qwen3_model_id)
        return self._model_dir

    @property
    def conv_frontend_path(self) -> Path:
        return self.model_dir / "conv_frontend.onnx"

    @property
    def encoder_path(self) -> Path:
        return self.model_dir / "encoder.int8.onnx"

    @property
    def decoder_path(self) -> Path:
        return self.model_dir / "decoder.int8.onnx"

    @property
    def tokenizer_path(self) -> Path:
        return self.model_dir / "tokenizer"

    def _get_recognizer(self) -> OfflineRecognizer:
        if self._recognizer is None:
            self._ensure_model_files()
            self._recognizer = self._recognizer_factory(
                conv_frontend=str(self.conv_frontend_path),
                encoder=str(self.encoder_path),
                decoder=str(self.decoder_path),
                tokenizer=str(self.tokenizer_path),
                hotwords=",".join(self.hotwords),
                max_new_tokens=512,
                num_threads=self.config.num_threads,
                provider=self.config.provider,
            )
        return self._recognizer

    def _ensure_model_files(self) -> None:
        missing = self.missing_model_files()
        if missing:
            joined = "、".join(str(path) for path in missing)
            raise FileNotFoundError(f"Qwen3-ASR 模型文件缺失：{joined}")

    def _numpy(self) -> Any:
        if self._numpy_module is None:
            self._numpy_module = _import_numpy()
        return self._numpy_module


class HybridProvider:
    def __init__(self, sensevoice: ASRProvider, qwen: ASRProvider) -> None:
        self.sensevoice = sensevoice
        self.qwen = qwen

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        draft = self.sensevoice.transcribe(wav_path)
        try:
            final = self.qwen.transcribe(wav_path)
        except Exception:
            return RecognitionResult(
                text=draft.text,
                backend="hybrid-fallback",
                draft_text=draft.text,
            )
        return RecognitionResult(
            text=final.text,
            backend="hybrid",
            draft_text=draft.text,
        )


def create_provider(
    config: ASRConfig,
    *,
    model_resolver: ModelResolver | None = None,
    hotwords: Iterable[str] = (),
    sensevoice_factory: RecognizerFactory | None = None,
    qwen_factory: RecognizerFactory | None = None,
    numpy_module: Any | None = None,
) -> ASRProvider:
    backend = config.batch_backend.lower()
    if backend == "fake":
        return FakeProvider()

    sensevoice = SenseVoiceProvider(
        config,
        model_resolver=model_resolver,
        recognizer_factory=sensevoice_factory,
        numpy_module=numpy_module,
    )
    if backend == "sensevoice":
        return sensevoice

    qwen = Qwen3SherpaProvider(
        config,
        hotwords=hotwords,
        model_resolver=model_resolver,
        recognizer_factory=qwen_factory,
        numpy_module=numpy_module,
    )
    if backend == "qwen3-sherpa":
        return qwen
    if backend == "hybrid":
        return HybridProvider(sensevoice, qwen)
    raise ValueError(f"不支持的 ASR 后端：{config.batch_backend}")


class SenseVoiceVadStreamer:
    """用 Silero VAD 分段，并用离线 SenseVoice 模拟流式识别。"""

    def __init__(
        self,
        config: ASRConfig,
        sensevoice: SenseVoiceProvider,
        *,
        vad_model_dir: Path | None = None,
        model_resolver: ModelResolver | None = None,
        vad_factory: VadFactory | None = None,
        numpy_module: Any | None = None,
    ) -> None:
        self.config = config
        self.sensevoice = sensevoice
        self._numpy_module = numpy_module
        self._pending: Any | None = None
        self._confirmed: list[str] = []
        self._last_partial = ""
        self._samples_received = 0
        self._next_partial_at = PARTIAL_INTERVAL_SAMPLES
        self._flushed: RecognitionTranscript | None = None

        resolver = model_resolver or _default_model_resolver
        model_root = vad_model_dir or resolver(config.vad_model_id)
        model_path = model_root if model_root.is_file() else model_root / "silero_vad.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(f"Silero VAD 模型文件缺失：{model_path}")
        factory = vad_factory or _vad_factory
        self._vad = factory(
            model=str(model_path),
            threshold=config.vad_threshold,
            min_silence_duration=config.vad_min_silence_seconds,
            min_speech_duration=config.vad_min_speech_seconds,
            window_size=VAD_WINDOW_SAMPLES,
            max_speech_duration=config.vad_max_speech_seconds,
            sample_rate=SAMPLE_RATE,
        )

    @property
    def confirmed_segments(self) -> tuple[str, ...]:
        return tuple(self._confirmed)

    def accept_chunk(self, pcm16le: bytes) -> tuple[RecognitionTranscript, ...]:
        if self._flushed is not None:
            raise RuntimeError("VAD 流已经结束，不能继续写入音频")
        if len(pcm16le) % 2:
            raise ValueError("PCM16-LE 数据长度必须是 2 字节的倍数")

        np = self._numpy()
        samples = _pcm16_to_float32(pcm16le, np)
        self._samples_received += int(samples.size)
        if self._pending is not None and self._pending.size:
            samples = np.concatenate((self._pending, samples))
        complete = (int(samples.size) // VAD_WINDOW_SAMPLES) * VAD_WINDOW_SAMPLES
        for offset in range(0, complete, VAD_WINDOW_SAMPLES):
            self._vad.accept_waveform(samples[offset : offset + VAD_WINDOW_SAMPLES])
        self._pending = samples[complete:].copy()

        emitted = self._drain_completed()
        if self._samples_received >= self._next_partial_at:
            while self._next_partial_at <= self._samples_received:
                self._next_partial_at += PARTIAL_INTERVAL_SAMPLES
            partial = self._decode_current_segment() if self._speech_detected() else ""
            if partial != self._last_partial:
                self._last_partial = partial
                emitted.append(self._transcript(partial=partial, final=False))
        return tuple(emitted)

    def flush(self) -> RecognitionTranscript:
        if self._flushed is not None:
            return self._flushed
        np = self._numpy()
        if self._pending is not None and self._pending.size:
            padded = np.zeros(VAD_WINDOW_SAMPLES, dtype=np.float32)
            padded[: self._pending.size] = self._pending
            self._vad.accept_waveform(padded)
            self._pending = np.empty(0, dtype=np.float32)
        self._vad.flush()
        self._drain_completed()

        remainder = self._decode_current_segment()
        if remainder:
            self._append_confirmed(remainder)
        self._last_partial = ""
        self._flushed = self._transcript(partial="", final=True)
        return self._flushed

    def _drain_completed(self) -> list[RecognitionTranscript]:
        emitted: list[RecognitionTranscript] = []
        while not self._vad.empty():
            segment = self._vad.front
            text = self._decode_samples(segment.samples)
            self._vad.pop()
            if text and self._append_confirmed(text):
                self._last_partial = ""
                emitted.append(self._transcript(partial="", final=False))
        return emitted

    def _decode_current_segment(self) -> str:
        segment = self._vad.current_segment
        samples = getattr(segment, "samples", None)
        if samples is None or len(samples) == 0:
            return ""
        return self._decode_samples(samples)

    def _decode_samples(self, samples: Any) -> str:
        np = self._numpy()
        values = np.asarray(samples, dtype=np.float32)
        if values.size == 0:
            return ""
        return self.sensevoice.transcribe_samples(values).strip()

    def _append_confirmed(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False
        self._confirmed.append(normalized)
        return True

    def _speech_detected(self) -> bool:
        value = self._vad.is_speech_detected
        return bool(value() if callable(value) else value)

    def _transcript(self, *, partial: str, final: bool) -> RecognitionTranscript:
        authoritative = "".join(self._confirmed)
        return RecognitionTranscript(
            confirmed_segments=tuple(self._confirmed),
            partial_text=partial,
            authoritative_text=authoritative,
            is_final=final,
            backend="sensevoice-vad",
        )

    def _numpy(self) -> Any:
        if self._numpy_module is None:
            self._numpy_module = _import_numpy()
        return self._numpy_module


def _load_wav_pcm16(path: Path, np: Any) -> Any:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getframerate() != SAMPLE_RATE:
            raise ValueError(f"WAV 采样率必须为 {SAMPLE_RATE} Hz")
        if wav_file.getnchannels() != 1:
            raise ValueError("WAV 必须是单声道")
        if wav_file.getsampwidth() != 2 or wav_file.getcomptype() != "NONE":
            raise ValueError("WAV 必须是未压缩的 PCM16")
        frames = wav_file.readframes(wav_file.getnframes())
    return _pcm16_to_float32(frames, np)


def _pcm16_to_float32(data: bytes, np: Any) -> Any:
    pcm = np.frombuffer(data, dtype="<i2")
    return pcm.astype(np.float32) / np.float32(32768.0)


def _result_text(result: Any) -> str:
    text = getattr(result, "text", None)
    if not isinstance(text, str):
        raise ValueError("sherpa-onnx 识别结果缺少文本")
    return text.strip()


def _sanitize_qwen_output(text: str, hotwords: tuple[str, ...]) -> str:
    cleaned = text.strip()
    cleaned = re.sub(
        r"^(转写|语音转写|transcription|transcript)\s*[:：]\s*",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"^(以下是|下面是).{0,12}(识别|转写).{0,8}[:：]\s*", "", cleaned)
    for hotword in hotwords:
        patterns = (
            rf"(热词|hotwords?)\s*[:：]\s*{re.escape(hotword)}",
            rf"{re.escape(hotword)}\s*(是热词|为热词)",
        )
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()


def _sensevoice_recognizer_factory(**kwargs: Any) -> OfflineRecognizer:
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)


def _qwen_recognizer_factory(**kwargs: Any) -> OfflineRecognizer:
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(**kwargs)


def _vad_factory(**kwargs: Any) -> Any:
    import sherpa_onnx

    silero = sherpa_onnx.SileroVadModelConfig(
        model=kwargs["model"],
        threshold=kwargs["threshold"],
        min_silence_duration=kwargs["min_silence_duration"],
        min_speech_duration=kwargs["min_speech_duration"],
        window_size=kwargs["window_size"],
        max_speech_duration=kwargs["max_speech_duration"],
    )
    config = sherpa_onnx.VadModelConfig(
        silero_vad=silero,
        sample_rate=kwargs["sample_rate"],
    )
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=100)


def _default_model_resolver(model_id: str) -> Path:
    from .model_manager import ModelManager
    from .paths import AppPaths

    return ModelManager(AppPaths.from_environment()).resolve(model_id)


def _import_numpy() -> Any:
    import numpy

    return numpy
