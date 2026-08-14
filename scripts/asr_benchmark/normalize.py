#!/usr/bin/env python3
"""标点无关/相关中文 CER 与英文 WER 计算（基准测试度量）。

CER: 编辑距离 / 参考长度（按字符）。
- content 模式：忽略所有标点与空白、NFKC、转小写 —— 衡量识别内容准确度。
- format 模式：保留标点但忽略空白 —— 衡量结构（含标点）准确度。
WER: 参考文本中 ASCII 单词（含数字）按词编辑距离。
"""

from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CJK = "".join(chr(code) for code in range(0x4E00, 0xA000))


def normalize_content(text: str) -> str:
    """NFKC、小写、去掉所有非字母数字字符（保留中英文与数字）。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    return _PUNCT_RE.sub("", normalized)


def normalize_format(text: str) -> str:
    """NFKC、小写、去掉空白（保留标点）。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", "", normalized)


def ascii_words(text: str) -> list[str]:
    return _ASCII_WORD_RE.findall(text)


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str, *, format_sensitive: bool = False) -> float:
    ref = normalize_format(reference) if format_sensitive else normalize_content(reference)
    hyp = normalize_format(hypothesis) if format_sensitive else normalize_content(hypothesis)
    if not ref:
        return 0.0
    return edit_distance(ref, hyp) / len(ref)


def wer(reference: str, hypothesis: str) -> float:
    ref_words = ascii_words(reference)
    if not ref_words:
        return 0.0
    hyp_words = ascii_words(hypothesis)
    return edit_distance(ref_words, hyp_words) / len(ref_words)


def has_ascii_words(reference: str) -> bool:
    return bool(ascii_words(reference))
