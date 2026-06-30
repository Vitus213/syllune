from __future__ import annotations

from pathlib import Path

from type4me_linux.config import Config
from type4me_linux.inject import InjectionResult
from type4me_linux.pipeline import VoiceInputPipeline
from type4me_linux.providers import FakeProvider


class _Injector:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def inject(self, text: str) -> InjectionResult:
        self.texts.append(text)
        return InjectionResult("test", True)


def test_pipeline_applies_snippets_before_injection(tmp_path: Path) -> None:
    injector = _Injector()
    pipeline = VoiceInputPipeline(
        Config(snippets={"我的邮箱": "me@example.com"}),
        provider=FakeProvider("我的邮箱"),
        injector=injector,  # type: ignore[arg-type]
    )

    result = pipeline.run_once(audio_path=tmp_path / "fake.wav")

    assert result.recognition.text == "me@example.com"
    assert injector.texts == ["me@example.com"]

