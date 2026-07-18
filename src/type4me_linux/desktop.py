from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .app_state import AppState
from .config import Config
from .controller import ApplicationController
from .history import HistoryStore
from .model_manager import ModelManager
from .modes import ModesRepository
from .paths import AppPaths
from .pipeline import VoiceInputPipeline
from .vocabulary import VocabularyService

APPLICATION_ID = "io.github.vitus.Type4Me"

APPLICATION_CSS = b"""
.type4me-shell { background: @window_bg_color; }
.type4me-sidebar {
  background: @sidebar_bg_color;
  border-right: 1px solid alpha(@window_fg_color, 0.12);
  min-width: 156px;
}
.type4me-brand { font-size: 20px; font-weight: 700; }
.type4me-brand-subtitle { color: alpha(@window_fg_color, 0.65); }
.type4me-navigation { background: transparent; }
.type4me-navigation row { margin: 2px 8px; border-radius: 6px; }
.type4me-navigation row:selected { background: alpha(@accent_bg_color, 0.16); }
.type4me-page { background: @window_bg_color; }
.type4me-transcript {
  background: @view_bg_color;
  border: 1px solid alpha(@window_fg_color, 0.16);
  border-radius: 6px;
  min-height: 190px;
}
.type4me-transcript label { font-size: 17px; }
.type4me-status { background: alpha(@window_fg_color, 0.055); }
.type4me-message { background: alpha(@warning_bg_color, 0.14); }
.type4me-controls button { min-width: 84px; min-height: 38px; }
.type4me-empty { color: alpha(@window_fg_color, 0.65); }
"""


class Controller(Protocol):
    @property
    def state(self) -> AppState: ...

    def subscribe(
        self, listener: Callable[[AppState], object], *, emit_current: bool = True
    ) -> Callable[[], None]: ...

    def toggle(self) -> bool: ...

    def cancel(self) -> bool: ...

    def select_mode(self, identifier: str) -> object: ...

    def refresh_history(self, *, limit: int = 50, cursor: str | None = None) -> None: ...

    def refresh_model_checks(self) -> None: ...

    def close(self) -> None: ...


ControllerFactory = Callable[..., Controller]
NotificationSender = Callable[[Gio.Notification], object]
ControlBusFactory = Callable[[Controller], object]
ShortcutsFactory = Callable[[Controller], object]


class ResidentService(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class ShortcutService(Protocol):
    fallback_message: str | None
    bound_shortcuts: frozenset[str]

    def start(self) -> None: ...

    def rebind(self) -> None: ...

    def shutdown(self) -> None: ...


class Type4MeWindow(Adw.ApplicationWindow):
    """Type4Me 的单实例 Adwaita 主窗口。"""

    PAGE_TITLES = (
        ("live", "语音输入", "audio-input-microphone-symbolic"),
        ("modes", "模式", "view-list-symbolic"),
        ("vocabulary", "词汇", "accessories-dictionary-symbolic"),
        ("models", "模型", "folder-download-symbolic"),
        ("history", "历史", "document-open-recent-symbolic"),
        ("settings", "设置", "preferences-system-symbolic"),
    )

    def __init__(
        self,
        *,
        application: Type4MeApplication,
        controller: Controller,
        vocabulary: VocabularyService | None = None,
        config: Config | None = None,
    ) -> None:
        super().__init__(application=application, title="Type4Me")
        self.set_default_size(920, 660)
        self.set_size_request(520, 420)
        self._controller = controller
        self._vocabulary = vocabulary
        self._config = config or Config()
        self._mode_ids: list[str] = []

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_hexpand(True)
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hhomogeneous(False)
        self.view_stack.set_vhomogeneous(False)
        self.view_stack.add_css_class("type4me-page")
        self.pages: dict[str, Gtk.Widget] = {}
        builders = {
            "live": self._build_live_page,
            "modes": self._build_modes_page,
            "vocabulary": self._build_vocabulary_page,
            "models": self._build_models_page,
            "history": self._build_history_page,
            "settings": self._build_settings_page,
        }
        for name, title, icon in self.PAGE_TITLES:
            page = builders[name]()
            self.pages[name] = page
            stack_page = self.view_stack.add_titled_with_icon(page, name, title, icon)
            stack_page.set_use_underline(False)

        self.sidebar = self._build_sidebar()
        self.content_scroller = Gtk.ScrolledWindow()
        self.content_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.content_scroller.set_hexpand(True)
        self.content_scroller.set_vexpand(True)
        self.content_scroller.set_child(self.view_stack)
        shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        shell.add_css_class("type4me-shell")
        shell.append(self.sidebar)
        shell.append(self.content_scroller)
        self.set_content(shell)

        self.connect("close-request", self._on_close_request)
        self._unsubscribe = controller.subscribe(self._queue_state, emit_current=True)
        controller.refresh_history()
        controller.refresh_model_checks()

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.set_size_request(156, -1)
        sidebar.add_css_class("type4me-sidebar")

        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        identity.set_margin_top(20)
        identity.set_margin_start(16)
        identity.set_margin_end(12)
        brand = Gtk.Label(label="Type4Me", xalign=0)
        brand.add_css_class("type4me-brand")
        subtitle = Gtk.Label(label="桌面语音输入", xalign=0)
        subtitle.add_css_class("type4me-brand-subtitle")
        identity.append(brand)
        identity.append(subtitle)
        sidebar.append(identity)

        self.navigation_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.navigation_list.add_css_class("type4me-navigation")
        self.navigation_list.set_accessible_role(Gtk.AccessibleRole.NAVIGATION)
        self.navigation_list.connect("row-selected", self._on_navigation_selected)
        self.navigation_rows: dict[str, Gtk.ListBoxRow] = {}
        for name, title, icon in self.PAGE_TITLES:
            row = Gtk.ListBoxRow()
            row.set_name(name)
            row.set_tooltip_text(title)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            content.set_margin_top(8)
            content.set_margin_bottom(8)
            content.set_margin_start(10)
            content.set_margin_end(10)
            content.append(Gtk.Image.new_from_icon_name(icon))
            label = Gtk.Label(label=title, xalign=0)
            label.set_hexpand(True)
            content.append(label)
            row.set_child(content)
            self.navigation_list.append(row)
            self.navigation_rows[name] = row
        sidebar.append(self.navigation_list)
        self.navigation_list.select_row(self.navigation_rows["live"])

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)
        resident = Gtk.Label(label="常驻运行", xalign=0)
        resident.set_margin_start(18)
        resident.set_margin_bottom(14)
        resident.add_css_class("type4me-brand-subtitle")
        sidebar.append(resident)
        return sidebar

    def _new_page(self, title: str, icon_name: str) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=title, icon_name=icon_name)
        page.add_css_class("type4me-page")
        return page

    @staticmethod
    def _prefix_row(row: Adw.ActionRow, icon_name: str) -> Adw.ActionRow:
        row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
        return row

    def _build_live_page(self) -> Gtk.Widget:
        page = self._new_page("语音输入", "audio-input-microphone-symbolic")

        workflow = Adw.PreferencesGroup(
            title="实时转写", description="录音内容会持续显示在下方，可随时选择和复制。"
        )
        transcript_scroll = Gtk.ScrolledWindow()
        transcript_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        transcript_scroll.set_min_content_height(190)
        transcript_scroll.set_max_content_height(320)
        transcript_scroll.set_propagate_natural_height(True)
        transcript_scroll.add_css_class("type4me-transcript")
        self.transcript_surface = transcript_scroll
        self.transcript_label = Gtk.Label(label="准备就绪")
        self.transcript_label.set_wrap(True)
        self.transcript_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.transcript_label.set_selectable(True)
        self.transcript_label.set_xalign(0)
        self.transcript_label.set_yalign(0)
        self.transcript_label.set_margin_top(16)
        self.transcript_label.set_margin_bottom(16)
        self.transcript_label.set_margin_start(16)
        self.transcript_label.set_margin_end(16)
        transcript_scroll.set_child(self.transcript_label)
        workflow.add(transcript_scroll)

        self.status_row = Adw.ActionRow(title="录音状态", subtitle="准备就绪")
        self.status_row.add_css_class("type4me-status")
        self.status_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        self.status_row.add_prefix(self.status_icon)
        workflow.add(self.status_row)

        self.live_mode_row = Adw.ComboRow(title="当前模式", subtitle="转写后处理方式")
        self.live_mode_row.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
        self.live_mode_row.connect("notify::selected", self._on_mode_selected)
        workflow.add(self.live_mode_row)

        self.message_row = Adw.ActionRow(title="提醒")
        self.message_row.set_visible(False)
        self.message_row.add_css_class("type4me-message")
        self.message_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        workflow.add(self.message_row)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_halign(Gtk.Align.CENTER)
        controls.set_homogeneous(True)
        controls.set_margin_top(8)
        controls.add_css_class("type4me-controls")
        self.start_button = Gtk.Button(label="开始", icon_name="media-record-symbolic")
        self.start_button.set_tooltip_text("开始录音")
        self.start_button.add_css_class("suggested-action")
        self.start_button.set_action_name("app.start")
        self.stop_button = Gtk.Button(label="停止", icon_name="media-playback-stop-symbolic")
        self.stop_button.set_tooltip_text("停止并完成识别")
        self.stop_button.set_action_name("app.stop")
        self.cancel_button = Gtk.Button(label="取消", icon_name="process-stop-symbolic")
        self.cancel_button.set_tooltip_text("取消本次录音")
        self.cancel_button.add_css_class("destructive-action")
        self.cancel_button.set_action_name("app.cancel")
        controls.append(self.start_button)
        controls.append(self.stop_button)
        controls.append(self.cancel_button)
        workflow.add(controls)
        page.add(workflow)
        return page

    def _build_modes_page(self) -> Gtk.Widget:
        page = self._new_page("模式", "view-list-symbolic")
        group = Adw.PreferencesGroup(
            title="文本处理模式", description="选择语音转写完成后的文本处理方式。"
        )
        self.mode_row = Adw.ComboRow(title="默认模式")
        self.mode_row.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
        self.mode_row.connect("notify::selected", self._on_mode_selected)
        group.add(self.mode_row)
        self.mode_detail_row = self._prefix_row(
            Adw.ActionRow(title="处理方式", subtitle="快速输入"),
            "document-edit-symbolic",
        )
        group.add(self.mode_detail_row)
        page.add(group)
        return page

    def _build_vocabulary_page(self) -> Gtk.Widget:
        page = self._new_page("词汇", "accessories-dictionary-symbolic")
        hotword_group = Adw.PreferencesGroup(
            title="识别热词", description="帮助本地识别器优先识别名称和专有词语。"
        )
        self.hotwords_row = self._prefix_row(
            Adw.ActionRow(title="已启用热词", subtitle="尚未加载"),
            "starred-symbolic",
        )
        hotword_group.add(self.hotwords_row)
        snippet_group = Adw.PreferencesGroup(
            title="语音片段", description="将口述触发词替换为固定文本。"
        )
        self.snippets_row = self._prefix_row(
            Adw.ActionRow(title="已启用片段", subtitle="尚未加载"),
            "insert-text-symbolic",
        )
        snippet_group.add(self.snippets_row)
        refresh = Gtk.Button(label="重新加载", icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("重新加载词汇和语音片段")
        refresh.set_halign(Gtk.Align.START)
        refresh.connect("clicked", self._on_vocabulary_refresh)
        snippet_group.add(refresh)
        page.add(hotword_group)
        page.add(snippet_group)
        self._refresh_vocabulary_rows()
        return page

    def _build_models_page(self) -> Gtk.Widget:
        page = self._new_page("模型", "folder-download-symbolic")
        group = Adw.PreferencesGroup(
            title="本地识别模型", description="检查模型是否已安装且文件完整。"
        )
        self.models_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.models_box.add_css_class("boxed-list")
        group.add(self.models_box)
        refresh = Gtk.Button(label="检查模型", icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("刷新本地模型状态")
        refresh.set_halign(Gtk.Align.START)
        refresh.connect("clicked", lambda _button: self._controller.refresh_model_checks())
        group.add(refresh)
        page.add(group)
        return page

    def _build_history_page(self) -> Gtk.Widget:
        page = self._new_page("历史", "document-open-recent-symbolic")
        group = Adw.PreferencesGroup(
            title="最近转写", description="按时间查看保存在本机的识别结果。"
        )
        self.history_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.history_box.add_css_class("boxed-list")
        group.add(self.history_box)
        refresh = Gtk.Button(label="刷新历史", icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("刷新最近转写")
        refresh.set_halign(Gtk.Align.START)
        refresh.connect("clicked", lambda _button: self._controller.refresh_history())
        group.add(refresh)
        page.add(group)
        return page

    def _build_settings_page(self) -> Gtk.Widget:
        page = self._new_page("设置", "preferences-system-symbolic")
        recognition = Adw.PreferencesGroup(title="语音识别")
        recognition.add(
            self._prefix_row(
                Adw.ActionRow(title="流式后端", subtitle=self._config.asr.streaming_backend),
                "audio-input-microphone-symbolic",
            )
        )
        recognition.add(
            self._prefix_row(
                Adw.ActionRow(title="最终后端", subtitle=self._config.asr.final_backend),
                "emblem-ok-symbolic",
            )
        )
        processing = Adw.PreferencesGroup(title="文本处理")
        processing.add(
            self._prefix_row(
                Adw.ActionRow(title="处理提供方", subtitle=self._config.processing.provider),
                "document-edit-symbolic",
            )
        )
        shortcut = Adw.PreferencesGroup(
            title="全局快捷键",
            description="通过桌面门户绑定，Sway 用户可继续使用配置中的备用快捷键。",
        )
        self.shortcut_row = self._prefix_row(
            Adw.ActionRow(title="快捷键状态", subtitle="尚未绑定"),
            "preferences-desktop-keyboard-shortcuts-symbolic",
        )
        self.rebind_shortcuts_button = Gtk.Button(
            label="绑定快捷键", icon_name="preferences-desktop-keyboard-shortcuts-symbolic"
        )
        self.rebind_shortcuts_button.set_tooltip_text("绑定或重新绑定全局快捷键")
        self.rebind_shortcuts_button.set_valign(Gtk.Align.CENTER)
        self.rebind_shortcuts_button.set_action_name("app.rebind-shortcuts")
        self.shortcut_row.add_suffix(self.rebind_shortcuts_button)
        shortcut.add(self.shortcut_row)
        application_group = Adw.PreferencesGroup(title="应用")
        quit_row = self._prefix_row(
            Adw.ActionRow(title="退出 Type4Me", subtitle="停止常驻服务"),
            "application-exit-symbolic",
        )
        quit_button = Gtk.Button(icon_name="application-exit-symbolic")
        quit_button.set_tooltip_text("退出应用")
        quit_button.set_valign(Gtk.Align.CENTER)
        quit_button.add_css_class("flat")
        quit_button.set_action_name("app.quit")
        quit_row.add_suffix(quit_button)
        application_group.add(quit_row)
        page.add(recognition)
        page.add(processing)
        page.add(shortcut)
        page.add(application_group)
        return page

    def _on_navigation_selected(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self.view_stack.set_visible_child_name(row.get_name())

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.set_visible(False)
        return True

    def _on_mode_selected(self, row: Adw.ComboRow, _parameter: object) -> None:
        selected = row.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self._mode_ids):
            return
        mode_id = self._mode_ids[selected]
        state_mode = self._controller.state.selected_mode
        if state_mode is None or state_mode.id != mode_id:
            self._controller.select_mode(mode_id)

    def _on_vocabulary_refresh(self, _button: Gtk.Button) -> None:
        if self._vocabulary is None:
            return
        try:
            self._vocabulary.reload()
        except Exception as exc:
            self.message_row.set_title("词汇加载失败")
            self.message_row.set_subtitle(str(exc))
            self.message_row.set_visible(True)
            return
        self._refresh_vocabulary_rows()

    def _refresh_vocabulary_rows(self) -> None:
        if self._vocabulary is None or not hasattr(self, "hotwords_row"):
            return
        hotwords = self._vocabulary.list_hotwords()
        snippets = self._vocabulary.list_snippets()
        self.hotwords_row.set_subtitle("、".join(hotwords) if hotwords else "暂无热词")
        self.snippets_row.set_subtitle(
            "；".join(f"{key} → {value}" for key, value in snippets.items())
            if snippets
            else "暂无语音片段"
        )

    def _queue_state(self, state: AppState) -> object:
        if GLib.MainContext.default().is_owner():
            self._render_state(state)
            return False
        return GLib.idle_add(self._render_state, state)

    def _render_state(self, state: AppState) -> bool:
        transcript = state.transcript
        text = ""
        if transcript is not None:
            text = transcript.authoritative_text or "".join(transcript.confirmed_segments)
            if transcript.partial_text:
                text = f"{text}{transcript.partial_text}"
        self.transcript_label.set_text(text or "准备就绪")

        statuses = {
            "idle": ("准备就绪", "emblem-ok-symbolic"),
            "starting": ("正在启动录音", "content-loading-symbolic"),
            "recording": ("正在录音", "media-record-symbolic"),
            "stopping": ("正在完成识别", "content-loading-symbolic"),
            "completed": ("识别完成", "emblem-ok-symbolic"),
            "error": ("识别失败", "dialog-error-symbolic"),
            "cancelled": ("已取消", "process-stop-symbolic"),
        }
        status, icon = statuses[state.session_state]
        self.status_row.set_subtitle(status)
        self.status_icon.set_from_icon_name(icon)

        message = state.error or (state.warnings[-1] if state.warnings else None)
        self.message_row.set_title("错误" if state.error else "提醒")
        self.message_row.set_subtitle(message or "")
        self.message_row.set_visible(message is not None)

        busy = state.is_busy
        self.get_application().set_action_enabled("start", not busy)
        self.get_application().set_action_enabled("stop", busy)
        self.get_application().set_action_enabled("cancel", busy)

        names = [mode.name for mode in state.modes]
        self._mode_ids = [mode.id for mode in state.modes]
        for row in (self.mode_row, self.live_mode_row):
            row.set_model(Gtk.StringList.new(names))
        if state.selected_mode is not None and state.selected_mode.id in self._mode_ids:
            selected = self._mode_ids.index(state.selected_mode.id)
            self.mode_row.set_selected(selected)
            self.live_mode_row.set_selected(selected)
            detail = state.selected_mode.processing_label or "直接输入"
            self.mode_detail_row.set_subtitle(detail)
            self.live_mode_row.set_subtitle(detail)
        else:
            self.live_mode_row.set_subtitle("暂无可用模式")

        self._replace_model_rows(state)
        self._replace_history_rows(state)
        shortcuts = state.shortcuts
        shortcut_labels = {
            "unbound": "尚未绑定",
            "binding": "正在请求绑定",
            "bound": "已绑定",
            "unavailable": "当前桌面不可用",
        }
        detail = shortcut_labels[shortcuts.status]
        if shortcuts.status == "bound" and shortcuts.bound_ids:
            shortcut_names = {
                "toggle-recording": "切换录音",
                "cancel-recording": "取消录音",
                "show-window": "显示窗口",
            }
            bound = "、".join(
                shortcut_names.get(identifier, identifier)
                for identifier in sorted(shortcuts.bound_ids)
            )
            detail = f"{detail} · {bound}"
        if shortcuts.message:
            detail = f"{detail} · {shortcuts.message}"
        self.shortcut_row.set_subtitle(detail)
        self.rebind_shortcuts_button.set_label(
            "重新绑定快捷键" if shortcuts.status == "bound" else "绑定快捷键"
        )
        return False

    def _replace_model_rows(self, state: AppState) -> None:
        self._clear_list(self.models_box)
        if not state.model_checks:
            row = Adw.ActionRow(title="尚无检查结果", subtitle="点击“检查模型”刷新")
            row.add_css_class("type4me-empty")
            row.add_prefix(Gtk.Image.new_from_icon_name("system-search-symbolic"))
            self.models_box.append(row)
            return
        for check in state.model_checks:
            if check.ok:
                subtitle = check.version or "模型完整"
                icon = "emblem-ok-symbolic"
            elif check.installed:
                subtitle = "模型校验失败"
                icon = "dialog-warning-symbolic"
            else:
                subtitle = "尚未安装"
                icon = "folder-download-symbolic"
            row = Adw.ActionRow(title=check.id, subtitle=subtitle)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.models_box.append(row)

    def _replace_history_rows(self, state: AppState) -> None:
        self._clear_list(self.history_box)
        page = state.history_page
        if page is None or not page.records:
            row = Adw.ActionRow(title="暂无识别历史", subtitle="完成一次语音输入后会显示在这里")
            row.add_css_class("type4me-empty")
            row.add_prefix(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
            self.history_box.append(row)
            return
        for record in page.records:
            text = record.final_text or "识别失败"
            row = Adw.ActionRow(title=text, subtitle=record.created_at)
            row.set_title_lines(2)
            icon = "dialog-error-symbolic" if not record.final_text else "text-x-generic-symbolic"
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.history_box.append(row)

    @staticmethod
    def _clear_list(list_box: Gtk.ListBox) -> None:
        child = list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            list_box.remove(child)
            child = following

    def dispose_controller_subscription(self) -> None:
        unsubscribe = self._unsubscribe
        self._unsubscribe = lambda: None
        unsubscribe()


class Type4MeApplication(Adw.Application):
    """持有控制器和唯一窗口的常驻桌面应用。"""

    def __init__(
        self,
        config: Config,
        *,
        background: bool = False,
        service: bool = False,
        controller: Controller | None = None,
        controller_factory: ControllerFactory | None = None,
        paths: AppPaths | None = None,
        vocabulary: VocabularyService | None = None,
        notification_sender: NotificationSender | None = None,
        control_bus_factory: ControlBusFactory | None = None,
        shortcuts_factory: ShortcutsFactory | None = None,
    ) -> None:
        flags = Gio.ApplicationFlags.IS_SERVICE if service else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APPLICATION_ID, flags=flags)
        self.config = config
        self.background = background
        self.paths = paths or AppPaths.from_environment()
        self.vocabulary = vocabulary
        self._controller = controller
        self._controller_factory = controller_factory
        self._notification_sender = notification_sender
        self._control_bus_factory = control_bus_factory
        self._shortcuts_factory = shortcuts_factory
        self._control_bus: ResidentService | None = None
        self._shortcuts: ShortcutService | None = None
        self._integrations_closed = False
        self._controller_closed = False
        self.window: Type4MeWindow | None = None
        self._held = False
        self._quitting = False
        self.css_provider: Gtk.CssProvider | None = None
        self._shortcut_poll_source = 0

    @property
    def controller(self) -> Controller:
        if self._controller is None:
            self._controller = self._make_controller()
        return self._controller

    @property
    def is_held(self) -> bool:
        return self._held

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            self.css_provider = Gtk.CssProvider()
            self.css_provider.load_from_string(APPLICATION_CSS.decode())
            Gtk.StyleContext.add_provider_for_display(
                display,
                self.css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self.hold()
        self._held = True
        self._install_actions()
        self._start_resident_integrations()

    def do_activate(self) -> None:
        if self.window is None:
            vocabulary = self.vocabulary
            if vocabulary is None:
                try:
                    vocabulary = VocabularyService(self.paths)
                except Exception:
                    vocabulary = None
            self.window = Type4MeWindow(
                application=self,
                controller=self.controller,
                vocabulary=vocabulary,
                config=self.config,
            )
        if not self.background or self.window.get_visible():
            self.present_window()
        self.background = False

    def present_window(self) -> None:
        if self.window is None:
            self.activate()
            return
        self.window.present()

    def notify_when_unfocused(self, title: str, body: str) -> None:
        window = self.window
        if window is not None and window.is_active():
            return
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_default_action("app.show-window")
        if self._notification_sender is not None:
            self._notification_sender(notification)
        else:
            self.send_notification(None, notification)

    def do_shutdown(self) -> None:
        if self._shortcut_poll_source:
            GLib.source_remove(self._shortcut_poll_source)
            self._shortcut_poll_source = 0
        self._shutdown_resident_integrations()
        self._close_controller()
        Adw.Application.do_shutdown(self)

    def explicit_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self.window is not None:
            self.window.dispose_controller_subscription()
        self._shutdown_resident_integrations()
        self._close_controller()

        if self._held:
            self.release()
            self._held = False
        self.quit()

    def set_action_enabled(self, name: str, enabled: bool) -> None:
        action = self.lookup_action(name)
        if isinstance(action, Gio.SimpleAction):
            action.set_enabled(enabled)

    def _install_actions(self) -> None:
        actions = {
            "start": lambda: self.controller.toggle(),
            "stop": lambda: self.controller.toggle(),
            "cancel": lambda: self.controller.cancel(),
            "rebind-shortcuts": self._rebind_shortcuts,
            "show-window": self.present_window,
            "quit": self.explicit_quit,
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _value, cb=callback: cb())
            self.add_action(action)
        self.set_action_enabled("stop", False)
        self.set_action_enabled("cancel", False)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def _rebind_shortcuts(self) -> None:
        shortcuts = self._shortcuts
        if shortcuts is None or self._shortcut_poll_source:
            return
        setter = getattr(self.controller, "set_shortcut_state", None)
        if callable(setter):
            setter("binding")
        self.set_action_enabled("rebind-shortcuts", False)
        try:
            shortcuts.rebind()
        except Exception as exc:
            if callable(setter):
                setter("unavailable", message=self._shortcut_fallback(str(exc)))
            self.set_action_enabled("rebind-shortcuts", True)
            return
        self._shortcut_poll_source = GLib.timeout_add(100, self._poll_shortcut_rebind)

    def _poll_shortcut_rebind(self) -> bool:
        shortcuts = self._shortcuts
        if shortcuts is None:
            self._shortcut_poll_source = 0
            self.set_action_enabled("rebind-shortcuts", True)
            return False
        fallback = shortcuts.fallback_message
        bound = shortcuts.bound_shortcuts
        if not fallback and not bound:
            return True
        setter = getattr(self.controller, "set_shortcut_state", None)
        if callable(setter):
            if bound:
                setter("bound", bound)
            else:
                setter("unavailable", message=self._shortcut_fallback(fallback))
        self._shortcut_poll_source = 0
        self.set_action_enabled("rebind-shortcuts", True)
        return False

    @staticmethod
    def _shortcut_fallback(message: str | None) -> str:
        detail = message or "全局快捷键不可用。"
        if "Sway" not in detail:
            detail = f"{detail} 请在 Sway 配置中保留备用快捷键。"
        return detail

    def _start_resident_integrations(self) -> None:
        controller = self.controller
        if self._control_bus_factory is None:
            from .control_bus import ControlBusService

            self._control_bus = ControlBusService(controller)  # type: ignore[arg-type]
        else:
            self._control_bus = self._control_bus_factory(controller)  # type: ignore[assignment]
        if self._shortcuts_factory is None:
            from .shortcuts import PortalShortcuts

            self._shortcuts = PortalShortcuts(controller)  # type: ignore[arg-type]
        else:
            self._shortcuts = self._shortcuts_factory(controller)  # type: ignore[assignment]
        self._control_bus.start()
        self._shortcuts.start()
        setter = getattr(controller, "set_shortcut_state", None)
        if callable(setter):
            fallback = self._shortcuts.fallback_message
            if fallback:
                setter("unavailable", message=self._shortcut_fallback(fallback))
            elif self._shortcuts.bound_shortcuts:
                setter("bound", self._shortcuts.bound_shortcuts)
            else:
                setter("binding")
                self.set_action_enabled("rebind-shortcuts", False)
                self._shortcut_poll_source = GLib.timeout_add(100, self._poll_shortcut_rebind)

    def _shutdown_resident_integrations(self) -> None:
        if self._integrations_closed:
            return
        self._integrations_closed = True
        if self._shortcuts is not None:
            self._shortcuts.shutdown()
        if self._control_bus is not None:
            self._control_bus.stop()

    def _close_controller(self) -> None:
        if self._controller_closed:
            return
        self._controller_closed = True
        if self._controller is not None:
            self._controller.close()

    def _make_controller(self) -> Controller:
        if self._controller_factory is not None:
            return self._controller_factory(
                scheduler=lambda callback: GLib.idle_add(callback),
                notifier=self.notify_when_unfocused,
                show_window_callback=self.present_window,
            )
        modes = ModesRepository(self.paths)
        history = HistoryStore(self.paths) if self.config.history.enabled else None
        models = ModelManager(self.paths)

        def session_factory(request: object) -> object:
            pipeline = VoiceInputPipeline(
                self.config,
                paths=self.paths,
                modes=modes,
                history=history,
                model_manager=models,
            )
            return pipeline.create_session(request)  # type: ignore[arg-type]

        return ApplicationController(
            session_factory=session_factory,
            modes=modes,
            history=history,
            models=models,
            scheduler=lambda callback: GLib.idle_add(callback),
            notifier=self.notify_when_unfocused,
            show_window_callback=self.present_window,
        )


def run(config: Config, background: bool = False, service: bool = False) -> int:
    """运行常驻 Adwaita 应用，并返回 Gio.Application 退出码。"""

    application = Type4MeApplication(config, background=background, service=service)
    return int(application.run(None))
