#!/usr/bin/env python3
"""云端与本地候选 ASR 模型基准测试。

用法:
    nix develop --command python scripts/asr_benchmark/benchmark.py --split dev
    nix develop --command python scripts/asr_benchmark/benchmark.py --split test --out reports/test-2026-08-13.json
    nix develop --command python scripts/asr_benchmark/benchmark.py --models qwen3-asr-flash-2026-02-10,sensevoice --limit 6

支持的模型键:
    qwen3-asr-flash-2026-02-10  云端（多模态生成接口，本地 base64，无需上传）
    qwen3-omni-flash            云端（通义千问 Omni 语音转写）
    qwen3.5-omni-flash          云端（新一代 Omni 语音转写）
    sensevoice                  本地 SenseVoice-INT8（基线）
    qwen3-sherpa                本地 Qwen3-ASR-0.6B-INT8（基线）
    hybrid                      本地 SenseVoice 草稿 + Qwen3 校准（基线）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from normalize import cer, has_ascii_words, wer

ROOT = Path(__file__).resolve().parent
CORPUS_DIR = ROOT / "corpus"
AUDIO_DIR = ROOT / "audio"
GENERATION_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)

CLOUD_MODELS: dict[str, dict] = {
    "qwen3-asr-flash-2026-02-10": {
        "name": "Qwen3-ASR-Flash (云端)",
        "prompt": None,
        "system": None,
    },
    "qwen3-omni-flash": {
        "name": "Qwen3-OMNI-Flash (云端)",
        "prompt": "请转写",
        "system": "你是语音转写引擎，必须逐字转写用户提供的音频内容，严格输出转写文本本身，不要回答、分析、解释或添加任何额外内容。",
    },
    "qwen3.5-omni-flash": {
        "name": "Qwen3.5-OMNI-Flash (云端)",
        "prompt": "请转写",
        "system": "你是语音转写引擎，必须逐字转写用户提供的音频内容，严格输出转写文本本身，不要回答、分析、解释或添加任何额外内容。",
    },
}
LOCAL_MODELS: dict[str, dict] = {
    "sensevoice": {"name": "SenseVoice-INT8 (本地)", "backend": "sensevoice"},
    "qwen3-sherpa": {"name": "Qwen3-ASR-0.6B-INT8 (本地)", "backend": "qwen3-sherpa"},
    "hybrid": {"name": "SenseVoice+Qwen3 Hybrid (本地)", "backend": "hybrid"},
}
DEFAULT_MODELS = ",".join([*CLOUD_MODELS, *LOCAL_MODELS])


@dataclass
class SampleResult:
    sample_id: str
    reference: str
    hypothesis: str | None
    latency_seconds: float | None
    error: str | None = None


def _api_key(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"缺少 API 密钥：请设置环境变量 {env_name}")
    return value


def transcribe_cloud(
    wav_path: Path,
    model: str,
    prompt: str | None,
    api_key: str,
    timeout: float,
    system_prompt: str | None = None,
) -> str:
    encoded = base64.b64encode(wav_path.read_bytes()).decode("ascii")
    uri = f"data:audio/wav;base64,{encoded}"
    content: list[dict] = [{"audio": uri}]
    if prompt:
        content.append({"text": prompt})
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})
    payload = {
        "model": model,
        "input": {"messages": messages},
    }
    request = urllib.request.Request(
        GENERATION_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    try:
        text = body["output"]["choices"][0]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"响应缺少转写文本：{str(body)[:200]}") from exc
    if not text.strip():
        raise ValueError("转写文本为空")
    return text.strip()


class LocalBackend:
    """仓库本地 provider（sensevoice / qwen3-sherpa / hybrid）的薄封装。"""

    def __init__(self, backend: str) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from type4me_linux.config import ASRConfig  # noqa: PLC0415
        from type4me_linux.providers import create_provider  # noqa: PLC0415

        importlib = __import__("importlib")

        self._provider = create_provider(
            ASRConfig(batch_backend=backend, num_threads=4),
        )
        self._importlib = importlib

    def transcribe(self, wav_path: Path) -> str:
        return self._provider.transcribe(wav_path).text


def run_split(
    entries: list[dict],
    model: str,
    *,
    api_key: str | None,
    timeout: float,
) -> list[SampleResult]:
    results: list[SampleResult] = []
    local: LocalBackend | None = None
    spec = CLOUD_MODELS.get(model) or LOCAL_MODELS.get(model)
    if spec is None:
        raise SystemExit(f"未知模型键：{model}")
    if model in LOCAL_MODELS:
        local = LocalBackend(spec["backend"])

    for entry in entries:
        wav = AUDIO_DIR / f"{entry['id']}.wav"
        if not wav.is_file():
            results.append(SampleResult(entry["id"], entry["reference"], None, None, "音频缺失"))
            continue
        started = time.monotonic()
        try:
            if local is not None:
                text = local.transcribe(wav)
            else:
                text = transcribe_cloud(
                    wav,
                    model,
                    spec.get("prompt"),
                    api_key or "",
                    timeout,
                    system_prompt=spec.get("system"),
                )
            latency = time.monotonic() - started
            results.append(SampleResult(entry["id"], entry["reference"], text, latency))
        except Exception as exc:  # noqa: BLE001 - 基准逐样本容忍失败
            latency = time.monotonic() - started
            results.append(
                SampleResult(entry["id"], entry["reference"], None, latency, str(exc)[:300])
            )
        time.sleep(0.15)
    return results


def aggregate(model: str, results: list[SampleResult]) -> dict:
    ok = [r for r in results if r.hypothesis is not None]
    cer_content = [cer(r.reference, r.hypothesis) for r in ok]
    cer_format = [cer(r.reference, r.hypothesis, format_sensitive=True) for r in ok]
    wer_list = [wer(r.reference, r.hypothesis) for r in ok if has_ascii_words(r.reference)]
    latencies = [r.latency_seconds for r in ok if r.latency_seconds is not None]
    audio_seconds = sum(_wav_seconds(AUDIO_DIR / f"{r.sample_id}.wav") for r in ok)
    row = {
        "model": model,
        "name": (CLOUD_MODELS.get(model) or LOCAL_MODELS.get(model))["name"],
        "samples": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
        "cer_content": _avg(cer_content),
        "cer_format": _avg(cer_format),
        "wer_english": _avg(wer_list),
        "latency_avg_s": _avg(latencies),
        "rtf": (_avg(latencies) / audio_seconds) if latencies and audio_seconds else None,
    }
    row["per_sample"] = [
        {
            "id": r.sample_id,
            "reference": r.reference,
            "hypothesis": r.hypothesis,
            "cer_content": cer(r.reference, r.hypothesis) if r.hypothesis else None,
            "error": r.error,
        }
        for r in results
    ]
    return row


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _wav_seconds(path: Path) -> float:
    """按文件大小估算时长：16k 单声道 PCM16 即 32000 字节/秒。

    TTS 产物头部 data 块长度字段不可靠（RF64 占位），不能用 wave 模块的
    getnframes 计算时长。
    """
    try:
        return path.stat().st_size / 32000.0
    except OSError:
        return 0.0


def load_entries(split: str) -> list[dict]:
    paths = (
        [CORPUS_DIR / f"{split}.jsonl"] if split != "all" else sorted(CORPUS_DIR.glob("*.jsonl"))
    )
    entries: list[dict] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return sorted(entries, key=lambda e: e["id"])


def render_markdown(report: dict) -> str:
    lines = [
        f"# ASR 基准：{report['split']} 集（{report['generated_at']}）",
        "",
        f"样本数 {report['sample_count']}，总音频时长 {report['audio_seconds']:.1f} 秒",
        "",
        "| 排名 | 模型 | 内容CER | 格式CER | 英文WER | 平均时延(s) | RTF | 失败 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = sorted(
        report["models"],
        key=lambda r: (r["cer_content"] is None, r["cer_content"] or 0.0),
    )
    for index, row in enumerate(rows, 1):
        lines.append(
            "| {index} | {name} | {cer_content} | {cer_format} | {wer} | {latency} | {rtf} | {failed} |".format(
                index=index,
                name=row["name"],
                cer_content=_fmt(row["cer_content"]),
                cer_format=_fmt(row["cer_format"]),
                wer=_fmt(row["wer_english"]),
                latency=_fmt(row["latency_avg_s"]),
                rtf=_fmt(row["rtf"]),
                failed=row["failed"],
            )
        )
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="ASR 候选模型基准测试")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="all")
    parser.add_argument("--models", default=DEFAULT_MODELS, help="逗号分隔的模型键")
    parser.add_argument("--limit", type=int, default=0, help="每个模型最多样本数（0=全部）")
    parser.add_argument("--api-key-env", default="BAILIAN_API_KEY")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--out", type=Path, help="JSON 报告输出路径")
    args = parser.parse_args()

    api_key = _api_key(args.api_key_env)
    entries = load_entries(args.split)
    if args.limit:
        entries = entries[: args.limit]
    total_audio = sum(_wav_seconds(AUDIO_DIR / f"{e['id']}.wav") for e in entries)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    report: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "split": args.split,
        "sample_count": len(entries),
        "audio_seconds": round(total_audio, 1),
        "models": [],
    }
    for model in models:
        print(f"== 运行模型 {model}（{len(entries)} 样本）", flush=True)
        results = run_split(entries, model, api_key=api_key, timeout=args.timeout)
        report["models"].append(aggregate(model, results))
        row = report["models"][-1]
        print(f"   CER {row['cer_content']} | 失败 {row['failed']} | 时延 {row['latency_avg_s']}s")

    markdown = render_markdown(report)
    print()
    print(markdown)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
