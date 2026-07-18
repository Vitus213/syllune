from __future__ import annotations

from dataclasses import replace
from typing import Any
import time

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk

from type4me_linux import desktop
from type4me_linux.app_state import AppState, ModelCheck, ShortcutState
from type4me_linux.config import Config
from type4me_linux.desktop import Type4MeApplication
from type4me_linux.events import RecognitionTranscript
from type4me_linux.history import HistoryPage, HistoryRecord
from type4me_linux.modes import BUILTIN_MODES, Mode


class FakeController:
    def __init__(self) -> None:
        self._state = AppState(modes=BUILTIN_MODES, selected_mode=BUILTIN_MODES[0])
        self.listeners: list[Any] = []
        self.toggle_calls = 0
        self.cancel_calls = 0
        self.close_calls = 0
        self.history_refreshes = 0
        self.model_refreshes = 0
        self.selected_modes: list[str] = []
        self.shortcut_updates: list[tuple[str, frozenset[str], str | None]] = []

    @property
    def state(self) -> AppState:
        return self._state

    def subscribe(self, listener: Any, *, emit_current: bool = True):  # type: ignore[no-untyped-def]
        self.listeners.append(listener)
        if emit_current:
            listener(self._state)

        def unsubscribe() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    def dispatch(self, state: AppState) -> None:
        self._state = state
        for listener in tuple(self.listeners):
            listener(state)

    def toggle(self) -> bool:
        self.toggle_calls += 1
        return True

    def cancel(self) -> bool:
        self.cancel_calls += 1
        return True

    def select_mode(self, identifier: str) -> object:
        self.selected_modes.append(identifier)
        selected = next(mode for mode in self._state.modes if mode.id == identifier)
        self._state = replace(self._state, selected_mode=selected)
        return selected

    def refresh_history(self, *, limit: int = 50, cursor: str | None = None) -> None:
        self.history_refreshes += 1

    def refresh_model_checks(self) -> None:
        self.model_refreshes += 1

    def set_shortcut_state(
        self,
        status: str,
        bound_ids: frozenset[str] = frozenset(),
        *,
        message: str | None = None,
    ) -> None:
        self.shortcut_updates.append((status, bound_ids, message))
        self.dispatch(
            replace(
                self._state,
                shortcuts=ShortcutState(
                    status=status,  # type: ignore[arg-type]
                    bound_ids=bound_ids,
                    message=message,
                ),
            )
        )

    def close(self) -> None:
        self.close_calls += 1


class FakeControlBus:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class FakeShortcuts:
    def __init__(self) -> None:
        self.fallback_message: str | None = None
        self.bound_shortcuts = frozenset({"toggle-recording"})
        self.rebind_result = frozenset({"toggle-recording", "cancel-recording"})
        self.rebind_error: Exception | None = None
        self.rebind_fallback: str | None = None
        self.start_calls = 0
        self.rebind_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def rebind(self) -> None:
        self.rebind_calls += 1
        if self.rebind_error is not None:
            raise self.rebind_error
        self.fallback_message = self.rebind_fallback
        self.bound_shortcuts = self.rebind_result

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class FakeVocabulary:
    def __init__(
        self,
        *,
        hotwords: tuple[str, ...] = ("Type4Me", "NixOS"),
        snippets: dict[str, str] | None = None,
    ) -> None:
        self.hotwords = hotwords
        self.snippets = snippets or {"邮箱": "me@example.com"}
        self.reload_calls = 0
        self.reload_error: Exception | None = None

    def list_hotwords(self) -> tuple[str, ...]:
        return self.hotwords

    def list_snippets(self) -> dict[str, str]:
        return self.snippets

    def reload(self) -> None:
        self.reload_calls += 1
        if self.reload_error is not None:
            raise self.reload_error


def descendants(widget: Gtk.Widget):  # type: ignore[no-untyped-def]
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def button_named(widget: Gtk.Widget, label: str) -> Gtk.Button:
    buttons = [child for child in descendants(widget) if isinstance(child, Gtk.Button)]
    for button in buttons:
        texts = [child.get_label() for child in descendants(button) if isinstance(child, Gtk.Label)]
        if button.get_label() == label or label in texts:
            return button
    assert len(buttons) == 1, f"无法唯一定位按钮 {label!r}: {buttons!r}"
    return buttons[0]


def list_rows(list_box: Gtk.ListBox) -> list[Any]:
    rows: list[Any] = []
    child = list_box.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def drain_main_context() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def action_enabled(application: Type4MeApplication, name: str) -> bool:
    action = application.lookup_action(name)
    assert isinstance(action, Gio.SimpleAction)
    return action.get_enabled()


def test_resident_window_state_navigation_notifications_and_quit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "APPLICATION_ID", "io.github.vitus.Type4Me.Tests.Shell")
    controller = FakeController()
    notifications: list[Gio.Notification] = []
    control_bus = FakeControlBus()
    shortcuts = FakeShortcuts()
    application = Type4MeApplication(
        Config(),
        controller=controller,
        notification_sender=notifications.append,
        control_bus_factory=lambda value: control_bus,
        shortcuts_factory=lambda value: shortcuts,
    )
    assert application.get_application_id() == desktop.APPLICATION_ID
    assert application.register(None)
    assert control_bus.start_calls == 1
    assert shortcuts.start_calls == 1
    window = desktop.Type4MeWindow(
        application=application,
        controller=controller,
        config=Config(),
    )
    application.window = window
    window.set_default_size(520, 420)
    assert window.get_default_size() == (520, 420)
    window.present()
    drain_main_context()
    assert application.is_held
    assert window.get_visible()
    assert set(window.pages) == {
        "live",
        "modes",
        "vocabulary",
        "models",
        "history",
        "settings",
    }
    assert [
        window.view_stack.get_page(window.pages[name]).get_title() for name in window.pages
    ] == [
        "语音输入",
        "模式",
        "词汇",
        "模型",
        "历史",
        "设置",
    ]
    assert controller.history_refreshes == 1
    assert controller.model_refreshes == 1
    assert window.get_size_request() == (520, 420)
    assert window.sidebar.has_css_class("type4me-sidebar")
    assert window.view_stack.has_css_class("type4me-page")
    assert len(window.navigation_rows) == 6
    assert window.navigation_list.get_selected_row() is window.navigation_rows["live"]
    assert not any(isinstance(child, desktop.Adw.ViewSwitcher) for child in descendants(window))
    brand_labels = [
        child.get_text()
        for child in descendants(window.sidebar)
        if isinstance(child, Gtk.Label) and child.has_css_class("type4me-brand")
    ]
    assert brand_labels == ["Type4Me"]
    assert window.transcript_surface.has_css_class("type4me-transcript")
    assert window.transcript_label.get_selectable()
    assert window.transcript_label.get_wrap()
    assert window.live_mode_row.get_selected() == 0
    assert window.start_button.get_action_name() == "app.start"
    assert window.stop_button.get_action_name() == "app.stop"
    assert window.cancel_button.get_action_name() == "app.cancel"
    assert window.start_button.get_tooltip_text() == "开始录音"
    assert window.stop_button.get_tooltip_text() == "停止并完成识别"
    assert window.cancel_button.get_tooltip_text() == "取消本次录音"
    assert window.transcript_label.get_width() > 0
    assert window.start_button.get_width() == window.stop_button.get_width()
    assert window.stop_button.get_width() == window.cancel_button.get_width()
    window.navigation_list.select_row(window.navigation_rows["settings"])
    drain_main_context()
    assert window.view_stack.get_visible_child_name() == "settings"
    window.navigation_list.select_row(window.navigation_rows["live"])
    assert window.transcript_label.get_text() == "准备就绪"
    assert action_enabled(application, "start")
    assert not action_enabled(application, "stop")
    assert not action_enabled(application, "cancel")

    partial = RecognitionTranscript(
        confirmed_segments=("已经确认，",),
        partial_text="正在输入",
        authoritative_text="已经确认，",
        is_final=False,
        backend="sensevoice-vad",
    )
    controller.dispatch(replace(controller.state, session_state="recording", transcript=partial))
    drain_main_context()
    assert window.transcript_label.get_text() == "已经确认，正在输入"
    assert window.status_row.get_subtitle() == "正在录音"
    assert not action_enabled(application, "start")
    assert action_enabled(application, "stop")
    assert action_enabled(application, "cancel")

    final = RecognitionTranscript(
        confirmed_segments=("完成文本",),
        partial_text="",
        authoritative_text="完成文本",
        is_final=True,
        backend="qwen3-sherpa",
    )
    history = HistoryPage(
        records=(
            HistoryRecord(
                id="record-1",
                created_at="2026-07-13T12:00:00Z",
                duration_seconds=1.5,
                raw_text="原始文本",
                processing_mode="quick",
                processed_text="完成文本",
                final_text="完成文本",
                status="completed",
                character_count=4,
                asr_provider="qwen3-sherpa",
                asr_model="qwen3-asr-0.6b-int8",
            ),
        ),
        next_cursor=None,
    )
    controller.dispatch(
        replace(
            controller.state,
            session_state="completed",
            transcript=final,
            history_page=history,
            warnings=("最终校准不可用，已保留本地识别文本。",),
        )
    )
    drain_main_context()
    assert window.transcript_label.get_text() == "完成文本"
    assert window.status_row.get_subtitle() == "识别完成"
    assert window.message_row.get_visible()
    assert "最终校准不可用" in window.message_row.get_subtitle()
    assert action_enabled(application, "start")

    controller.dispatch(replace(controller.state, session_state="error", error="麦克风不可用。"))
    drain_main_context()
    assert window.status_row.get_subtitle() == "识别失败"
    assert window.message_row.get_title() == "错误"
    assert window.message_row.get_subtitle() == "麦克风不可用。"

    class ActiveWindow:
        def is_active(self) -> bool:
            return True

    application.window = ActiveWindow()  # type: ignore[assignment]
    application.notify_when_unfocused("不应发送", "窗口有焦点")
    assert not notifications
    application.window = window
    window.set_visible(False)
    application.notify_when_unfocused("识别完成", "完成文本")
    assert len(notifications) == 1
    assert isinstance(notifications[0], Gio.Notification)
    application.notify_when_unfocused("提醒", "模型回退警告")
    application.notify_when_unfocused("识别失败", "麦克风不可用。")
    assert len(notifications) == 3

    assert window.emit("close-request") is True
    drain_main_context()
    assert application.is_held
    assert not window.get_visible()
    application.activate()
    drain_main_context()
    assert application.window is window
    assert window.get_visible()

    application.activate_action("start", None)
    controller.dispatch(replace(controller.state, session_state="recording", error=None))
    drain_main_context()
    application.activate_action("stop", None)
    application.activate_action("cancel", None)
    assert controller.toggle_calls == 2
    assert controller.cancel_calls == 1

    application.explicit_quit()
    application.explicit_quit()
    drain_main_context()
    assert not application.is_held
    assert controller.close_calls == 1
    assert control_bus.stop_calls == 1
    assert shortcuts.shutdown_calls == 1
    assert not controller.listeners


def test_page_refresh_actions_and_complete_state_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "APPLICATION_ID", "io.github.vitus.Type4Me.Tests.Page")
    controller = FakeController()
    vocabulary = FakeVocabulary()
    control_bus = FakeControlBus()
    shortcuts = FakeShortcuts()
    application = Type4MeApplication(
        Config(),
        controller=controller,
        vocabulary=vocabulary,  # type: ignore[arg-type]
        control_bus_factory=lambda _controller: control_bus,
        shortcuts_factory=lambda _controller: shortcuts,
    )
    assert application.register(None)
    application.activate()
    drain_main_context()
    window = application.window
    assert window is not None

    assert window.hotwords_row.get_subtitle() == "Type4Me、NixOS"
    assert window.snippets_row.get_subtitle() == "邮箱 → me@example.com"
    window.view_stack.set_visible_child_name("vocabulary")
    drain_main_context()
    button_named(window.pages["vocabulary"], "重新加载").emit("clicked")
    window.view_stack.set_visible_child_name("models")
    drain_main_context()
    button_named(window.pages["models"], "检查模型").emit("clicked")
    window.view_stack.set_visible_child_name("history")
    drain_main_context()
    button_named(window.pages["history"], "刷新历史").emit("clicked")
    assert vocabulary.reload_calls == 1
    assert controller.model_refreshes == 2
    assert controller.history_refreshes == 2

    vocabulary.hotwords = ()
    vocabulary.snippets = {}
    window.view_stack.set_visible_child_name("vocabulary")
    drain_main_context()
    button_named(window.pages["vocabulary"], "重新加载").emit("clicked")
    assert window.hotwords_row.get_subtitle() == "暂无热词"
    assert window.snippets_row.get_subtitle() == "暂无语音片段"
    vocabulary.reload_error = RuntimeError("词汇文件损坏")
    button_named(window.pages["vocabulary"], "重新加载").emit("clicked")
    assert window.message_row.get_visible()
    assert window.message_row.get_title() == "词汇加载失败"
    assert window.message_row.get_subtitle() == "词汇文件损坏"

    window.mode_row.set_selected(1)
    drain_main_context()
    controller.dispatch(controller.state)
    drain_main_context()
    assert controller.selected_modes[-1] == "voice-polish"
    assert controller.state.selected_mode == BUILTIN_MODES[1]
    assert window.mode_detail_row.get_subtitle() == "润色中"
    assert window.live_mode_row.get_selected() == 1
    window.live_mode_row.set_selected(0)
    drain_main_context()
    assert controller.selected_modes[-1] == "quick"
    assert window.rebind_shortcuts_button.get_action_name() == "app.rebind-shortcuts"
    assert window.rebind_shortcuts_button.get_tooltip_text() == "绑定或重新绑定全局快捷键"
    application.activate_action("rebind-shortcuts", None)
    drain_main_context()
    time.sleep(0.12)
    drain_main_context()
    assert shortcuts.rebind_calls == 1
    assert controller.shortcut_updates[-1] == (
        "bound",
        frozenset({"toggle-recording", "cancel-recording"}),
        None,
    )
    assert action_enabled(application, "rebind-shortcuts")
    assert window.shortcut_row.get_subtitle() == "已绑定 · 取消录音、切换录音"
    assert window.rebind_shortcuts_button.get_label() == "重新绑定快捷键"
    shortcuts.rebind_result = frozenset()
    shortcuts.rebind_fallback = "用户取消了全局快捷键授权。"
    application.activate_action("rebind-shortcuts", None)
    drain_main_context()
    time.sleep(0.12)
    drain_main_context()
    assert shortcuts.rebind_calls == 2
    assert controller.shortcut_updates[-1] == (
        "unavailable",
        frozenset(),
        "用户取消了全局快捷键授权。 请在 Sway 配置中保留备用快捷键。",
    )
    assert "Sway" in window.shortcut_row.get_subtitle()
    assert window.rebind_shortcuts_button.get_label() == "绑定快捷键"

    direct_mode = Mode(
        id="direct-test",
        name="直接测试",
        prompt="",
        processing_label="",
        builtin=False,
        sort_order=10,
    )
    controller.dispatch(replace(controller.state, modes=(direct_mode,), selected_mode=direct_mode))
    drain_main_context()
    assert window.mode_detail_row.get_subtitle() == "直接输入"
    controller.dispatch(replace(controller.state, modes=(), selected_mode=None))
    drain_main_context()
    assert window.mode_row.get_model().get_n_items() == 0

    status_labels = {
        "idle": "准备就绪",
        "starting": "正在启动录音",
        "recording": "正在录音",
        "stopping": "正在完成识别",
        "completed": "识别完成",
        "error": "识别失败",
        "cancelled": "已取消",
    }
    for status, label in status_labels.items():
        controller.dispatch(
            replace(
                controller.state,
                session_state=status,  # type: ignore[arg-type]
                transcript=None,
                warnings=(),
                error=None,
            )
        )
        drain_main_context()
        assert window.status_row.get_subtitle() == label
        assert action_enabled(application, "start") is (
            status not in {"starting", "recording", "stopping"}
        )
        assert action_enabled(application, "stop") is (
            status in {"starting", "recording", "stopping"}
        )
        assert action_enabled(application, "cancel") is (
            status in {"starting", "recording", "stopping"}
        )
        assert not window.message_row.get_visible()
        assert window.transcript_label.get_text() == "准备就绪"

    confirmed_only = RecognitionTranscript(
        confirmed_segments=("第一段", "第二段"),
        partial_text="追加",
        authoritative_text="",
        is_final=False,
        backend="sensevoice-vad",
    )
    context = GLib.MainContext.default()
    assert context.acquire()
    try:
        controller.dispatch(replace(controller.state, transcript=confirmed_only))
    finally:
        context.release()
    assert window.transcript_label.get_text() == "第一段第二段追加"

    model_checks = (
        ModelCheck(id="完整版本", installed=True, ok=True, version="1.2.3"),
        ModelCheck(id="完整无版本", installed=True, ok=True),
        ModelCheck(id="损坏模型", installed=True, ok=False, corrupt=("encoder.onnx",)),
        ModelCheck(id="缺失模型", installed=False, ok=False),
    )
    failed_history = HistoryRecord(
        id="record-failed",
        created_at="2026-07-13T13:00:00Z",
        duration_seconds=None,
        raw_text="",
        processing_mode=None,
        processed_text=None,
        final_text="",
        status="failed",
        character_count=0,
        asr_provider=None,
        asr_model=None,
    )
    controller.dispatch(
        replace(
            controller.state,
            model_checks=model_checks,
            history_page=HistoryPage(records=(failed_history,), next_cursor="next"),
        )
    )
    drain_main_context()
    model_rows = list_rows(window.models_box)
    assert [(row.get_title(), row.get_subtitle()) for row in model_rows] == [
        ("完整版本", "1.2.3"),
        ("完整无版本", "模型完整"),
        ("损坏模型", "模型校验失败"),
        ("缺失模型", "尚未安装"),
    ]
    history_rows = list_rows(window.history_box)
    assert [(row.get_title(), row.get_subtitle()) for row in history_rows] == [
        ("识别失败", "2026-07-13T13:00:00Z")
    ]

    controller.dispatch(
        replace(
            controller.state,
            model_checks=(),
            history_page=HistoryPage(records=(), next_cursor=None),
        )
    )
    drain_main_context()
    assert list_rows(window.models_box)[0].get_title() == "尚无检查结果"
    assert list_rows(window.history_box)[0].get_title() == "暂无识别历史"

    shortcut_labels = {
        "unbound": "尚未绑定",
        "binding": "正在请求绑定",
        "bound": "已绑定",
        "unavailable": "当前桌面不可用",
    }
    for status, label in shortcut_labels.items():
        controller.dispatch(
            replace(
                controller.state,
                shortcuts=ShortcutState(
                    status=status,  # type: ignore[arg-type]
                    message="使用 Sway 备用键" if status == "unavailable" else None,
                ),
            )
        )
        drain_main_context()
        expected = f"{label} · 使用 Sway 备用键" if status == "unavailable" else label
        assert window.shortcut_row.get_subtitle() == expected

    application.explicit_quit()
    application.do_shutdown()
    application.do_shutdown()
    assert controller.close_calls == 1
    assert control_bus.stop_calls == 1
    assert shortcuts.shutdown_calls == 1


def test_background_activation_factory_fallback_and_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "APPLICATION_ID", "io.github.vitus.Type4Me.Tests.Background")
    controller = FakeController()
    control_bus = FakeControlBus()
    shortcuts = FakeShortcuts()

    class BrokenVocabulary:
        def __init__(self, _paths: object) -> None:
            raise RuntimeError("无法读取词汇")

    monkeypatch.setattr(desktop, "VocabularyService", BrokenVocabulary)
    application = Type4MeApplication(
        Config(),
        background=True,
        controller=controller,
        control_bus_factory=lambda _controller: control_bus,
        shortcuts_factory=lambda _controller: shortcuts,
    )
    assert application.register(None)
    application.activate()
    drain_main_context()
    window = application.window
    assert window is not None
    assert not window.get_visible()
    assert window.hotwords_row.get_subtitle() == "尚未加载"
    window._on_vocabulary_refresh(window.start_button)
    assert window.hotwords_row.get_subtitle() == "尚未加载"

    application.activate()
    drain_main_context()
    assert window.get_visible()
    window.set_visible(False)
    application.activate_action("show-window", None)
    drain_main_context()
    assert window.get_visible()
    application.explicit_quit()

    monkeypatch.setattr(desktop, "APPLICATION_ID", "io.github.vitus.Type4Me.Tests.Present")
    second_controller = FakeController()
    second = Type4MeApplication(
        Config(),
        controller=second_controller,
        vocabulary=FakeVocabulary(),  # type: ignore[arg-type]
        control_bus_factory=lambda _controller: FakeControlBus(),
        shortcuts_factory=lambda _controller: FakeShortcuts(),
    )
    assert second.register(None)
    assert second.window is None
    second.present_window()
    drain_main_context()
    assert second.window is not None
    assert second.window.get_visible()
    second.explicit_quit()


def test_controller_and_resident_factory_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "APPLICATION_ID", "io.github.vitus.Type4Me.Tests.Factory")
    created: dict[str, Any] = {}
    controller = FakeController()

    def controller_factory(**kwargs: Any) -> FakeController:
        created.update(kwargs)
        return controller

    fallback_shortcuts = FakeShortcuts()
    fallback_shortcuts.fallback_message = "Portal 不可用"
    fallback_shortcuts.bound_shortcuts = frozenset()
    application = Type4MeApplication(
        Config(),
        controller_factory=controller_factory,
        control_bus_factory=lambda _controller: FakeControlBus(),
        shortcuts_factory=lambda _controller: fallback_shortcuts,
    )
    assert application.controller is controller
    assert callable(created["scheduler"])
    assert callable(created["notifier"])
    assert callable(created["show_window_callback"])
    assert application.register(None)
    assert controller.shortcut_updates[-1] == (
        "unavailable",
        frozenset(),
        "Portal 不可用 请在 Sway 配置中保留备用快捷键。",
    )
    application.explicit_quit()

    def broken_factory(**_kwargs: Any) -> FakeController:
        raise RuntimeError("控制器创建失败")

    broken = Type4MeApplication(Config(), controller_factory=broken_factory)
    with pytest.raises(RuntimeError, match="控制器创建失败"):
        _ = broken.controller

    bound_controller = FakeController()
    bound_shortcuts = FakeShortcuts()
    bound = Type4MeApplication(
        Config(),
        controller=bound_controller,
        control_bus_factory=lambda _controller: FakeControlBus(),
        shortcuts_factory=lambda _controller: bound_shortcuts,
    )
    bound._start_resident_integrations()
    assert bound_controller.shortcut_updates[-1] == (
        "bound",
        frozenset({"toggle-recording"}),
        None,
    )
    bound._shutdown_resident_integrations()

    binding_controller = FakeController()
    binding_shortcuts = FakeShortcuts()
    binding_shortcuts.bound_shortcuts = frozenset()
    binding = Type4MeApplication(
        Config(),
        controller=binding_controller,
        control_bus_factory=lambda _controller: FakeControlBus(),
        shortcuts_factory=lambda _controller: binding_shortcuts,
    )
    binding._start_resident_integrations()
    assert binding_controller.shortcut_updates[-1] == ("binding", frozenset(), None)
    binding_shortcuts.bound_shortcuts = frozenset({"hold-to-talk"})
    time.sleep(0.12)
    drain_main_context()
    assert binding_controller.shortcut_updates[-1] == (
        "bound",
        frozenset({"hold-to-talk"}),
        None,
    )
    assert binding._shortcut_poll_source == 0
    binding._shutdown_resident_integrations()

    unavailable_controller = FakeController()
    unavailable_shortcuts = FakeShortcuts()
    unavailable_shortcuts.bound_shortcuts = frozenset()
    unavailable = Type4MeApplication(
        Config(),
        controller=unavailable_controller,
        control_bus_factory=lambda _controller: FakeControlBus(),
        shortcuts_factory=lambda _controller: unavailable_shortcuts,
    )
    unavailable._start_resident_integrations()
    unavailable_shortcuts.fallback_message = "全局快捷键请求未完成，请使用备用快捷键。"
    time.sleep(0.12)
    drain_main_context()
    assert unavailable_controller.shortcut_updates[-1] == (
        "unavailable",
        frozenset(),
        "全局快捷键请求未完成，请使用备用快捷键。 请在 Sway 配置中保留备用快捷键。",
    )
    assert unavailable._shortcut_poll_source == 0
    unavailable._shutdown_resident_integrations()

    service = Type4MeApplication(Config(), service=True, controller=FakeController())
    assert service.get_flags() & Gio.ApplicationFlags.IS_SERVICE

    sent_notifications: list[tuple[str | None, Gio.Notification]] = []

    class RecordingApplication(Type4MeApplication):
        def send_notification(
            self,
            identifier: str | None,
            notification: Gio.Notification,
        ) -> None:
            sent_notifications.append((identifier, notification))

    sender = RecordingApplication(Config(), controller=FakeController())
    sender.notify_when_unfocused("识别完成", "后台文本")
    assert len(sent_notifications) == 1
    assert sent_notifications[0][0] is None


def test_run_returns_application_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[Config, bool, bool]] = []

    class FakeApplication:
        def __init__(
            self, config: Config, *, background: bool = False, service: bool = False
        ) -> None:
            observed.append((config, background, service))

        def run(self, arguments: object) -> int:
            assert arguments is None
            return 23

    monkeypatch.setattr(desktop, "Type4MeApplication", FakeApplication)
    config = Config()
    assert desktop.run(config, background=True) == 23
    assert desktop.run(config, background=True, service=True) == 23
    assert observed == [(config, True, False), (config, True, True)]
