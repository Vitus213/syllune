from __future__ import annotations

import re


def apply_snippets(text: str, snippets: dict[str, str]) -> str:
    output = text
    for spoken, replacement in snippets.items():
        output = output.replace(spoken, replacement)
    return output


def sanitize_qwen_output(text: str, hotwords: tuple[str, ...] = ()) -> str:
    """Remove common prompt/hotword leaks from local Qwen-style ASR output."""

    cleaned = text.strip()
    cleaned = re.sub(
        r"^(转写|语音转写|transcription|transcript)\s*[:：]\s*", "", cleaned, flags=re.I
    )
    cleaned = re.sub(r"^(以下是|下面是).{0,12}(识别|转写).{0,8}[:：]\s*", "", cleaned)
    for hotword in hotwords:
        if not hotword:
            continue
        leak_patterns = [
            rf"(热词|hotwords?)\s*[:：]\s*{re.escape(hotword)}",
            rf"{re.escape(hotword)}\s*(是热词|为热词)",
        ]
        for pattern in leak_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()
