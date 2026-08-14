from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .capture import RawCaptureSession, Recorder
from .clipboard import ClipboardSnapshotService
from .cloud_asr import CloudASRProvider, CloudVadStreamer
from .config import Config
from .events import RecognitionEvent, RecognitionTranscript
from .history import CompletedHistoryRecord, HistoryStore
from .inject import InjectionResult, TextInjector
from .model_manager import ModelManager
from .modes import Mode, ModesRepository
from .paths import AppPaths
from .processing import (
    OllamaProcessor,
    OpenAICompatibleProcessor,
    TextProcessRequest,
    TextProcessResult,
    TextProcessor,
)
from .providers import (
    ASRProvider,
    Qwen3SherpaProvider,
    RecognitionResult,
    SenseVoiceProvider,
    SenseVoiceVadStreamer,
    create_provider,
)
from .session import RecognitionSession
from .vocabulary import VocabularyService


@dataclass(frozen=True)
class PipelineResult:
    recognition: RecognitionResult
    injection: InjectionResult | None


@dataclass(frozen=True)
class RecognitionRequest:
    mode: str | None = None
    inject: bool = True
    event_sink: Callable[[RecognitionEvent], None] | None = None


class VoiceInputPipeline:
    """批量和实时识别的依赖装配边界。"""

    def __init__(
        self,
        config: Config,
        provider: ASRProvider | None = None,
        injector: TextInjector | None = None,
        recorder: Recorder | None = None,
        vocabulary: VocabularyService | None = None,
        *,
        paths: AppPaths | None = None,
        modes: ModesRepository | None = None,
        history: HistoryStore | None = None,
        model_manager: ModelManager | None = None,
        clipboard: ClipboardSnapshotService | None = None,
        processor: TextProcessor | None = None,
        capture_factory: Callable[[], object] | None = None,
        streamer_factory: Callable[..., object] | None = None,
        calibrator: ASRProvider | None = None,
    ) -> None:
        self.config = config
        self.paths = paths or AppPaths.from_environment()
        self.vocabulary = vocabulary or VocabularyService(self.paths)
        self.model_manager = model_manager or ModelManager(
            self.paths,
            active_model_ids=self._active_model_ids,
        )
        self.provider = provider or create_provider(
            config.asr,
            model_resolver=self.model_manager.resolve,
            hotwords=self.vocabulary.list_hotwords(),
            cloud=self.config.cloud,
        )
        self.injector = injector or TextInjector(config.inject)
        self.recorder = recorder or Recorder(config.capture)
        self.modes = modes or ModesRepository(self.paths)
        self.history = (
            history
            if history is not None
            else (HistoryStore(self.paths) if config.history.enabled else None)
        )
        self.clipboard = clipboard or ClipboardSnapshotService()
        self.processor = processor or _create_processor(config)
        self._capture_factory = capture_factory or (lambda: RawCaptureSession(config.capture))
        self._streamer_factory = streamer_factory or SenseVoiceVadStreamer
        self._live_sensevoice: SenseVoiceProvider | None = None
        self._live_sensevoice_path: Path | None = None
        self._configured_calibrator: ASRProvider | None = calibrator
        self._live_calibrator: ASRProvider | None = None
        self._live_calibrator_path: Path | None = None
        self._live_calibrator_hotwords: tuple[str, ...] | None = None
        self._live_calibrator_ready = False

    def run_once(
        self,
        audio_path: Path | None = None,
        record_seconds: float | None = None,
        inject: bool = True,
    ) -> PipelineResult:
        if audio_path is None:
            if record_seconds is None:
                raise ValueError("必须提供 audio_path 或 record_seconds")
            audio_path = self.recorder.record_seconds(record_seconds)

        raw = self.provider.transcribe(audio_path)
        snippet_text = self.vocabulary.apply_snippets(raw.text)
        mode = self.modes.resolve(None)
        processed = self._process_text(snippet_text, mode, selected="", clipboard="")
        recognition = RecognitionResult(
            text=processed.text,
            backend=raw.backend,
            draft_text=raw.draft_text,
        )
        injection = self.injector.inject(recognition.text) if inject else None
        self._write_history(
            raw_text=raw.text,
            final_text=recognition.text,
            mode=mode,
            processing=processed,
            backend=raw.backend,
            model=self._model_for_backend(raw.backend),
            status=(
                "injection-failed" if injection is not None and not injection.ok else "completed"
            ),
        )
        return PipelineResult(recognition=recognition, injection=injection)

    def create_session(self, request: RecognitionRequest) -> RecognitionSession:
        self.vocabulary.reload()
        mode = self.modes.resolve(request.mode)
        snapshot = self.clipboard.snapshot()
        started_at = time.monotonic()
        processing_state: dict[str, TextProcessResult | None] = {"result": None}
        session_state: dict[str, RecognitionSession] = {}
        warning_state: dict[str, bool] = {"cloud_segments": False}

        if self.config.asr.streaming_backend == "cloud-vad":
            streamer = CloudVadStreamer(
                self.config.asr,
                CloudASRProvider(self.config.cloud),
                model_resolver=self.model_manager.resolve,
            )
        else:
            sensevoice = self._get_live_sensevoice()
            streamer = self._streamer_factory(
                self.config.asr,
                sensevoice,
                model_resolver=self.model_manager.resolve,
            )
        calibrator = self._get_live_calibrator()

        def process_text(raw_text: str) -> str:
            snippet_text = self.vocabulary.apply_snippets(raw_text)
            result = self._process_text(
                snippet_text,
                mode,
                selected=snapshot.selected,
                clipboard=snapshot.clipboard,
            )
            processing_state["result"] = result
            failed_segments = getattr(streamer, "failed_segment_count", 0)
            if failed_segments and not warning_state["cloud_segments"]:
                warning_state["cloud_segments"] = True
                session_state["session"].publish_warning(
                    f"云端识别 {failed_segments} 个语音段失败并已跳过，最终文本仅包含成功片段。"
                )
            if result.warning:
                session_state["session"].publish_warning(result.warning)
            return result.text

        def write_history(
            raw: RecognitionTranscript,
            final: RecognitionTranscript,
            injection: InjectionResult | None,
        ) -> None:
            result = processing_state["result"]
            status = "completed"
            if result is not None and not result.succeeded:
                status = result.status
            if injection is not None and not injection.ok:
                status = "injection-failed"
            self._write_history(
                raw_text=raw.authoritative_text,
                final_text=final.authoritative_text,
                mode=mode,
                processing=result,
                backend=final.backend,
                model=self._model_for_backend(final.backend),
                status=status,
                duration_seconds=max(0.0, time.monotonic() - started_at),
            )

        def write_failure(message: str) -> None:
            self._write_history(
                raw_text="",
                final_text="",
                mode=mode,
                processing=None,
                backend=self.config.asr.streaming_backend,
                model=self.config.asr.sensevoice_model_id,
                status="failed",
                duration_seconds=max(0.0, time.monotonic() - started_at),
            )

        session_kwargs: dict[str, object] = {
            "capture_factory": self._capture_factory,
            "streamer": streamer,
            "calibrator": calibrator,
            "calibration_backend": "cloud" if self.config.asr.final_backend == "cloud" else "hybrid",
            "processor": process_text,
            "history_writer": write_history if self.history is not None else None,
            "failed_history_writer": write_failure if self.history is not None else None,
            "injector": self.injector.inject if request.inject else None,
            "event_sink": request.event_sink,
        }
        if snapshot.warnings:
            session_kwargs["startup_warnings"] = snapshot.warnings
        session = RecognitionSession(**session_kwargs)  # type: ignore[arg-type]
        session_state["session"] = session
        return session

    def _process_text(
        self,
        text: str,
        mode: Mode,
        *,
        selected: str,
        clipboard: str,
    ) -> TextProcessResult:
        request = TextProcessRequest(
            text=text,
            mode=mode,
            selected=selected,
            clipboard=clipboard,
        )
        if mode.id == "quick" or self.processor is None:
            return TextProcessResult(text, "bypassed", "none")
        return self.processor.process(request)

    def _get_live_sensevoice(self) -> SenseVoiceProvider:
        model_dir = self.model_manager.resolve(self.config.asr.sensevoice_model_id)
        if self._live_sensevoice is None or self._live_sensevoice_path != model_dir:
            self._live_sensevoice = SenseVoiceProvider(
                self.config.asr,
                model_dir=model_dir,
            )
            self._live_sensevoice_path = model_dir
        return self._live_sensevoice

    def _get_live_calibrator(self) -> ASRProvider | None:
        model_dir: Path | None = None
        hotwords: tuple[str, ...] | None = None
        if self._configured_calibrator is not None:
            if self._live_calibrator_ready:
                return self._live_calibrator
            calibrator = self._configured_calibrator
        elif self.config.asr.final_backend == "sensevoice":
            if self._live_calibrator_ready:
                return self._live_calibrator
            calibrator = None
        elif self.config.asr.final_backend == "qwen3-sherpa":
            model_dir = self.model_manager.resolve(self.config.asr.qwen3_model_id)
            hotwords = self.vocabulary.list_hotwords()
            if (
                self._live_calibrator_ready
                and self._live_calibrator_path == model_dir
                and self._live_calibrator_hotwords == hotwords
            ):
                return self._live_calibrator
            calibrator = Qwen3SherpaProvider(
                self.config.asr,
                model_dir=model_dir,
                hotwords=hotwords,
            )
        elif self.config.asr.final_backend == "cloud":
            if self._live_calibrator_ready:
                return self._live_calibrator
            calibrator = CloudASRProvider(self.config.cloud)
        else:
            raise ValueError(f"不支持的最终识别后端：{self.config.asr.final_backend}")

        self._live_calibrator = calibrator
        self._live_calibrator_path = model_dir
        self._live_calibrator_hotwords = hotwords
        self._live_calibrator_ready = True
        return calibrator

    def _write_history(
        self,
        *,
        raw_text: str,
        final_text: str,
        mode: Mode,
        processing: TextProcessResult | None,
        backend: str,
        model: str | None,
        status: str,
        duration_seconds: float | None = None,
    ) -> None:
        if self.history is None:
            return
        processed_text = None
        if processing is not None and processing.status == "success":
            processed_text = processing.text
        self.history.insert(
            CompletedHistoryRecord(
                raw_text=raw_text,
                final_text=final_text,
                duration_seconds=duration_seconds,
                processing_mode=mode.id,
                processed_text=processed_text,
                status=status,
                character_count=len(final_text),
                asr_provider=backend,
                asr_model=model,
            )
        )

    def _model_for_backend(self, backend: str) -> str | None:
        if backend in {"qwen3-sherpa", "hybrid"}:
            return self.config.asr.qwen3_model_id
        if backend in {"sensevoice", "sensevoice-vad", "hybrid-fallback"}:
            return self.config.asr.sensevoice_model_id
        if backend in {"cloud", "cloud-vad"}:
            return self.config.cloud.model
        return None

    def _active_model_ids(self) -> tuple[str, ...]:
        return (
            self.config.asr.sensevoice_model_id,
            self.config.asr.vad_model_id,
            self.config.asr.qwen3_model_id,
        )


def _create_processor(config: Config) -> TextProcessor | None:
    settings = config.processing
    if settings.provider == "none":
        return None
    arguments = {
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "timeout_seconds": settings.timeout_seconds,
    }
    if settings.provider == "openai-compatible":
        return OpenAICompatibleProcessor(**arguments)
    if settings.provider == "ollama":
        return OllamaProcessor(**arguments)
    raise ValueError(f"不支持的文本处理提供方：{settings.provider}")
