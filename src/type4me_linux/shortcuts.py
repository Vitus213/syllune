from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .control_bus import ControllerProtocol

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
GLOBAL_SHORTCUTS_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

HOLD_SHORTCUT_ID = "hold-to-talk"
TOGGLE_SHORTCUT_ID = "toggle-recording"
REQUEST_PATH_PREFIX = "/org/freedesktop/portal/desktop/request"
SESSION_PATH_PREFIX = "/org/freedesktop/portal/desktop/session"

_REQUESTED_SHORTCUTS: tuple[tuple[str, Mapping[str, str]], ...] = (
    (HOLD_SHORTCUT_ID, {"description": "按住说话"}),
    (TOGGLE_SHORTCUT_ID, {"description": "切换录音"}),
)
_VALID_PATH = re.compile(r"^/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+$")
_VALID_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SignalCallback = Callable[..., None]


class PortalTransport(Protocol):
    @property
    def unique_name(self) -> str: ...

    def get_global_shortcuts_version(self) -> int: ...

    def subscribe(
        self,
        interface_name: str,
        signal_name: str,
        object_path: str,
        callback: SignalCallback,
    ) -> object: ...

    def watch_name_loss(self, callback: Callable[[], None]) -> object: ...

    def unsubscribe(self, subscription: object) -> None: ...

    def create_session(self, options: Mapping[str, str]) -> str: ...

    def bind_shortcuts(
        self,
        session_handle: str,
        shortcuts: Sequence[tuple[str, Mapping[str, str]]],
        parent_window: str,
        options: Mapping[str, str],
    ) -> str: ...

    def close_request(self, request_handle: str) -> None: ...

    def close_session(self, session_handle: str) -> None: ...


@dataclass
class _PendingRequest:
    generation: int
    stage: str
    path: str
    subscription: object


class PortalShortcuts:
    """具有可注入无 GI 传输边界的 GlobalShortcuts 状态机。"""

    def __init__(
        self,
        controller: ControllerProtocol,
        *,
        transport: PortalTransport | None = None,
        token_factory: Callable[[str], str] | None = None,
        parent_window: str = "",
    ) -> None:
        self._controller = controller
        self._transport = transport or GioPortalTransport()
        self._token_factory = token_factory or self._make_token
        self._parent_window = parent_window
        self._lock = threading.RLock()
        self._generation = 0
        self._running = False
        self._forced_stop_done = False
        self._version: int | None = None
        self._session_handle: str | None = None
        self._predicted_session_handle: str | None = None
        self._bound_shortcuts: frozenset[str] = frozenset()
        self._pressed: set[str] = set()
        self._subscriptions: list[object] = []
        self._name_watch: object | None = None
        self._pending: _PendingRequest | None = None
        self._bind_attempted = False
        self._fallback_message: str | None = None
        self._last_response_code: int | None = None

    @property
    def version(self) -> int | None:
        with self._lock:
            return self._version

    @property
    def session_handle(self) -> str | None:
        with self._lock:
            return self._session_handle

    @property
    def bound_shortcuts(self) -> frozenset[str]:
        with self._lock:
            return self._bound_shortcuts

    @property
    def fallback_message(self) -> str | None:
        with self._lock:
            return self._fallback_message

    @property
    def last_response_code(self) -> int | None:
        with self._lock:
            return self._last_response_code

    @property
    def is_bound(self) -> bool:
        with self._lock:
            return bool(self._bound_shortcuts)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._generation += 1
            generation = self._generation
            self._running = True
            self._forced_stop_done = False
            self._fallback_message = None
            self._last_response_code = None
            self._bind_attempted = False
            self._bound_shortcuts = frozenset()
            self._pressed.clear()

        try:
            version = self._transport.get_global_shortcuts_version()
        except Exception as exc:
            self._initial_failure(f"无法使用全局快捷键门户：{exc}")
            return
        if version < 1:
            self._initial_failure("全局快捷键门户版本不受支持，请配置 Sway 备用快捷键。")
            return

        try:
            sender = _normalize_sender(self._transport.unique_name)
            request_token = self._new_token("create")
            session_token = self._new_token("session")
        except (TypeError, ValueError) as exc:
            self._initial_failure(f"无法创建全局快捷键会话：{exc}")
            return

        request_path = f"{REQUEST_PATH_PREFIX}/{sender}/{request_token}"
        session_path = f"{SESSION_PATH_PREFIX}/{sender}/{session_token}"
        with self._lock:
            if not self._is_generation_active(generation):
                return
            self._version = version
            self._predicted_session_handle = session_path

        try:
            self._subscribe_lifecycle(generation, session_path)
            pending = self._subscribe_request(generation, "create", request_path)
            returned_path = self._transport.create_session(
                {
                    "handle_token": request_token,
                    "session_handle_token": session_token,
                }
            )
            self._reconcile_request_path(pending, returned_path)
        except Exception as exc:
            self._initial_failure(f"无法创建全局快捷键会话：{exc}")

    def rebind(self) -> None:
        self._teardown(force_stop=True, close_remote=True)
        self.start()

    def shutdown(self) -> None:
        self._teardown(force_stop=True, close_remote=True)

    def close_pending_request(self) -> None:
        with self._lock:
            pending = self._pending
            has_session = self._session_handle is not None
            if pending is None:
                return
            self._fallback_message = "全局快捷键请求已关闭，请使用备用快捷键。"
        self._teardown(force_stop=has_session, close_remote=True)

    def _subscribe_lifecycle(self, generation: int, session_path: str) -> None:
        subscriptions = [
            self._transport.subscribe(
                GLOBAL_SHORTCUTS_INTERFACE,
                "Activated",
                PORTAL_OBJECT_PATH,
                lambda *args: self._on_activated(generation, *args),
            ),
            self._transport.subscribe(
                GLOBAL_SHORTCUTS_INTERFACE,
                "Deactivated",
                PORTAL_OBJECT_PATH,
                lambda *args: self._on_deactivated(generation, *args),
            ),
            self._transport.subscribe(
                GLOBAL_SHORTCUTS_INTERFACE,
                "ShortcutsChanged",
                PORTAL_OBJECT_PATH,
                lambda *args: self._on_shortcuts_changed(generation, *args),
            ),
            self._transport.subscribe(
                SESSION_INTERFACE,
                "Closed",
                session_path,
                lambda *args: self._on_session_closed(generation),
            ),
        ]
        watch = self._transport.watch_name_loss(lambda: self._on_bus_loss(generation))
        with self._lock:
            if self._is_generation_active(generation):
                self._subscriptions.extend(subscriptions)
                self._name_watch = watch
                return
        for subscription in subscriptions:
            self._transport.unsubscribe(subscription)
        self._transport.unsubscribe(watch)

    def _subscribe_request(
        self,
        generation: int,
        stage: str,
        path: str,
    ) -> _PendingRequest:
        pending_ref: list[_PendingRequest] = []

        def response_callback(response: int, results: Mapping[str, Any]) -> None:
            if pending_ref:
                self._on_request_response(pending_ref[0], response, results)

        subscription = self._transport.subscribe(
            REQUEST_INTERFACE,
            "Response",
            path,
            response_callback,
        )
        pending = _PendingRequest(generation, stage, path, subscription)
        pending_ref.append(pending)
        with self._lock:
            if not self._is_generation_active(generation):
                self._transport.unsubscribe(subscription)
                raise RuntimeError("全局快捷键会话已结束。")
            self._pending = pending
            self._subscriptions.append(subscription)
        return pending

    def _reconcile_request_path(self, pending: _PendingRequest, returned_path: str) -> None:
        _require_object_path(returned_path, "请求路径")
        with self._lock:
            if self._pending is not pending or returned_path == pending.path:
                return
        replacement = self._transport.subscribe(
            REQUEST_INTERFACE,
            "Response",
            returned_path,
            lambda response, results: self._on_request_response(pending, response, results),
        )
        with self._lock:
            if self._pending is not pending:
                self._transport.unsubscribe(replacement)
                return
            old = pending.subscription
            pending.subscription = replacement
            pending.path = returned_path
            self._subscriptions.remove(old)
            self._subscriptions.append(replacement)
        self._transport.unsubscribe(old)

    def _on_request_response(
        self,
        pending: _PendingRequest,
        response: int,
        results: Mapping[str, Any],
    ) -> None:
        with self._lock:
            if self._pending is not pending or not self._is_generation_active(pending.generation):
                return
            self._pending = None
            self._last_response_code = int(response)
            if pending.subscription in self._subscriptions:
                self._subscriptions.remove(pending.subscription)
        self._transport.unsubscribe(pending.subscription)

        if response != 0:
            with self._lock:
                if response == 1:
                    self._fallback_message = "用户取消了全局快捷键授权，请使用备用快捷键。"
                else:
                    self._fallback_message = "全局快捷键请求未完成，请使用备用快捷键。"
                self._bound_shortcuts = frozenset()
            return

        normalized = _unpack(results)
        if pending.stage == "create":
            self._complete_create(pending.generation, normalized)
        else:
            self._complete_bind(normalized)

    def _complete_create(self, generation: int, results: Any) -> None:
        session_handle = results.get("session_handle") if isinstance(results, Mapping) else None
        if not isinstance(session_handle, str):
            self._initial_failure("门户未返回有效的全局快捷键会话路径。")
            return
        try:
            _require_object_path(session_handle, "会话路径")
        except ValueError as exc:
            self._initial_failure(str(exc))
            return
        with self._lock:
            if not self._is_generation_active(generation):
                return
            if session_handle != self._predicted_session_handle:
                self._fallback_message = "门户返回的会话路径与请求令牌不匹配。"
                mismatched = True
            else:
                self._session_handle = session_handle
                mismatched = False
        if mismatched:
            try:
                self._transport.close_session(session_handle)
            finally:
                self._teardown(force_stop=False, close_remote=False)
            return
        self._begin_bind(generation, session_handle)

    def _begin_bind(self, generation: int, session_handle: str) -> None:
        with self._lock:
            if not self._is_generation_active(generation) or self._bind_attempted:
                return
            self._bind_attempted = True
        try:
            token = self._new_token("bind")
            sender = _normalize_sender(self._transport.unique_name)
            request_path = f"{REQUEST_PATH_PREFIX}/{sender}/{token}"
            pending = self._subscribe_request(generation, "bind", request_path)
            returned_path = self._transport.bind_shortcuts(
                session_handle,
                _REQUESTED_SHORTCUTS,
                self._parent_window,
                {"handle_token": token},
            )
            self._reconcile_request_path(pending, returned_path)
        except Exception as exc:
            with self._lock:
                self._fallback_message = f"无法绑定全局快捷键：{exc}"
                self._bound_shortcuts = frozenset()
            self._teardown(force_stop=True, close_remote=True)

    def _complete_bind(self, results: Any) -> None:
        shortcuts = results.get("shortcuts", ()) if isinstance(results, Mapping) else ()
        bound = _extract_shortcut_ids(shortcuts)
        with self._lock:
            self._bound_shortcuts = bound
            self._pressed.intersection_update(bound)
            self._fallback_message = (
                None if bound else "门户未绑定任何全局快捷键，请使用备用快捷键。"
            )

    def _on_activated(
        self,
        generation: int,
        session_handle: str,
        shortcut_id: str,
        _timestamp: int,
        _options: Mapping[str, Any],
    ) -> None:
        action: Callable[[], None] | None = None
        with self._lock:
            if (
                not self._is_generation_active(generation)
                or session_handle != self._session_handle
                or shortcut_id not in self._bound_shortcuts
                or shortcut_id in self._pressed
            ):
                return
            self._pressed.add(shortcut_id)
            if shortcut_id == HOLD_SHORTCUT_ID:
                action = self._controller.hold_start
            elif shortcut_id == TOGGLE_SHORTCUT_ID:
                action = self._controller.toggle
        if action is not None:
            action()

    def _on_deactivated(
        self,
        generation: int,
        session_handle: str,
        shortcut_id: str,
        _timestamp: int,
        _options: Mapping[str, Any],
    ) -> None:
        action: Callable[[], None] | None = None
        with self._lock:
            if (
                not self._is_generation_active(generation)
                or session_handle != self._session_handle
                or shortcut_id not in self._bound_shortcuts
                or shortcut_id not in self._pressed
            ):
                return
            self._pressed.remove(shortcut_id)
            if shortcut_id == HOLD_SHORTCUT_ID:
                action = self._controller.hold_stop
        if action is not None:
            action()

    def _on_shortcuts_changed(
        self,
        generation: int,
        session_handle: str,
        shortcuts: Sequence[Any],
    ) -> None:
        with self._lock:
            if not self._is_generation_active(generation) or session_handle != self._session_handle:
                return
            self._bound_shortcuts = _extract_shortcut_ids(_unpack(shortcuts))
            self._pressed.intersection_update(self._bound_shortcuts)
            self._fallback_message = (
                None if self._bound_shortcuts else "门户已移除全局快捷键，请使用备用快捷键。"
            )

    def _on_session_closed(self, generation: int) -> None:
        with self._lock:
            if not self._is_generation_active(generation):
                return
        self._teardown(force_stop=True, close_remote=False)

    def _on_bus_loss(self, generation: int) -> None:
        with self._lock:
            if not self._is_generation_active(generation):
                return
            self._fallback_message = "全局快捷键门户连接已断开，请使用备用快捷键。"
        self._teardown(force_stop=True, close_remote=False)

    def _initial_failure(self, message: str) -> None:
        with self._lock:
            self._fallback_message = message
        self._teardown(force_stop=False, close_remote=True)

    def _teardown(self, *, force_stop: bool, close_remote: bool) -> None:
        with self._lock:
            if not self._running and not self._subscriptions and self._name_watch is None:
                return
            self._running = False
            pending = self._pending
            session_handle = self._session_handle
            subscriptions = tuple(self._subscriptions)
            name_watch = self._name_watch
            should_stop = force_stop and not self._forced_stop_done
            if should_stop:
                self._forced_stop_done = True
            self._pending = None
            self._session_handle = None
            self._predicted_session_handle = None
            self._bound_shortcuts = frozenset()
            self._pressed.clear()
            self._subscriptions.clear()
            self._name_watch = None

        for subscription in subscriptions:
            self._transport.unsubscribe(subscription)
        if name_watch is not None:
            self._transport.unsubscribe(name_watch)
        if close_remote and pending is not None:
            try:
                self._transport.close_request(pending.path)
            except Exception:
                pass
        if close_remote and session_handle is not None:
            try:
                self._transport.close_session(session_handle)
            except Exception:
                pass
        if should_stop:
            self._controller.hold_stop()

    def _is_generation_active(self, generation: int) -> bool:
        return self._running and generation == self._generation

    def _new_token(self, kind: str) -> str:
        token = self._token_factory(kind)
        if not isinstance(token, str) or not _VALID_TOKEN.fullmatch(token):
            raise ValueError(f"无效的 D-Bus 请求令牌：{token!r}")
        return token

    @staticmethod
    def _make_token(kind: str) -> str:
        return f"type4me_{kind}_{secrets.token_hex(16)}"


class GioPortalTransport:
    """org.freedesktop.portal.GlobalShortcuts 的延迟加载 PyGObject 适配器。"""

    def __init__(self) -> None:
        self._connection: Any = None
        self._gio: Any = None
        self._glib: Any = None
        self._closed_handlers: dict[int, int] = {}

    @property
    def unique_name(self) -> str:
        _, _, connection = self._ensure_connection()
        name = connection.get_unique_name()
        if not name:
            raise RuntimeError("会话总线连接没有唯一名称。")
        return name

    def get_global_shortcuts_version(self) -> int:
        Gio, GLib, connection = self._ensure_connection()
        reply = connection.call_sync(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            PROPERTIES_INTERFACE,
            "Get",
            GLib.Variant("(ss)", (GLOBAL_SHORTCUTS_INTERFACE, "version")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        unpacked = _unpack(reply)
        return int(unpacked[0])

    def subscribe(
        self,
        interface_name: str,
        signal_name: str,
        object_path: str,
        callback: SignalCallback,
    ) -> object:
        Gio, _, connection = self._ensure_connection()

        def handler(
            _connection,
            _sender_name,
            _object_path,
            _interface_name,
            _signal_name,
            parameters,
            _user_data,
        ) -> None:  # type: ignore[no-untyped-def]
            values = _unpack(parameters)
            callback(*values)

        return connection.signal_subscribe(
            PORTAL_BUS_NAME,
            interface_name,
            signal_name,
            object_path,
            None,
            Gio.DBusSignalFlags.NONE,
            handler,
            None,
        )

    def watch_name_loss(self, callback: Callable[[], None]) -> object:
        Gio, _, connection = self._ensure_connection()

        def owner_changed(
            _connection,
            _sender_name,
            _object_path,
            _interface_name,
            _signal_name,
            parameters,
            _user_data,
        ) -> None:  # type: ignore[no-untyped-def]
            name, _old_owner, new_owner = _unpack(parameters)
            if name == PORTAL_BUS_NAME and not new_owner:
                callback()

        signal_id = connection.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            PORTAL_BUS_NAME,
            Gio.DBusSignalFlags.NONE,
            owner_changed,
            None,
        )
        closed_id = connection.connect("closed", lambda *_args: callback())
        token = ("name-watch", signal_id, closed_id)
        self._closed_handlers[id(token)] = closed_id
        return token

    def unsubscribe(self, subscription: object) -> None:
        _, _, connection = self._ensure_connection()
        if isinstance(subscription, tuple) and subscription[:1] == ("name-watch",):
            _, signal_id, closed_id = subscription
            connection.signal_unsubscribe(signal_id)
            if connection.handler_is_connected(closed_id):
                connection.disconnect(closed_id)
            self._closed_handlers.pop(id(subscription), None)
            return
        connection.signal_unsubscribe(subscription)

    def create_session(self, options: Mapping[str, str]) -> str:
        Gio, GLib, connection = self._ensure_connection()
        reply = connection.call_sync(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            GLOBAL_SHORTCUTS_INTERFACE,
            "CreateSession",
            GLib.Variant("(a{sv})", (_variant_dict(GLib, options),)),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return str(_unpack(reply)[0])

    def bind_shortcuts(
        self,
        session_handle: str,
        shortcuts: Sequence[tuple[str, Mapping[str, str]]],
        parent_window: str,
        options: Mapping[str, str],
    ) -> str:
        Gio, GLib, connection = self._ensure_connection()
        typed_shortcuts = [
            (shortcut_id, _variant_dict(GLib, details)) for shortcut_id, details in shortcuts
        ]
        reply = connection.call_sync(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            GLOBAL_SHORTCUTS_INTERFACE,
            "BindShortcuts",
            GLib.Variant(
                "(oa(sa{sv})sa{sv})",
                (
                    session_handle,
                    typed_shortcuts,
                    parent_window,
                    _variant_dict(GLib, options),
                ),
            ),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return str(_unpack(reply)[0])

    def close_request(self, request_handle: str) -> None:
        self._close(request_handle, REQUEST_INTERFACE)

    def close_session(self, session_handle: str) -> None:
        self._close(session_handle, SESSION_INTERFACE)

    def _close(self, object_path: str, interface_name: str) -> None:
        Gio, GLib, connection = self._ensure_connection()
        connection.call_sync(
            PORTAL_BUS_NAME,
            object_path,
            interface_name,
            "Close",
            None,
            GLib.VariantType.new("()"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

    def _ensure_connection(self):  # type: ignore[no-untyped-def]
        if self._connection is None:
            try:
                import gi

                gi.require_version("Gio", "2.0")
                from gi.repository import Gio, GLib
            except (ImportError, ValueError) as exc:
                raise RuntimeError("无法加载 Gio，不能使用全局快捷键门户。") from exc
            self._gio = Gio
            self._glib = GLib
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._gio, self._glib, self._connection


def _variant_dict(GLib: Any, values: Mapping[str, str]) -> dict[str, Any]:
    return {key: GLib.Variant("s", value) for key, value in values.items()}


def _unpack(value: Any) -> Any:
    if hasattr(value, "unpack"):
        return _unpack(value.unpack())
    if isinstance(value, Mapping):
        return {key: _unpack(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_unpack(item) for item in value)
    if isinstance(value, list):
        return [_unpack(item) for item in value]
    return value


def _extract_shortcut_ids(shortcuts: Any) -> frozenset[str]:
    allowed = {HOLD_SHORTCUT_ID, TOGGLE_SHORTCUT_ID}
    if not isinstance(shortcuts, (list, tuple)):
        return frozenset()
    return frozenset(
        item[0]
        for item in shortcuts
        if isinstance(item, (list, tuple))
        and len(item) == 2
        and isinstance(item[0], str)
        and item[0] in allowed
    )


def _normalize_sender(unique_name: str) -> str:
    if not isinstance(unique_name, str) or not unique_name.startswith(":"):
        raise ValueError(f"无效的会话总线唯一名称：{unique_name!r}")
    sender = unique_name[1:].replace(".", "_")
    if not sender or not re.fullmatch(r"[A-Za-z0-9_]+", sender):
        raise ValueError(f"无效的会话总线唯一名称：{unique_name!r}")
    return sender


def _require_object_path(path: str, label: str) -> None:
    if not isinstance(path, str) or not _VALID_PATH.fullmatch(path):
        raise ValueError(f"门户返回了无效的{label}：{path!r}")
