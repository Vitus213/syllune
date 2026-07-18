from __future__ import annotations

import argparse
import importlib
import json
import signal
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import FrameType
from typing import Any

from .config import Config, load_config
from .daemon import serve
from .doctor import run_checks
from .events import RecognitionEvent
from .history import HistoryStore
from .inject import TextInjector
from .model_catalog import MODEL_CATALOG
from .model_manager import ModelManager
from .modes import ModesRepository
from .paths import AppPaths
from .pipeline import RecognitionRequest, VoiceInputPipeline
from .vocabulary import VocabularyService

BATCH_BACKENDS = ("fake", "sensevoice", "qwen3-sherpa", "hybrid")
STREAM_BACKENDS = ("sensevoice-vad",)


class _ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "显示帮助并退出"

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法：", 1)

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法：", 1)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 错误：{_translate_argument_error(message)}\n")


def _translate_argument_error(message: str) -> str:
    required = "the following arguments are required:"
    if message.startswith(required):
        return f"缺少必需参数：{message.removeprefix(required).strip()}"

    unrecognized = "unrecognized arguments:"
    if message.startswith(unrecognized):
        return f"无法识别的参数：{message.removeprefix(unrecognized).strip()}"

    invalid_choice = re.fullmatch(
        r"argument (.+?): invalid choice: (.+?) \(choose from (.+)\)", message
    )
    if invalid_choice is not None:
        argument, value, choices = invalid_choice.groups()
        return f"参数 {argument}：无效选项 {value}（可选值：{choices}）"

    invalid_value = re.fullmatch(r"argument (.+?): invalid (.+?) value: (.+)", message)
    if invalid_value is not None:
        argument, value_type, value = invalid_value.groups()
        type_name = {"int": "整数", "float": "浮点数"}.get(value_type, value_type)
        return f"参数 {argument}：无法将 {value} 解析为{type_name}。"

    expected = re.fullmatch(r"argument (.+?): expected one argument", message)
    if expected is not None:
        return f"参数 {expected.group(1)}：必须提供一个值。"

    return "命令行参数无效。"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        return int(args.func(args, config))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = _ChineseArgumentParser(prog="type4me-linux", description="本地语音输入工具")
    parser.add_argument("--config", help="config.toml 配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查运行依赖、模型和桌面能力")
    doctor.add_argument(
        "--allow-missing-models",
        action="store_true",
        help="仅允许模型尚未安装，其他检查仍须通过",
    )
    doctor.set_defaults(func=_doctor)

    transcribe = sub.add_parser("transcribe", help="转写 WAV 文件")
    transcribe.add_argument("wav", type=Path)
    _add_backend_argument(transcribe, streaming=False)
    transcribe.add_argument("--inject", action="store_true", help="注入最终文本")
    transcribe.add_argument("--json", action="store_true", help="输出 JSON")
    transcribe.set_defaults(func=_transcribe)

    inject = sub.add_parser("inject", help="向当前 Wayland 客户端注入文本")
    inject.add_argument("text")
    inject.set_defaults(func=_inject)

    record = sub.add_parser("record", help="定时录音、转写并注入")
    record.add_argument("--seconds", type=float, default=5.0, help="录音秒数")
    _add_backend_argument(record, streaming=False)
    record.add_argument("--no-inject", action="store_true", help="不注入最终文本")
    record.set_defaults(func=_record)

    stream = sub.add_parser("stream", help="实时录音并输出模拟流式识别事件")
    _add_backend_argument(stream, streaming=True)
    stream.add_argument("--mode", help="模式 UUID 或名称")
    stream.add_argument("--no-inject", action="store_true", help="不注入最终文本")
    stream.add_argument("--json", action="store_true", help="逐行输出识别事件 JSON")
    stream.set_defaults(func=_stream)

    daemon = sub.add_parser("daemon", help="运行本地 HTTP 服务")
    daemon.set_defaults(func=_daemon)

    gui = sub.add_parser("gui", help="启动桌面应用")
    gui.add_argument("--background", action="store_true", help="在后台启动常驻服务")
    gui.set_defaults(func=_gui)
    service = sub.add_parser("service", help="运行 systemd 常驻桌面服务")
    service.set_defaults(func=_service)

    _add_model_parser(sub)
    _add_mode_parser(sub)
    _add_vocabulary_parser(sub)
    _add_history_parser(sub)

    for command, method, help_text in (
        ("toggle", "toggle", "切换录音状态"),
        ("hold-start", "hold_start", "开始按住说话录音"),
        ("hold-stop", "hold_stop", "结束按住说话录音"),
        ("cancel", "cancel", "取消当前录音"),
    ):
        control = sub.add_parser(command, help=help_text)
        control.set_defaults(func=_control, control_method=method)
    return parser


def _add_backend_argument(parser: argparse.ArgumentParser, *, streaming: bool) -> None:
    parser.add_argument(
        "--backend",
        choices=STREAM_BACKENDS if streaming else BATCH_BACKENDS,
        help="识别后端",
    )


def _add_model_parser(sub: Any) -> None:
    model = sub.add_parser("model", help="安装、校验或删除模型")
    actions = model.add_subparsers(dest="model_action", required=True)
    listing = actions.add_parser("list", help="列出模型目录")
    listing.set_defaults(func=_model)
    for action, help_text in (
        ("install", "安装模型"),
        ("update", "更新模型"),
        ("check", "离线校验模型"),
        ("remove", "删除模型"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("id", choices=tuple(MODEL_CATALOG))
        if action == "check":
            command.add_argument("--json", action="store_true", help="输出 JSON")
        if action == "remove":
            command.add_argument("--force", action="store_true", help="删除当前配置使用的模型")
        command.set_defaults(func=_model)


def _add_mode_parser(sub: Any) -> None:
    mode = sub.add_parser("mode", help="管理文本处理模式")
    actions = mode.add_subparsers(dest="mode_action", required=True)
    for action, help_text in (
        ("list", "列出模式"),
        ("reload", "重新读取模式"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.set_defaults(func=_mode)
    add = actions.add_parser("add", help="添加模式")
    add.add_argument("name")
    add.add_argument("--prompt", required=True)
    add.add_argument("--processing-label", default="")
    add.add_argument("--sort-order", type=int)
    add.set_defaults(func=_mode)
    update = actions.add_parser("update", help="更新用户模式")
    update.add_argument("id")
    update.add_argument("--name")
    update.add_argument("--prompt")
    update.add_argument("--processing-label")
    update.add_argument("--sort-order", type=int)
    update.set_defaults(func=_mode)
    remove = actions.add_parser("remove", help="删除用户模式")
    remove.add_argument("id")
    remove.set_defaults(func=_mode)


def _add_vocabulary_parser(sub: Any) -> None:
    vocabulary = sub.add_parser("vocabulary", help="管理热词和片段")
    kinds = vocabulary.add_subparsers(dest="vocabulary_kind", required=True)
    reload_command = kinds.add_parser("reload", help="重新读取词汇")
    reload_command.set_defaults(func=_vocabulary)
    for kind, label in (("hotwords", "热词"), ("snippets", "片段")):
        parser = kinds.add_parser(kind, help=f"管理{label}")
        actions = parser.add_subparsers(dest="vocabulary_action", required=True)
        listing = actions.add_parser("list", help=f"列出{label}")
        listing.set_defaults(func=_vocabulary)
        add = actions.add_parser("add", help=f"添加{label}")
        add.add_argument("key")
        if kind == "snippets":
            add.add_argument("value")
        add.set_defaults(func=_vocabulary)
        update = actions.add_parser("update", help=f"更新{label}")
        update.add_argument("key")
        update.add_argument("value")
        if kind == "snippets":
            update.add_argument("--new-trigger")
        update.set_defaults(func=_vocabulary)
        remove = actions.add_parser("remove", help=f"删除{label}")
        remove.add_argument("key")
        remove.set_defaults(func=_vocabulary)


def _add_history_parser(sub: Any) -> None:
    history = sub.add_parser("history", help="查询、导出或删除历史")
    actions = history.add_subparsers(dest="history_action", required=True)
    listing = actions.add_parser("list", help="列出历史")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--cursor")
    listing.add_argument("--from-date")
    listing.add_argument("--to-date")
    listing.set_defaults(func=_history)
    delete = actions.add_parser("delete", help="删除历史")
    delete.add_argument("ids", nargs="*")
    delete.add_argument("--all", action="store_true")
    delete.set_defaults(func=_history)
    export = actions.add_parser("export", help="导出 RFC 4180 CSV")
    export.add_argument("destination", type=Path)
    export.add_argument("--from-date")
    export.add_argument("--to-date")
    export.set_defaults(func=_history)
    totals = actions.add_parser("totals", help="汇总历史")
    totals.set_defaults(func=_history)
    usage = actions.add_parser("usage", help="统计模型用量")
    usage.add_argument("--days", type=int, choices=(1, 7, 30), default=7)
    usage.set_defaults(func=_history)


def _with_backend(config: Config, backend: str | None, *, streaming: bool = False) -> Config:
    if not backend:
        return config
    field = "streaming_backend" if streaming else "batch_backend"
    return replace(config, asr=replace(config.asr, **{field: backend}))


def _doctor(args: argparse.Namespace, config: Config) -> int:
    checks = run_checks(config)
    for check in checks:
        marker = "通过" if check.ok else "缺失"
        print(f"{marker:4} {check.name}: {check.detail}")
    if args.allow_missing_models:
        required = [check for check in checks if not check.allowed_missing_model]
        return 0 if all(check.ok for check in required) else 1
    return 0 if all(check.ok for check in checks) else 1


def _transcribe(args: argparse.Namespace, config: Config) -> int:
    configured = _with_backend(config, args.backend)
    result = VoiceInputPipeline(configured).run_once(audio_path=args.wav, inject=args.inject)
    if args.json:
        print(
            json.dumps(
                {
                    "text": result.recognition.text,
                    "backend": result.recognition.backend,
                    "draft_text": result.recognition.draft_text,
                    "injection": _injection_payload(result.injection),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(result.recognition.text)
    return 0 if result.injection is None or result.injection.ok else 1


def _inject(args: argparse.Namespace, config: Config) -> int:
    result = TextInjector(config.inject).inject(args.text)
    if not result.ok:
        print(result.message, file=sys.stderr)
        return 1
    return 0


def _record(args: argparse.Namespace, config: Config) -> int:
    configured = _with_backend(config, args.backend)
    result = VoiceInputPipeline(configured).run_once(
        record_seconds=args.seconds,
        inject=not args.no_inject,
    )
    print(result.recognition.text)
    return 0 if result.injection is None or result.injection.ok else 1


def _stream(args: argparse.Namespace, config: Config) -> int:
    configured = _with_backend(config, args.backend, streaming=True)
    saw_error = False
    saw_cancelled = False
    printed_final = False
    interactive = bool(getattr(sys.stderr, "isatty", lambda: False)())

    def consume_event(event: RecognitionEvent) -> None:
        nonlocal saw_error, saw_cancelled, printed_final
        if args.json:
            print(json.dumps(_event_payload(event), ensure_ascii=False), flush=True)
        else:
            if event.type == "transcript" and event.transcript is not None:
                transcript = event.transcript
                if transcript.is_final and not printed_final:
                    if interactive:
                        print("", file=sys.stderr)
                    print(transcript.authoritative_text, flush=True)
                    printed_final = True
                elif interactive and transcript.partial_text:
                    print(f"\r{transcript.partial_text}", end="", file=sys.stderr, flush=True)
            elif event.type in {"warning", "error"} and event.message:
                if interactive:
                    print("", file=sys.stderr)
                print(event.message, file=sys.stderr, flush=True)
        saw_error = saw_error or event.type == "error"
        saw_cancelled = saw_cancelled or event.type == "cancelled"

    pipeline = VoiceInputPipeline(configured)
    session = pipeline.create_session(
        RecognitionRequest(
            mode=args.mode,
            inject=not args.no_inject,
            event_sink=consume_event,
        )
    )
    interrupt_count = 0
    forced_cancel = False

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal interrupt_count, forced_cancel
        if signum == signal.SIGINT:
            interrupt_count += 1
            if interrupt_count == 1:
                session.stop()
                return
        forced_cancel = True
        session.cancel()

    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, handle_signal)
        session.run()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    if forced_cancel or saw_cancelled:
        return 130
    if saw_error:
        return 1
    return 0


def _event_payload(event: RecognitionEvent) -> dict[str, object]:
    transcript: dict[str, object] | None = None
    if event.transcript is not None:
        transcript = {
            "confirmed_segments": list(event.transcript.confirmed_segments),
            "partial_text": event.transcript.partial_text,
            "authoritative_text": event.transcript.authoritative_text,
            "is_final": event.transcript.is_final,
            "backend": event.transcript.backend,
        }
    return {
        "type": event.type,
        "sequence": event.sequence,
        "transcript": transcript,
        "message": event.message,
        "injection": _injection_payload(event.injection),
    }


def _injection_payload(injection: object | None) -> dict[str, object] | None:
    if injection is None:
        return None
    return {
        "ok": bool(getattr(injection, "ok")),
        "method": str(getattr(injection, "method")),
        "message": str(getattr(injection, "message")),
    }


def _model(args: argparse.Namespace, config: Config) -> int:
    paths = AppPaths.from_environment()
    manager = ModelManager(paths, active_model_ids=lambda: _active_model_ids(config))
    if args.model_action == "list":
        print(
            json.dumps([manager.check(model_id) for model_id in MODEL_CATALOG], ensure_ascii=False)
        )
        return 0
    if args.model_action == "install":
        print(manager.install(args.id))
        return 0
    if args.model_action == "update":
        print(manager.update(args.id))
        return 0
    if args.model_action == "remove":
        removed = manager.remove(args.id, force=args.force)
        print("已删除" if removed else "模型未安装")
        return 0
    result = manager.check(args.id)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("模型完整" if result["ok"] else "模型校验失败")
        for key in ("missing", "extra", "corrupt", "errors"):
            for detail in result[key]:
                print(f"{key}: {detail}")
    return 0 if result["ok"] else 1


def _mode(args: argparse.Namespace, config: Config) -> int:
    repository = ModesRepository(AppPaths.from_environment())
    action = args.mode_action
    if action == "list":
        value: object = [asdict(mode) for mode in repository.list()]
    elif action == "reload":
        value = [asdict(mode) for mode in repository.reload()]
    elif action == "add":
        value = asdict(
            repository.add(
                args.name,
                args.prompt,
                args.processing_label,
                sort_order=args.sort_order,
            )
        )
    elif action == "update":
        value = asdict(
            repository.update(
                args.id,
                name=args.name,
                prompt=args.prompt,
                processing_label=args.processing_label,
                sort_order=args.sort_order,
            )
        )
    else:
        value = asdict(repository.remove(args.id))
    print(json.dumps(value, ensure_ascii=False))
    return 0


def _vocabulary(args: argparse.Namespace, config: Config) -> int:
    service = VocabularyService(AppPaths.from_environment())
    if args.vocabulary_kind == "reload":
        service.reload()
        value: object = {
            "hotwords": list(service.list_hotwords()),
            "snippets": service.list_snippets(),
        }
    elif args.vocabulary_kind == "hotwords":
        action = args.vocabulary_action
        if action == "list":
            value = list(service.list_hotwords())
        elif action == "add":
            value = list(service.add_hotword(args.key))
        elif action == "update":
            value = list(service.update_hotword(args.key, args.value))
        else:
            value = list(service.remove_hotword(args.key))
    else:
        action = args.vocabulary_action
        if action == "list":
            value = service.list_snippets()
        elif action == "add":
            value = service.add_snippet(args.key, args.value)
        elif action == "update":
            value = service.update_snippet(
                args.key,
                args.value,
                new_trigger=args.new_trigger,
            )
        else:
            value = service.remove_snippet(args.key)
    print(json.dumps(value, ensure_ascii=False))
    return 0


def _history(args: argparse.Namespace, config: Config) -> int:
    store = HistoryStore(AppPaths.from_environment())
    action = args.history_action
    if action == "list":
        page = store.query(
            limit=args.limit,
            cursor=args.cursor,
            from_date=args.from_date,
            to_date=args.to_date,
        )
        value: object = {
            "records": [asdict(record) for record in page.records],
            "next_cursor": page.next_cursor,
        }
    elif action == "delete":
        if args.all:
            count = store.delete_all()
        else:
            count = store.delete_many(args.ids)
        value = {"deleted": count}
    elif action == "export":
        count = store.export_csv(
            args.destination,
            from_date=args.from_date,
            to_date=args.to_date,
        )
        value = {"exported": count, "path": str(args.destination)}
    elif action == "totals":
        value = asdict(store.totals())
    else:
        value = asdict(store.usage_summary(args.days))
    print(json.dumps(value, ensure_ascii=False))
    return 0


def _control(args: argparse.Namespace, config: Config) -> int:
    try:
        module = importlib.import_module("type4me_linux.control_bus")
        client = module.ControlBusClient()
        getattr(client, args.control_method)()
    except Exception as exc:
        print(f"无法连接常驻服务：{exc}", file=sys.stderr)
        return 1
    return 0


def _active_model_ids(config: Config) -> tuple[str, ...]:
    return (
        config.asr.sensevoice_model_id,
        config.asr.vad_model_id,
        config.asr.qwen3_model_id,
    )


def _daemon(args: argparse.Namespace, config: Config) -> int:
    serve(config)
    return 0


def _service(args: argparse.Namespace, config: Config) -> int:
    from .desktop import run

    return run(config=config, background=True, service=True)


def _gui(args: argparse.Namespace, config: Config) -> int:
    from .desktop import run

    return run(config=config, background=args.background)


if __name__ == "__main__":
    raise SystemExit(main())
