from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .capture import Recorder
from .config import Config
from .hotwords import apply_snippets
from .inject import InjectionResult, TextInjector
from .providers import ASRProvider, RecognitionResult, create_provider


@dataclass(frozen=True)
class PipelineResult:
    recognition: RecognitionResult
    injection: InjectionResult | None


class VoiceInputPipeline:
    def __init__(
        self,
        config: Config,
        provider: ASRProvider | None = None,
        injector: TextInjector | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.config = config
        self.provider = provider or create_provider(config.asr)
        self.injector = injector or TextInjector(config.inject)
        self.recorder = recorder or Recorder(config.capture)

    def run_once(
        self,
        audio_path: Path | None = None,
        record_seconds: float | None = None,
        inject: bool = True,
    ) -> PipelineResult:
        if audio_path is None:
            if record_seconds is None:
                raise ValueError("audio_path or record_seconds is required")
            audio_path = self.recorder.record_seconds(record_seconds)
        recognition = self.provider.transcribe(audio_path)
        text = apply_snippets(recognition.text, self.config.snippets)
        recognition = RecognitionResult(
            text=text,
            backend=recognition.backend,
            draft_text=recognition.draft_text,
        )
        injection = self.injector.inject(text) if inject else None
        return PipelineResult(recognition=recognition, injection=injection)

