from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .inject import InjectionResult


@dataclass(frozen=True)
class RecognitionTranscript:
    confirmed_segments: tuple[str, ...]
    partial_text: str
    authoritative_text: str
    is_final: bool
    backend: str


@dataclass(frozen=True)
class RecognitionEvent:
    type: Literal[
        "ready",
        "transcript",
        "warning",
        "error",
        "cancelled",
        "completed",
        "finalized",
    ]
    sequence: int
    transcript: RecognitionTranscript | None = None
    message: str | None = None
    injection: InjectionResult | None = None
