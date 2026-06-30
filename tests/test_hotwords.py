from __future__ import annotations

from type4me_linux.hotwords import apply_snippets, sanitize_qwen_output


def test_sanitize_qwen_output_removes_prompt_prefix_and_hotword_leak() -> None:
    text = sanitize_qwen_output("语音转写：热词：Qwen3-ASR 今天测试成功", ("Qwen3-ASR",))

    assert text == "今天测试成功"


def test_apply_snippets_replaces_spoken_phrases() -> None:
    text = apply_snippets("我的邮箱 请发给 NixOS", {"我的邮箱": "me@example.com"})

    assert text == "me@example.com 请发给 NixOS"

