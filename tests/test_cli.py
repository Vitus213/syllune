from __future__ import annotations

import io
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

from type4me_linux.cli import _with_backend, main
from type4me_linux.config import Config
from type4me_linux.doctor import Check
from type4me_linux.events import RecognitionEvent, RecognitionTranscript
from type4me_linux.inject import InjectionResult
from type4me_linux.modes import Mode
from type4me_linux.providers import RecognitionResult


@pytest.mark.parametrize(
    ("argv", "expected_error"),
    [
        (["transcribe"], "缺少必需参数：wav"),
        (
            ["transcribe", "/tmp/input.wav", "--backend", "unknown"],
            "参数 --backend：无效选项 'unknown'（可选值：fake, sensevoice, qwen3-sherpa, hybrid）",
        ),
        (["record", "--seconds", "later"], "参数 --seconds：无法将 'later' 解析为浮点数。"),
        (["record", "--seconds"], "参数 --seconds：必须提供一个值。"),
        (["doctor", "--unknown"], "无法识别的参数：--unknown"),
    ],
)
def test_argument_errors_are_localized(
    argv: list[str], expected_error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2
    assert expected_error in capsys.readouterr().err


def _xdg(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HOME", str(tmp_path))
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))


def test_doctor_allows_only_uninstalled_model_payloads(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.run_checks",
        lambda config: [
            Check("命令 wtype", True, "/bin/wtype", "executable"),
            Check("SenseVoice 模型", False, "模型尚未安装。", "model", True),
            Check("Qwen3-ASR 模型", False, "模型尚未安装。", "model", True),
        ],
    )

    code = main(["doctor", "--allow-missing-models"])

    output = capsys.readouterr().out
    assert code == 0
    assert "缺失   SenseVoice 模型" in output


def test_doctor_requires_models_without_allow_flag(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.run_checks",
        lambda config: [
            Check("SenseVoice 模型", False, "模型尚未安装。", "model", True),
        ],
    )

    assert main(["doctor"]) == 1
    assert "模型尚未安装" in capsys.readouterr().out


def test_doctor_allow_missing_does_not_hide_runtime_failure(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.run_checks",
        lambda config: [
            Check("命令 wtype", False, "PATH 中未找到 wtype。", "executable"),
            Check("SenseVoice 模型", False, "模型尚未安装。", "model", True),
        ],
    )

    assert main(["doctor", "--allow-missing-models"]) == 1
    assert "PATH 中未找到 wtype" in capsys.readouterr().out


def test_doctor_allow_missing_does_not_hide_corrupt_model(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.run_checks",
        lambda config: [
            Check("SenseVoice 模型", False, "损坏文件：model.int8.onnx", "model", False),
        ],
    )

    assert main(["doctor", "--allow-missing-models"]) == 1
    assert "损坏文件" in capsys.readouterr().out


def test_transcribe_preserves_batch_json_keys(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _xdg(monkeypatch, tmp_path)
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"fake")

    code = main(["transcribe", str(wav_path), "--backend", "fake", "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "text": "测试语音输入",
        "backend": "fake",
        "draft_text": None,
        "injection": None,
    }


def test_record_uses_batch_backend_recorder_and_injector(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: dict[str, object] = {}

    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            calls["backend"] = config.asr.batch_backend

        def run_once(self, *, record_seconds: float, inject: bool):  # type: ignore[no-untyped-def]
            calls["record_seconds"] = record_seconds
            calls["inject"] = inject
            return SimpleNamespace(
                recognition=RecognitionResult("录音文本", "test"),
                injection=InjectionResult("test", True),
            )

    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)

    code = main(["record", "--seconds", "1.5", "--backend", "fake"])

    assert code == 0
    assert capsys.readouterr().out == "录音文本\n"
    assert calls == {"backend": "fake", "record_seconds": 1.5, "inject": True}


def test_backend_override_reuses_one_function_for_batch_and_stream() -> None:
    config = Config()

    batch = _with_backend(config, "sensevoice")
    stream = _with_backend(config, "sensevoice-vad", streaming=True)

    assert batch.asr.batch_backend == "sensevoice"
    assert batch.asr.streaming_backend == config.asr.streaming_backend
    assert stream.asr.streaming_backend == "sensevoice-vad"
    assert stream.asr.batch_backend == config.asr.batch_backend


def test_inject_returns_failure_for_failed_output(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    class _Injector:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def inject(self, text: str) -> InjectionResult:
            return InjectionResult("wtype", False, f"注入失败：{text}")

    monkeypatch.setattr("type4me_linux.cli.TextInjector", _Injector)

    code = main(["inject", "hello"])

    assert code == 1
    assert capsys.readouterr().err == "注入失败：hello\n"


def test_gui_dispatches_to_desktop_runner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, bool]] = []
    module = SimpleNamespace(
        run=lambda *, config, background: calls.append((config, background)) or 7
    )
    monkeypatch.setitem(sys.modules, "type4me_linux.desktop", module)

    code = main(["gui", "--background"])

    assert code == 7
    assert len(calls) == 1
    assert calls[0][1] is True


def test_service_dispatches_to_desktop_service_runner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, bool, bool]] = []
    module = SimpleNamespace(
        run=lambda *, config, background, service: calls.append((config, background, service)) or 9
    )
    monkeypatch.setitem(sys.modules, "type4me_linux.desktop", module)

    code = main(["service"])

    assert code == 9
    assert len(calls) == 1
    assert calls[0][1:] == (True, True)


def test_control_commands_use_lazy_resident_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    class _Client:
        def toggle(self) -> None:
            calls.append("toggle")

        def hold_start(self) -> None:
            calls.append("hold_start")

        def hold_stop(self) -> None:
            calls.append("hold_stop")

        def cancel(self) -> None:
            calls.append("cancel")

    module = SimpleNamespace(ControlBusClient=_Client)
    real_import = __import__("importlib").import_module
    monkeypatch.setattr(
        "type4me_linux.cli.importlib.import_module",
        lambda name: module if name == "type4me_linux.control_bus" else real_import(name),
    )

    assert main(["toggle"]) == 0
    assert main(["hold-start"]) == 0
    assert main(["hold-stop"]) == 0
    assert main(["cancel"]) == 0
    assert calls == ["toggle", "hold_start", "hold_stop", "cancel"]


def test_control_command_does_not_start_local_pipeline(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.importlib.import_module",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )
    monkeypatch.setattr(
        "type4me_linux.cli.VoiceInputPipeline",
        lambda config: (_ for _ in ()).throw(AssertionError("不应启动本地录音")),
    )

    assert main(["toggle"]) == 1
    assert "无法连接常驻服务" in capsys.readouterr().err


def test_model_cli_dispatches_install_check_and_remove(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, ...]] = []

    class _Manager:
        def __init__(self, paths, active_model_ids) -> None:  # type: ignore[no-untyped-def]
            calls.append(("init", tuple(active_model_ids())))

        def install(self, model_id: str) -> Path:
            calls.append(("install", model_id))
            return Path("/models") / model_id

        def check(self, model_id: str) -> dict[str, object]:
            calls.append(("check", model_id))
            return {
                "id": model_id,
                "ok": True,
                "missing": [],
                "extra": [],
                "corrupt": [],
                "errors": [],
            }

        def remove(self, model_id: str, *, force: bool) -> bool:
            calls.append(("remove", model_id, force))
            return True

    monkeypatch.setattr("type4me_linux.cli.AppPaths.from_environment", lambda: object())
    monkeypatch.setattr("type4me_linux.cli.ModelManager", _Manager)

    assert main(["model", "install", "sensevoice-int8"]) == 0
    assert main(["model", "check", "sensevoice-int8", "--json"]) == 0
    assert main(["model", "remove", "sensevoice-int8", "--force"]) == 0
    assert ("install", "sensevoice-int8") in calls
    assert ("check", "sensevoice-int8") in calls
    assert ("remove", "sensevoice-int8", True) in calls
    capsys.readouterr()


def test_mode_vocabulary_and_history_crud_dispatch(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    mode = Mode(
        id="00000000-0000-0000-0000-000000000001",
        name="自定义",
        prompt="{text}",
        processing_label="处理",
        builtin=False,
        sort_order=4,
    )
    mode_calls: list[tuple[object, ...]] = []

    class _Modes:
        def __init__(self, paths) -> None:  # type: ignore[no-untyped-def]
            pass

        def add(self, name, prompt, label, *, sort_order):  # type: ignore[no-untyped-def]
            mode_calls.append((name, prompt, label, sort_order))
            return mode

    vocabulary_calls: list[tuple[str, ...]] = []

    class _Vocabulary:
        def __init__(self, paths) -> None:  # type: ignore[no-untyped-def]
            pass

        def add_snippet(self, key: str, value: str) -> dict[str, str]:
            vocabulary_calls.append((key, value))
            return {key: value}

    class _History:
        def __init__(self, paths) -> None:  # type: ignore[no-untyped-def]
            pass

        def delete_many(self, ids):  # type: ignore[no-untyped-def]
            return len(tuple(ids))

    monkeypatch.setattr("type4me_linux.cli.AppPaths.from_environment", lambda: object())
    monkeypatch.setattr("type4me_linux.cli.ModesRepository", _Modes)
    monkeypatch.setattr("type4me_linux.cli.VocabularyService", _Vocabulary)
    monkeypatch.setattr("type4me_linux.cli.HistoryStore", _History)

    assert main(["mode", "add", "自定义", "--prompt", "{text}"]) == 0
    assert main(["vocabulary", "snippets", "add", "邮箱", "me@example.com"]) == 0
    assert main(["history", "delete", "a", "b"]) == 0
    assert mode_calls == [("自定义", "{text}", "", None)]
    assert vocabulary_calls == [("邮箱", "me@example.com")]
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"deleted": 2}


def test_main_reports_unexpected_command_failure(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "type4me_linux.cli.load_config",
        lambda _path: (_ for _ in ()).throw(RuntimeError("配置损坏")),
    )

    assert main(["doctor"]) == 1
    assert capsys.readouterr().err == "操作失败：配置损坏\n"


def test_transcribe_plain_output_preserves_failed_injection_exit(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_once(self, *, audio_path: Path, inject: bool):  # type: ignore[no-untyped-def]
            assert audio_path == Path("input.wav")
            assert inject is True
            return SimpleNamespace(
                recognition=RecognitionResult("仍输出转写", "fake"),
                injection=InjectionResult("wtype", False, "目标不可用"),
            )

    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)

    assert main(["transcribe", "input.wav", "--inject"]) == 1
    assert capsys.readouterr().out == "仍输出转写\n"


def test_inject_success_has_no_output(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    class _Injector:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def inject(self, text: str) -> InjectionResult:
            assert text == "你好"
            return InjectionResult("wtype", True, "")

    monkeypatch.setattr("type4me_linux.cli.TextInjector", _Injector)

    assert main(["inject", "你好"]) == 0
    assert capsys.readouterr() == ("", "")


def test_model_list_update_plain_check_and_absent_remove(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, ...]] = []

    class _Manager:
        def __init__(self, paths, active_model_ids) -> None:  # type: ignore[no-untyped-def]
            calls.append(("active", *active_model_ids()))

        def check(self, model_id: str) -> dict[str, object]:
            calls.append(("check", model_id))
            failed = model_id == "sensevoice-int8"
            return {
                "id": model_id,
                "ok": not failed,
                "missing": ["model.int8.onnx"] if failed else [],
                "extra": [],
                "corrupt": [],
                "errors": ["清单错误"] if failed else [],
            }

        def update(self, model_id: str) -> Path:
            calls.append(("update", model_id))
            return Path("/updated") / model_id

        def remove(self, model_id: str, *, force: bool) -> bool:
            calls.append(("remove", model_id, force))
            return False

    monkeypatch.setattr("type4me_linux.cli.AppPaths.from_environment", lambda: object())
    monkeypatch.setattr("type4me_linux.cli.ModelManager", _Manager)

    assert main(["model", "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in listing} == {
        "sensevoice-int8",
        "silero-vad",
        "qwen3-asr-0.6b-int8",
    }

    assert main(["model", "update", "silero-vad"]) == 0
    assert capsys.readouterr().out == "/updated/silero-vad\n"

    assert main(["model", "check", "sensevoice-int8"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "模型校验失败",
        "missing: model.int8.onnx",
        "errors: 清单错误",
    ]

    assert main(["model", "remove", "silero-vad"]) == 0
    assert capsys.readouterr().out == "模型未安装\n"
    assert ("update", "silero-vad") in calls
    assert ("remove", "silero-vad", False) in calls


def test_interactive_stream_renders_partial_warning_and_one_final(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    partial = RecognitionTranscript((), "正在识别", "", False, "sensevoice-vad")
    final = RecognitionTranscript(("最终文本",), "", "最终文本", True, "hybrid")

    class _Session:
        def __init__(self, sink) -> None:  # type: ignore[no-untyped-def]
            self._sink = sink

        def run(self) -> None:
            self._sink(RecognitionEvent("transcript", 1, transcript=partial))
            self._sink(RecognitionEvent("warning", 2, message="校准失败，保留草稿"))
            self._sink(RecognitionEvent("transcript", 3, transcript=final))
            self._sink(RecognitionEvent("finalized", 4, transcript=final))

        def stop(self) -> None:
            raise AssertionError("不应停止")

        def cancel(self) -> None:
            raise AssertionError("不应取消")

    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def create_session(self, request):  # type: ignore[no-untyped-def]
            return _Session(request.event_sink)

    terminal = io.StringIO()
    terminal.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)
    monkeypatch.setattr("type4me_linux.cli.sys.stderr", terminal)

    assert main(["stream", "--no-inject"]) == 0
    assert capsys.readouterr().out == "最终文本\n"
    assert terminal.getvalue() == "\r正在识别\n校准失败，保留草稿\n\n"


def test_stream_restores_signal_handlers_when_session_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    original = {signal.SIGINT: object(), signal.SIGTERM: object()}
    installed = dict(original)
    signal_calls: list[tuple[int, object]] = []

    def fake_signal(signum: int, handler: object) -> object:
        previous = installed[signum]
        installed[signum] = handler
        signal_calls.append((signum, handler))
        return previous

    class _Session:
        def run(self) -> None:
            raise RuntimeError("采集线程失败")

        def stop(self) -> None:
            pass

        def cancel(self) -> None:
            pass

    class _Pipeline:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def create_session(self, request):  # type: ignore[no-untyped-def]
            return _Session()

    monkeypatch.setattr("type4me_linux.cli.signal.signal", fake_signal)
    monkeypatch.setattr("type4me_linux.cli.VoiceInputPipeline", _Pipeline)

    assert main(["stream", "--json"]) == 1
    assert installed == original
    assert len(signal_calls) == 4


def test_daemon_command_delegates_to_server(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    served: list[Config] = []
    monkeypatch.setattr("type4me_linux.cli.serve", served.append)

    assert main(["daemon"]) == 0
    assert len(served) == 1
    assert isinstance(served[0], Config)


def test_control_command_reports_resident_method_failure(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    class _Client:
        def toggle(self) -> None:
            raise RuntimeError("D-Bus 名称无所有者")

    module = SimpleNamespace(ControlBusClient=_Client)
    monkeypatch.setattr(
        "type4me_linux.cli.importlib.import_module",
        lambda name: module,
    )

    assert main(["toggle"]) == 1
    assert capsys.readouterr().err == "无法连接常驻服务：D-Bus 名称无所有者\n"
