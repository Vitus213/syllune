from __future__ import annotations

from pathlib import Path
import pytest
import type4me_linux.pipeline as pipeline_module

from type4me_linux.clipboard import ClipboardSnapshot
from type4me_linux.config import ASRConfig, Config, HistoryConfig, ProcessingConfig
from type4me_linux.events import RecognitionTranscript
from type4me_linux.inject import InjectionResult
from type4me_linux.modes import ModesRepository
from type4me_linux.paths import AppPaths
from type4me_linux.pipeline import (
    RecognitionRequest,
    VoiceInputPipeline,
    _create_processor,
)
from type4me_linux.processing import (
    OllamaProcessor,
    OpenAICompatibleProcessor,
    TextProcessResult,
)
from type4me_linux.providers import FakeProvider, Qwen3SherpaProvider, RecognitionResult
from type4me_linux.vocabulary import VocabularyService


class _Injector:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def inject(self, text: str) -> InjectionResult:
        self.texts.append(text)
        return InjectionResult("test", True)


class _History:
    def __init__(self) -> None:
        self.records: list[object] = []

    def insert(self, record: object) -> object:
        self.records.append(record)
        return record


class _Models:
    def resolve(self, model_id: str) -> Path:
        return Path("/unused") / model_id


class _Clipboard:
    def __init__(self, warnings: tuple[str, ...] = ()) -> None:
        self.warnings = warnings
        self.calls = 0

    def snapshot(self) -> ClipboardSnapshot:
        self.calls += 1
        return ClipboardSnapshot("剪贴板", "选区", self.warnings)


class _Capture:
    def __init__(self, wav_path: Path, chunks: tuple[bytes, ...] = (b"pcm",)) -> None:
        self.wav_path = wav_path
        self.chunks = chunks
        self.started = False
        self.stopped = False
        self.cancelled = False
        self.released = False

    def start(self) -> None:
        self.started = True
        self.wav_path.write_bytes(b"RIFF")

    def iter_chunks(self):  # type: ignore[no-untyped-def]
        yield from self.chunks

    def stop(self) -> Path:
        self.stopped = True
        return self.wav_path

    def cancel(self) -> None:
        self.cancelled = True

    def release_wav(self) -> None:
        self.released = True
        self.wav_path.unlink(missing_ok=True)


class _Streamer:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail

    def accept_chunk(self, chunk: bytes) -> tuple[RecognitionTranscript, ...]:
        if self.fail is not None:
            raise self.fail
        return (RecognitionTranscript((), "局部", "局部", False, "sensevoice-vad"),)

    def flush(self) -> RecognitionTranscript:
        return RecognitionTranscript(("我的邮箱",), "", "我的邮箱", True, "sensevoice-vad")


class _Calibrator:
    def __init__(self, result: str | Exception) -> None:
        self.result = result

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        if isinstance(self.result, Exception):
            raise self.result
        return RecognitionResult(self.result, "qwen3-sherpa")


class _CancellingCalibrator:
    def __init__(self) -> None:
        self.cancel = lambda: None

    def transcribe(self, wav_path: Path) -> RecognitionResult:
        self.cancel()
        return RecognitionResult("不得发布", "qwen3-sherpa")


class _Processor:
    def __init__(self, result: TextProcessResult) -> None:
        self.result = result

    def process(self, request):  # type: ignore[no-untyped-def]
        return self.result


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=None,
    )


def _vocabulary(tmp_path: Path) -> VocabularyService:
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    defaults.joinpath("hotwords.json").write_text("[]", encoding="utf-8")
    defaults.joinpath("snippets.json").write_text(
        '{"我的邮箱": "me@example.com"}', encoding="utf-8"
    )
    return VocabularyService(_paths(tmp_path), defaults)


def _pipeline(
    tmp_path: Path,
    *,
    capture: _Capture,
    streamer: _Streamer,
    calibrator: object,
    injector: _Injector,
    history: _History,
    clipboard: _Clipboard | None = None,
    processor: _Processor | None = None,
) -> VoiceInputPipeline:
    paths = _paths(tmp_path)
    return VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake")),
        provider=FakeProvider("我的邮箱"),
        injector=injector,  # type: ignore[arg-type]
        vocabulary=_vocabulary(tmp_path),
        paths=paths,
        modes=ModesRepository(paths),
        history=history,  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
        clipboard=clipboard or _Clipboard(),  # type: ignore[arg-type]
        capture_factory=lambda: capture,
        streamer_factory=lambda *_args, **_kwargs: streamer,
        calibrator=calibrator,  # type: ignore[arg-type]
        processor=processor,  # type: ignore[arg-type]
    )


def test_run_once_owns_vocabulary_injection_and_history(tmp_path: Path) -> None:
    injector = _Injector()
    history = _History()
    paths = _paths(tmp_path)
    pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake")),
        provider=FakeProvider("我的邮箱"),
        injector=injector,  # type: ignore[arg-type]
        vocabulary=_vocabulary(tmp_path),
        paths=paths,
        modes=ModesRepository(paths),
        history=history,  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
    )

    result = pipeline.run_once(audio_path=tmp_path / "fake.wav")

    assert result.recognition == RecognitionResult("me@example.com", "fake")
    assert injector.texts == ["me@example.com"]
    record = history.records[0]
    assert getattr(record, "raw_text") == "我的邮箱"
    assert getattr(record, "final_text") == "me@example.com"
    assert getattr(record, "processing_mode") == "quick"
    assert getattr(record, "status") == "completed"


def test_live_session_orders_events_and_injects_only_final(tmp_path: Path) -> None:
    wav_path = tmp_path / "owned.wav"
    capture = _Capture(wav_path)
    injector = _Injector()
    history = _History()
    pipeline = _pipeline(
        tmp_path,
        capture=capture,
        streamer=_Streamer(),
        calibrator=_Calibrator("校准文本"),
        injector=injector,
        history=history,
    )
    events = []

    session = pipeline.create_session(RecognitionRequest(event_sink=events.append))
    session.run()

    assert [event.type for event in events] == [
        "ready",
        "transcript",
        "transcript",
        "finalized",
        "completed",
    ]
    assert events[1].transcript.partial_text == "局部"
    assert events[2].transcript.authoritative_text == "校准文本"
    assert injector.texts == ["校准文本"]
    assert capture.stopped and capture.released
    assert not wav_path.exists()


def test_live_qwen_failure_warns_and_keeps_snippet_corrected_text(tmp_path: Path) -> None:
    injector = _Injector()
    capture = _Capture(tmp_path / "fallback.wav")
    pipeline = _pipeline(
        tmp_path,
        capture=capture,
        streamer=_Streamer(),
        calibrator=_Calibrator(RuntimeError("模型不可用")),
        injector=injector,
        history=_History(),
    )
    events = []

    pipeline.create_session(RecognitionRequest(event_sink=events.append)).run()

    assert [event.type for event in events] == [
        "ready",
        "transcript",
        "warning",
        "transcript",
        "finalized",
        "completed",
    ]
    final = next(event.transcript for event in events if event.type == "finalized")
    assert final.backend == "hybrid-fallback"
    assert final.authoritative_text == "me@example.com"
    assert injector.texts == ["me@example.com"]


def test_live_processing_warning_preserves_fallback_and_records_status(tmp_path: Path) -> None:
    injector = _Injector()
    history = _History()
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / "processing-fallback.wav"),
        streamer=_Streamer(),
        calibrator=_Calibrator("校准文本"),
        injector=injector,
        history=history,
        processor=_Processor(
            TextProcessResult(
                "处理回退文本",
                "http-error",
                "openai-compatible",
                "文本服务不可用",
            )
        ),
    )
    events = []

    pipeline.create_session(RecognitionRequest(mode="voice-polish", event_sink=events.append)).run()

    assert [event.type for event in events] == [
        "ready",
        "transcript",
        "warning",
        "transcript",
        "finalized",
        "completed",
    ]
    assert events[2].message == "文本服务不可用"
    assert injector.texts == ["处理回退文本"]
    assert getattr(history.records[0], "status") == "http-error"


def test_live_error_completes_without_injection_and_reaps_capture(tmp_path: Path) -> None:
    capture = _Capture(tmp_path / "error.wav")
    injector = _Injector()
    pipeline = _pipeline(
        tmp_path,
        capture=capture,
        streamer=_Streamer(fail=RuntimeError("VAD 失败")),
        calibrator=_Calibrator("unused"),
        injector=injector,
        history=_History(),
    )
    events = []

    pipeline.create_session(RecognitionRequest(event_sink=events.append)).run()

    assert [event.type for event in events] == ["ready", "error", "completed"]
    assert injector.texts == []
    assert capture.cancelled and capture.released
    assert not capture.wav_path.exists()


def test_live_cancel_reaps_capture_without_injection_or_history(tmp_path: Path) -> None:
    capture = _Capture(tmp_path / "cancel.wav")
    injector = _Injector()
    history = _History()
    pipeline = _pipeline(
        tmp_path,
        capture=capture,
        streamer=_Streamer(),
        calibrator=_Calibrator("unused"),
        injector=injector,
        history=history,
    )
    events = []
    session = pipeline.create_session(RecognitionRequest(event_sink=events.append))

    session.start()
    session.cancel()

    assert [event.type for event in events] == ["ready", "cancelled"]
    assert capture.cancelled and capture.released
    assert injector.texts == []
    assert history.records == []
    assert not capture.wav_path.exists()


def test_forced_cancel_during_calibration_skips_final_side_effects(tmp_path: Path) -> None:
    capture = _Capture(tmp_path / "forced-cancel.wav")
    injector = _Injector()
    history = _History()
    calibrator = _CancellingCalibrator()
    pipeline = _pipeline(
        tmp_path,
        capture=capture,
        streamer=_Streamer(),
        calibrator=calibrator,
        injector=injector,
        history=history,
    )
    events = []
    session = pipeline.create_session(RecognitionRequest(event_sink=events.append))
    calibrator.cancel = session.cancel

    session.start()
    session.stop()

    assert [event.type for event in events] == ["ready", "cancelled"]
    assert injector.texts == []
    assert history.records == []
    assert capture.cancelled and capture.released
    assert not capture.wav_path.exists()


def test_clipboard_snapshot_occurs_at_session_creation_and_warns_after_ready(
    tmp_path: Path,
) -> None:
    clipboard = _Clipboard(("主选区为空。",))
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / "warning.wav", chunks=()),
        streamer=_Streamer(),
        calibrator=_Calibrator("完成"),
        injector=_Injector(),
        history=_History(),
        clipboard=clipboard,
    )
    events = []

    session = pipeline.create_session(RecognitionRequest(inject=False, event_sink=events.append))
    assert clipboard.calls == 1
    session.run()

    assert [event.type for event in events[:2]] == ["ready", "warning"]
    assert events[1].message == "主选区为空。"


class _Recorder:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.seconds: list[float] = []

    def record_seconds(self, seconds: float) -> Path:
        self.seconds.append(seconds)
        return self.output


def test_run_once_requires_an_audio_source(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake")),
        provider=FakeProvider("文本"),
        injector=_Injector(),  # type: ignore[arg-type]
        vocabulary=_vocabulary(tmp_path),
        paths=paths,
        modes=ModesRepository(paths),
        history=_History(),  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="audio_path 或 record_seconds"):
        pipeline.run_once()


def test_run_once_records_when_requested_and_can_skip_injection_and_history(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    recorder = _Recorder(tmp_path / "recorded.wav")
    injector = _Injector()
    pipeline = VoiceInputPipeline(
        Config(
            asr=ASRConfig(batch_backend="fake"),
            history=HistoryConfig(enabled=False),
        ),
        provider=FakeProvider("我的邮箱"),
        injector=injector,  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        vocabulary=_vocabulary(tmp_path),
        paths=paths,
        modes=ModesRepository(paths),
        history=None,
        model_manager=_Models(),  # type: ignore[arg-type]
    )

    result = pipeline.run_once(record_seconds=1.75, inject=False)

    assert recorder.seconds == [1.75]
    assert result.recognition.text == "me@example.com"
    assert result.injection is None
    assert injector.texts == []
    assert pipeline.history is None


def test_run_once_failed_injection_is_persisted_as_failure_status(tmp_path: Path) -> None:
    class FailedInjector(_Injector):
        def inject(self, text: str) -> InjectionResult:
            self.texts.append(text)
            return InjectionResult("wtype", False, "目标拒绝输入")

    paths = _paths(tmp_path)
    history = _History()
    pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake")),
        provider=FakeProvider("原文"),
        injector=FailedInjector(),  # type: ignore[arg-type]
        vocabulary=_vocabulary(tmp_path),
        paths=paths,
        modes=ModesRepository(paths),
        history=history,  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
    )

    result = pipeline.run_once(audio_path=tmp_path / "audio.wav")

    assert result.injection is not None and not result.injection.ok
    assert getattr(history.records[0], "status") == "injection-failed"


def test_non_quick_mode_dispatches_complete_processing_request(tmp_path: Path) -> None:
    processor = _Processor(TextProcessResult("润色结果", "success", "ollama"))
    injector = _Injector()
    history = _History()
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / "processed.wav"),
        streamer=_Streamer(),
        calibrator=_Calibrator("校准原文"),
        injector=injector,
        history=history,
        clipboard=_Clipboard(),
        processor=processor,
    )
    requests: list[object] = []
    original_process = processor.process

    def capture_request(request):  # type: ignore[no-untyped-def]
        requests.append(request)
        return original_process(request)

    processor.process = capture_request  # type: ignore[method-assign]

    pipeline.create_session(RecognitionRequest(mode="voice-polish")).run()

    assert injector.texts == ["润色结果"]
    request = requests[0]
    assert request.text == "校准原文"
    assert request.selected == "选区"
    assert request.clipboard == "剪贴板"
    record = history.records[0]
    assert getattr(record, "processed_text") == "润色结果"
    assert getattr(record, "asr_provider") == "hybrid"
    assert getattr(record, "asr_model") == "qwen3-asr-0.6b-int8"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("qwen3-sherpa", "qwen3-asr-0.6b-int8"),
        ("hybrid", "qwen3-asr-0.6b-int8"),
        ("sensevoice", "sensevoice-int8"),
        ("sensevoice-vad", "sensevoice-int8"),
        ("hybrid-fallback", "sensevoice-int8"),
        ("fake", None),
    ],
)
def test_backend_model_mapping_is_stable(
    tmp_path: Path,
    backend: str,
    expected: str | None,
) -> None:
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / f"{backend}.wav"),
        streamer=_Streamer(),
        calibrator=_Calibrator("文本"),
        injector=_Injector(),
        history=_History(),
    )

    assert pipeline._model_for_backend(backend) == expected
    assert pipeline._active_model_ids() == (
        "sensevoice-int8",
        "silero-vad",
        "qwen3-asr-0.6b-int8",
    )


@pytest.mark.parametrize(
    ("provider", "processor_type"),
    [
        ("none", type(None)),
        ("openai-compatible", OpenAICompatibleProcessor),
        ("ollama", OllamaProcessor),
    ],
)
def test_processor_factory_supports_all_configured_providers(
    provider: str,
    processor_type: type[object],
) -> None:
    processor = _create_processor(
        Config(
            processing=ProcessingConfig(
                provider=provider,  # type: ignore[arg-type]
                base_url="http://127.0.0.1:11434",
                model="local-model",
                api_key_env="TYPE4ME_TOKEN",
                timeout_seconds=7.5,
            )
        )
    )

    assert isinstance(processor, processor_type)


@pytest.mark.parametrize("final_backend", ["", "none", "unsupported"])
def test_live_calibrator_rejects_unknown_ids(tmp_path: Path, final_backend: str) -> None:
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / "unsupported.wav"),
        streamer=_Streamer(),
        calibrator=_Calibrator("ignored"),
        injector=_Injector(),
        history=_History(),
    )
    pipeline._configured_calibrator = None
    pipeline.config = Config(asr=ASRConfig(batch_backend="fake", final_backend=final_backend))

    with pytest.raises(ValueError, match="不支持的最终识别后端"):
        pipeline._get_live_calibrator()


def test_live_sensevoice_policy_disables_calibrator(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / "disabled.wav"),
        streamer=_Streamer(),
        calibrator=_Calibrator("ignored"),
        injector=_Injector(),
        history=_History(),
    )
    pipeline._configured_calibrator = None
    pipeline.config = Config(asr=ASRConfig(batch_backend="fake", final_backend="sensevoice"))

    assert pipeline._get_live_calibrator() is None


def test_live_failed_injection_overrides_successful_processing_status(tmp_path: Path) -> None:
    class FailedInjector(_Injector):
        def inject(self, text: str) -> InjectionResult:
            self.texts.append(text)
            return InjectionResult("wtype", False, "目标拒绝输入")

    history = _History()
    pipeline = _pipeline(
        tmp_path,
        capture=_Capture(tmp_path / "live-injection-failure.wav"),
        streamer=_Streamer(),
        calibrator=_Calibrator("校准文本"),
        injector=FailedInjector(),
        history=history,
        processor=_Processor(TextProcessResult("处理文本", "success", "ollama")),
    )

    pipeline.create_session(RecognitionRequest(mode="voice-polish")).run()

    record = history.records[0]
    assert getattr(record, "status") == "injection-failed"
    assert getattr(record, "processed_text") == "处理文本"


def test_live_calibrator_factory_builds_qwen_with_effective_hotwords(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    vocabulary = _vocabulary(tmp_path)
    vocabulary.add_hotword("Qwen")
    pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake", final_backend="qwen3-sherpa")),
        provider=FakeProvider(),
        injector=_Injector(),  # type: ignore[arg-type]
        vocabulary=vocabulary,
        paths=paths,
        modes=ModesRepository(paths),
        history=_History(),  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
    )

    calibrator = pipeline._get_live_calibrator()

    assert isinstance(calibrator, Qwen3SherpaProvider)
    assert calibrator.hotwords == ("Qwen",)


def test_live_provider_lifetime_reuses_models_and_keeps_session_state_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensevoice_instances: list[object] = []
    qwen_instances: list[object] = []

    class RecordingSenseVoiceProvider:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.model_dir = kwargs["model_dir"]
            sensevoice_instances.append(self)

    class RecordingQwenProvider:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.calls = 0
            self.hotwords = kwargs["hotwords"]
            qwen_instances.append(self)

        def transcribe(self, _wav_path: Path) -> RecognitionResult:
            self.calls += 1
            return RecognitionResult("Qwen 最终文本", "qwen3-sherpa")

    class FlushedSenseVoiceStreamer:
        def accept_chunk(self, _chunk: bytes) -> tuple[RecognitionTranscript, ...]:
            return ()

        def flush(self) -> RecognitionTranscript:
            return RecognitionTranscript(
                ("SenseVoice 最终文本",),
                "",
                "SenseVoice 最终文本",
                True,
                "sensevoice-vad",
            )

    monkeypatch.setattr(pipeline_module, "SenseVoiceProvider", RecordingSenseVoiceProvider)
    monkeypatch.setattr(pipeline_module, "Qwen3SherpaProvider", RecordingQwenProvider)

    qwen_root = tmp_path / "qwen"
    qwen_root.mkdir()
    qwen_paths = _paths(qwen_root)
    qwen_streamers: list[tuple[object, _Streamer]] = []

    def qwen_streamer_factory(_config: object, sensevoice: object, **_kwargs: object) -> _Streamer:
        streamer = _Streamer()
        qwen_streamers.append((sensevoice, streamer))
        return streamer

    qwen_pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake", final_backend="qwen3-sherpa")),
        provider=FakeProvider(),
        injector=_Injector(),  # type: ignore[arg-type]
        vocabulary=_vocabulary(qwen_root),
        paths=qwen_paths,
        modes=ModesRepository(qwen_paths),
        history=_History(),  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
        capture_factory=lambda: _Capture(qwen_root / "unused.wav"),
        streamer_factory=qwen_streamer_factory,
    )

    first = qwen_pipeline.create_session(RecognitionRequest(inject=False))
    second = qwen_pipeline.create_session(RecognitionRequest(inject=False))
    qwen_pipeline.vocabulary.add_hotword("Fresh")
    third = qwen_pipeline.create_session(RecognitionRequest(inject=False))

    assert len(sensevoice_instances) == 1
    assert [item[0] for item in qwen_streamers] == [sensevoice_instances[0]] * 3
    assert len({id(item[1]) for item in qwen_streamers}) == 3
    assert len(qwen_instances) == 2
    assert first._calibrator is second._calibrator is qwen_instances[0]  # type: ignore[attr-defined]
    assert third._calibrator is qwen_instances[1]  # type: ignore[attr-defined]
    assert getattr(qwen_instances[1], "hotwords") == ("Fresh",)

    sense_root = tmp_path / "sense"
    sense_root.mkdir()
    sense_paths = _paths(sense_root)
    events = []
    sense_pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake", final_backend="sensevoice")),
        provider=FakeProvider(),
        injector=_Injector(),  # type: ignore[arg-type]
        vocabulary=_vocabulary(sense_root),
        paths=sense_paths,
        modes=ModesRepository(sense_paths),
        history=_History(),  # type: ignore[arg-type]
        model_manager=_Models(),  # type: ignore[arg-type]
        capture_factory=lambda: _Capture(sense_root / "sense.wav"),
        streamer_factory=lambda *_args, **_kwargs: FlushedSenseVoiceStreamer(),
    )

    sense_pipeline.create_session(RecognitionRequest(inject=False, event_sink=events.append)).run()

    final = next(event.transcript for event in events if event.type == "finalized")
    assert final is not None
    assert final.authoritative_text == "SenseVoice 最终文本"
    assert final.backend == "sensevoice-vad"
    assert len(qwen_instances) == 2
    assert all(getattr(instance, "calls") == 0 for instance in qwen_instances)


def test_live_provider_refreshes_changed_model_and_rejects_removed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_model = tmp_path / "sensevoice-v1"
    second_model = tmp_path / "sensevoice-v2"
    resolutions: list[Path | Exception] = [
        first_model,
        second_model,
        RuntimeError("模型已删除"),
    ]
    providers: list[object] = []
    stream_providers: list[object] = []

    class ChangingModels:
        def resolve(self, model_id: str) -> Path:
            if model_id != "sensevoice-int8":
                return Path("/unused") / model_id
            resolved = resolutions.pop(0)
            if isinstance(resolved, Exception):
                raise resolved
            return resolved

    class RecordingSenseVoiceProvider:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.model_dir = kwargs["model_dir"]
            providers.append(self)

    def streamer_factory(_config: object, sensevoice: object, **_kwargs: object) -> _Streamer:
        stream_providers.append(sensevoice)
        return _Streamer()

    monkeypatch.setattr(pipeline_module, "SenseVoiceProvider", RecordingSenseVoiceProvider)
    paths = _paths(tmp_path)
    pipeline = VoiceInputPipeline(
        Config(asr=ASRConfig(batch_backend="fake", final_backend="sensevoice")),
        provider=FakeProvider(),
        injector=_Injector(),  # type: ignore[arg-type]
        vocabulary=_vocabulary(tmp_path),
        paths=paths,
        modes=ModesRepository(paths),
        history=_History(),  # type: ignore[arg-type]
        model_manager=ChangingModels(),  # type: ignore[arg-type]
        capture_factory=lambda: _Capture(tmp_path / "unused.wav"),
        streamer_factory=streamer_factory,
    )

    pipeline.create_session(RecognitionRequest(inject=False))
    pipeline.create_session(RecognitionRequest(inject=False))

    assert [getattr(provider, "model_dir") for provider in providers] == [
        first_model,
        second_model,
    ]
    assert stream_providers == providers
    with pytest.raises(RuntimeError, match="模型已删除"):
        pipeline.create_session(RecognitionRequest(inject=False))
    assert len(providers) == 2
