from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .capture import RawCaptureSession, Recorder
from .clipboard import ClipboardSnapshotService
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
        self._calibrator = calibrator

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
        mode = self.modes.resolve(request.mode)
        snapshot = self.clipboard.snapshot()
        started_at = time.monotonic()
        processing_state: dict[str, TextProcessResult | None] = {"result": None}
        session_state: dict[str, RecognitionSession] = {}

        sensevoice = SenseVoiceProvider(
            self.config.asr,
            model_resolver=self.model_manager.resolve,
        )
        streamer = self._streamer_factory(
            self.config.asr,
            sensevoice,
            model_resolver=self.model_manager.resolve,
        )
        calibrator = self._live_calibrator()

        def process_text(raw_text: str) -> str:
            snippet_text = self.vocabulary.apply_snippets(raw_text)
            result = self._process_text(
                snippet_text,
                mode,
                selected=snapshot.selected,
                clipboard=snapshot.clipboard,
            )
            processing_state["result"] = result
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

    def _live_calibrator(self) -> ASRProvider | None:
        if self._calibrator is not None:
            return self._calibrator
        if self.config.asr.final_backend in {"", "none", "sensevoice"}:
            return None
        if self.config.asr.final_backend != "qwen3-sherpa":
            raise ValueError(f"不支持的最终识别后端：{self.config.asr.final_backend}")
        return Qwen3SherpaProvider(
            self.config.asr,
            hotwords=self.vocabulary.list_hotwords(),
            model_resolver=self.model_manager.resolve,
        )

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
