from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from type4me_linux.shortcuts import (
    GLOBAL_SHORTCUTS_INTERFACE,
    HOLD_SHORTCUT_ID,
    PORTAL_BUS_NAME,
    PORTAL_OBJECT_PATH,
    REQUEST_INTERFACE,
    REQUEST_PATH_PREFIX,
    SESSION_INTERFACE,
    SESSION_PATH_PREFIX,
    TOGGLE_SHORTCUT_ID,
    GioPortalTransport,
    PortalShortcuts,
)


class _Controller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def toggle(self) -> None:
        self.calls.append("toggle")

    def hold_start(self) -> None:
        self.calls.append("hold_start")

    def hold_stop(self) -> None:
        self.calls.append("hold_stop")

    def cancel(self) -> None:
        self.calls.append("cancel")

    def show_window(self) -> None:
        self.calls.append("show_window")


@dataclass
class _Subscription:
    interface: str
    signal: str
    path: str
    callback: Callable[..., None]
    active: bool = True


class _PortalTransport:
    unique_name = ":1.234"

    def __init__(self, *, version: int = 2) -> None:
        self.version = version
        self.log: list[tuple[Any, ...]] = []
        self.subscriptions: list[_Subscription] = []
        self.name_watches: list[_Subscription] = []
        self.create_calls: list[dict[str, str]] = []
        self.bind_calls: list[
            tuple[str, Sequence[tuple[str, Mapping[str, str]]], str, dict[str, str]]
        ] = []
        self.closed_requests: list[str] = []
        self.closed_sessions: list[str] = []
        self.returned_create_path: str | None = None
        self.returned_bind_path: str | None = None

    def get_global_shortcuts_version(self) -> int:
        self.log.append(("version",))
        return self.version

    def subscribe(
        self,
        interface_name: str,
        signal_name: str,
        object_path: str,
        callback: Callable[..., None],
    ) -> object:
        subscription = _Subscription(interface_name, signal_name, object_path, callback)
        self.subscriptions.append(subscription)
        self.log.append(("subscribe", interface_name, signal_name, object_path))
        return subscription

    def watch_name_loss(self, callback: Callable[[], None]) -> object:
        watch = _Subscription("bus", "NameLost", PORTAL_BUS_NAME, callback)
        self.name_watches.append(watch)
        self.log.append(("watch-name", PORTAL_BUS_NAME))
        return watch

    def unsubscribe(self, subscription: object) -> None:
        assert isinstance(subscription, _Subscription)
        subscription.active = False
        self.log.append(
            ("unsubscribe", subscription.interface, subscription.signal, subscription.path)
        )

    def create_session(self, options: Mapping[str, str]) -> str:
        copied = dict(options)
        self.create_calls.append(copied)
        self.log.append(("call", "CreateSession", copied))
        expected = request_path(copied["handle_token"])
        return self.returned_create_path or expected

    def bind_shortcuts(
        self,
        session_handle: str,
        shortcuts: Sequence[tuple[str, Mapping[str, str]]],
        parent_window: str,
        options: Mapping[str, str],
    ) -> str:
        copied = dict(options)
        self.bind_calls.append((session_handle, shortcuts, parent_window, copied))
        self.log.append(("call", "BindShortcuts", session_handle, copied))
        expected = request_path(copied["handle_token"])
        return self.returned_bind_path or expected

    def close_request(self, request_handle: str) -> None:
        self.closed_requests.append(request_handle)
        self.log.append(("close-request", request_handle))

    def close_session(self, session_handle: str) -> None:
        self.closed_sessions.append(session_handle)
        self.log.append(("close-session", session_handle))

    def emit(self, interface: str, signal: str, path: str, *args: Any) -> None:
        for subscription in tuple(self.subscriptions):
            if (
                subscription.active
                and subscription.interface == interface
                and subscription.signal == signal
                and subscription.path == path
            ):
                subscription.callback(*args)

    def emit_response(self, path: str, code: int, results: Mapping[str, Any]) -> None:
        self.emit(REQUEST_INTERFACE, "Response", path, code, results)

    def lose_name(self) -> None:
        for watch in tuple(self.name_watches):
            if watch.active:
                watch.callback()


def request_path(token: str) -> str:
    return f"{REQUEST_PATH_PREFIX}/1_234/{token}"


def session_path(token: str) -> str:
    return f"{SESSION_PATH_PREFIX}/1_234/{token}"


def token_factory(*tokens: str) -> Callable[[str], str]:
    remaining = iter(tokens)
    return lambda _kind: next(remaining)


def begin_binding(
    *,
    tokens: tuple[str, str, str] = ("create_a", "session_a", "bind_a"),
) -> tuple[PortalShortcuts, _PortalTransport, _Controller, str, str]:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory(*tokens),
    )
    shortcuts.start()
    create_request = request_path(tokens[0])
    session = session_path(tokens[1])
    transport.emit_response(create_request, 0, {"session_handle": session})
    return shortcuts, transport, controller, session, request_path(tokens[2])


def complete_binding(
    transport: _PortalTransport,
    bind_request: str,
    ids: Sequence[str],
) -> None:
    transport.emit_response(
        bind_request,
        0,
        {
            "shortcuts": [
                (shortcut_id, {"description": "说明", "trigger_description": "快捷键"})
                for shortcut_id in ids
            ]
        },
    )


def test_exact_portal_identifiers_and_subscribe_before_calls() -> None:
    shortcuts, transport, _controller, session, bind_request = begin_binding()

    assert PORTAL_BUS_NAME == "org.freedesktop.portal.Desktop"
    assert PORTAL_OBJECT_PATH == "/org/freedesktop/portal/desktop"
    assert GLOBAL_SHORTCUTS_INTERFACE == "org.freedesktop.portal.GlobalShortcuts"
    assert HOLD_SHORTCUT_ID == "hold-to-talk"
    assert TOGGLE_SHORTCUT_ID == "toggle-recording"
    assert transport.create_calls == [
        {"handle_token": "create_a", "session_handle_token": "session_a"}
    ]
    assert transport.bind_calls[0][0] == session
    assert transport.bind_calls[0][2] == ""
    assert [item[0] for item in transport.bind_calls[0][1]] == [
        HOLD_SHORTCUT_ID,
        TOGGLE_SHORTCUT_ID,
    ]

    create_subscribe = transport.log.index(
        ("subscribe", REQUEST_INTERFACE, "Response", request_path("create_a"))
    )
    create_call = next(
        i for i, item in enumerate(transport.log) if item[:2] == ("call", "CreateSession")
    )
    bind_subscribe = transport.log.index(("subscribe", REQUEST_INTERFACE, "Response", bind_request))
    bind_call = next(
        i for i, item in enumerate(transport.log) if item[:2] == ("call", "BindShortcuts")
    )
    assert create_subscribe < create_call
    assert bind_subscribe < bind_call
    assert (
        "subscribe",
        SESSION_INTERFACE,
        "Closed",
        session_path("session_a"),
    ) in transport.log
    assert shortcuts.version == 2


def test_successful_binding_routes_hold_and_suppresses_duplicate_toggle() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    complete_binding(transport, bind_request, [HOLD_SHORTCUT_ID, TOGGLE_SHORTCUT_ID])

    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        10,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        11,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Deactivated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        12,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        TOGGLE_SHORTCUT_ID,
        13,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        TOGGLE_SHORTCUT_ID,
        14,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Deactivated",
        PORTAL_OBJECT_PATH,
        session,
        TOGGLE_SHORTCUT_ID,
        15,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        TOGGLE_SHORTCUT_ID,
        16,
        {},
    )

    assert shortcuts.bound_shortcuts == frozenset({HOLD_SHORTCUT_ID, TOGGLE_SHORTCUT_ID})
    assert shortcuts.fallback_message is None
    assert controller.calls == ["hold_start", "hold_stop", "toggle", "toggle"]


def test_returned_subset_is_authoritative() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    complete_binding(transport, bind_request, [HOLD_SHORTCUT_ID, "portal-extra"])

    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        TOGGLE_SHORTCUT_ID,
        1,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        2,
        {},
    )

    assert shortcuts.bound_shortcuts == frozenset({HOLD_SHORTCUT_ID})
    assert controller.calls == ["hold_start"]


def test_empty_success_exposes_fallback_without_dispatch() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    complete_binding(transport, bind_request, [])
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        1,
        {},
    )

    assert shortcuts.bound_shortcuts == frozenset()
    assert shortcuts.is_bound is False
    assert shortcuts.fallback_message is not None
    assert "备用快捷键" in shortcuts.fallback_message
    assert controller.calls == []


@pytest.mark.parametrize(
    ("response_code", "expected"),
    [(1, "用户取消"), (2, "请求未完成")],
)
def test_bind_response_codes_leave_shortcuts_unbound(
    response_code: int,
    expected: str,
) -> None:
    shortcuts, transport, _controller, _session, bind_request = begin_binding()
    transport.emit_response(bind_request, response_code, {})

    assert shortcuts.last_response_code == response_code
    assert shortcuts.bound_shortcuts == frozenset()
    assert expected in (shortcuts.fallback_message or "")


def test_start_does_not_bind_twice_and_rebind_uses_new_session() -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory(
            "create_a",
            "session_a",
            "bind_a",
            "create_b",
            "session_b",
            "bind_b",
        ),
    )
    shortcuts.start()
    transport.emit_response(
        request_path("create_a"),
        0,
        {"session_handle": session_path("session_a")},
    )
    complete_binding(transport, request_path("bind_a"), [TOGGLE_SHORTCUT_ID])

    shortcuts.start()
    assert len(transport.bind_calls) == 1

    shortcuts.rebind()
    transport.emit_response(
        request_path("create_b"),
        0,
        {"session_handle": session_path("session_b")},
    )
    complete_binding(transport, request_path("bind_b"), [HOLD_SHORTCUT_ID])

    assert len(transport.bind_calls) == 2
    assert [call[0] for call in transport.bind_calls] == [
        session_path("session_a"),
        session_path("session_b"),
    ]
    assert transport.closed_sessions == [session_path("session_a")]
    assert controller.calls == ["hold_stop"]
    assert shortcuts.bound_shortcuts == frozenset({HOLD_SHORTCUT_ID})


def test_session_closed_forces_exactly_one_stop() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    complete_binding(transport, bind_request, [HOLD_SHORTCUT_ID])

    closed_callbacks = [
        subscription.callback
        for subscription in transport.subscriptions
        if subscription.interface == SESSION_INTERFACE and subscription.signal == "Closed"
    ]
    assert len(closed_callbacks) == 1
    closed_callbacks[0]({})
    closed_callbacks[0]({})
    shortcuts.shutdown()

    assert controller.calls == ["hold_stop"]
    assert shortcuts.bound_shortcuts == frozenset()
    assert transport.closed_sessions == []
    assert shortcuts.session_handle is None


def test_request_close_does_not_wait_for_response() -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory("create_a", "session_a"),
    )
    shortcuts.start()
    shortcuts.close_pending_request()

    assert transport.closed_requests == [request_path("create_a")]
    assert shortcuts.fallback_message is not None
    transport.emit_response(
        request_path("create_a"),
        0,
        {"session_handle": session_path("session_a")},
    )
    assert transport.bind_calls == []
    assert controller.calls == []


def test_closing_pending_bind_closes_request_and_session_once() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    shortcuts.close_pending_request()
    shortcuts.shutdown()

    assert transport.closed_requests == [bind_request]
    assert transport.closed_sessions == [session]
    assert controller.calls == ["hold_stop"]


def test_bus_loss_forces_exactly_one_stop_and_ignores_late_signals() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    complete_binding(transport, bind_request, [TOGGLE_SHORTCUT_ID])
    signal_callbacks = [
        subscription.callback
        for subscription in transport.subscriptions
        if subscription.interface == GLOBAL_SHORTCUTS_INTERFACE
        and subscription.signal == "Activated"
    ]

    transport.lose_name()
    transport.lose_name()
    signal_callbacks[0](session, TOGGLE_SHORTCUT_ID, 99, {})
    shortcuts.shutdown()

    assert controller.calls == ["hold_stop"]
    assert shortcuts.bound_shortcuts == frozenset()


def test_unsupported_portal_is_nonfatal_and_exposes_fallback() -> None:
    transport = _PortalTransport(version=0)
    controller = _Controller()
    shortcuts = PortalShortcuts(controller, transport=transport)
    shortcuts.start()

    assert shortcuts.is_bound is False
    assert "不受支持" in (shortcuts.fallback_message or "")
    assert transport.create_calls == []
    assert controller.calls == []


class _PackedVariant:
    def __init__(self, signature: str, value: Any) -> None:
        self.signature = signature
        self.value = value

    def unpack(self) -> Any:
        return self.value


class _VariantType:
    @staticmethod
    def new(signature: str) -> str:
        return signature


class _GioConnection:
    def __init__(self, *, unique_name: str | None = ":1.77") -> None:
        self.unique_name = unique_name
        self.calls: list[tuple[Any, ...]] = []
        self.signal_subscriptions: dict[int, tuple[Any, ...]] = {}
        self.signal_unsubscriptions: list[int] = []
        self.closed_handlers: dict[int, Callable[..., None]] = {}
        self.disconnected: list[int] = []
        self._next_id = 1

    def get_unique_name(self) -> str | None:
        return self.unique_name

    def call_sync(self, *args: Any) -> _PackedVariant:
        self.calls.append(args)
        method = args[3]
        if method == "Get":
            return _PackedVariant("(v)", (_PackedVariant("u", 2),))
        if method == "CreateSession":
            return _PackedVariant("(o)", ("/request/create",))
        if method == "BindShortcuts":
            return _PackedVariant("(o)", ("/request/bind",))
        return _PackedVariant("()", ())

    def signal_subscribe(self, *args: Any) -> int:
        signal_id = self._next_id
        self._next_id += 1
        self.signal_subscriptions[signal_id] = args
        return signal_id

    def signal_unsubscribe(self, signal_id: int) -> None:
        self.signal_unsubscriptions.append(signal_id)

    def connect(self, signal: str, callback: Callable[..., None]) -> int:
        assert signal == "closed"
        handler_id = self._next_id
        self._next_id += 1
        self.closed_handlers[handler_id] = callback
        return handler_id

    def handler_is_connected(self, handler_id: int) -> bool:
        return handler_id in self.closed_handlers and handler_id not in self.disconnected

    def disconnect(self, handler_id: int) -> None:
        self.disconnected.append(handler_id)


def _gio_transport(
    connection: _GioConnection | None = None,
) -> tuple[GioPortalTransport, _GioConnection, Any, Any]:
    connection = connection or _GioConnection()
    gio = SimpleNamespace(
        DBusCallFlags=SimpleNamespace(NONE=0),
        DBusSignalFlags=SimpleNamespace(NONE=0),
        BusType=SimpleNamespace(SESSION=1),
    )
    glib = SimpleNamespace(Variant=_PackedVariant, VariantType=_VariantType)
    transport = GioPortalTransport()
    transport._connection = connection
    transport._gio = gio
    transport._glib = glib
    return transport, connection, gio, glib


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda transport: setattr(transport, "version", 0), "不受支持"),
        (
            lambda transport: setattr(
                transport,
                "get_global_shortcuts_version",
                lambda: (_ for _ in ()).throw(RuntimeError("代理不可用")),
            ),
            "代理不可用",
        ),
        (lambda transport: setattr(transport, "unique_name", "not-a-unique-name"), "唯一名称"),
    ],
)
def test_start_failures_are_nonfatal_and_leave_no_live_transport(
    setup: Callable[[_PortalTransport], None],
    expected: str,
) -> None:
    transport = _PortalTransport()
    setup(transport)
    controller = _Controller()
    shortcuts = PortalShortcuts(controller, transport=transport)

    shortcuts.start()
    shortcuts.shutdown()

    assert expected in (shortcuts.fallback_message or "")
    assert shortcuts.session_handle is None
    assert shortcuts.bound_shortcuts == frozenset()
    assert all(not subscription.active for subscription in transport.subscriptions)
    assert controller.calls == []


@pytest.mark.parametrize("token", ["9starts_with_digit", "contains-hyphen", 42])
def test_invalid_generated_tokens_fail_before_portal_call(token: Any) -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=lambda _kind: token,
    )

    shortcuts.start()

    assert "请求令牌" in (shortcuts.fallback_message or "")
    assert transport.create_calls == []
    assert shortcuts.session_handle is None


def test_reconciles_portal_returned_request_paths_and_ignores_old_response() -> None:
    transport = _PortalTransport()
    transport.returned_create_path = "/org/freedesktop/portal/desktop/request/portal/create"
    transport.returned_bind_path = "/org/freedesktop/portal/desktop/request/portal/bind"
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory("create_a", "session_a", "bind_a"),
    )
    shortcuts.start()

    transport.emit_response(
        request_path("create_a"), 0, {"session_handle": session_path("session_a")}
    )
    assert transport.bind_calls == []
    transport.emit_response(
        transport.returned_create_path,
        0,
        {"session_handle": session_path("session_a")},
    )
    assert len(transport.bind_calls) == 1

    transport.emit_response(request_path("bind_a"), 0, {"shortcuts": []})
    assert shortcuts.last_response_code == 0
    assert shortcuts.bound_shortcuts == frozenset()
    transport.emit_response(
        transport.returned_bind_path,
        0,
        {"shortcuts": [(TOGGLE_SHORTCUT_ID, {})]},
    )

    assert shortcuts.bound_shortcuts == frozenset({TOGGLE_SHORTCUT_ID})
    replaced = [
        subscription
        for subscription in transport.subscriptions
        if subscription.interface == REQUEST_INTERFACE
        and subscription.path in {request_path("create_a"), request_path("bind_a")}
    ]
    assert replaced and all(not subscription.active for subscription in replaced)


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ({}, "有效"),
        ({"session_handle": 7}, "有效"),
        ({"session_handle": "not/an/object/path"}, "无效"),
    ],
)
def test_create_response_rejects_missing_or_invalid_session_payload(
    results: Mapping[str, Any],
    expected: str,
) -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory("create_a", "session_a"),
    )
    shortcuts.start()
    transport.emit_response(request_path("create_a"), 0, results)

    assert expected in (shortcuts.fallback_message or "")
    assert shortcuts.session_handle is None
    assert transport.bind_calls == []


def test_create_response_closes_unexpected_valid_session() -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory("create_a", "session_a"),
    )
    shortcuts.start()
    unexpected = session_path("different")
    transport.emit_response(request_path("create_a"), 0, {"session_handle": unexpected})

    assert transport.closed_sessions == [unexpected]
    assert "不匹配" in (shortcuts.fallback_message or "")
    assert shortcuts.session_handle is None
    assert transport.bind_calls == []


def test_invalid_returned_request_path_closes_predicted_request() -> None:
    transport = _PortalTransport()
    transport.returned_create_path = "not/an/object/path"
    shortcuts = PortalShortcuts(
        _Controller(),
        transport=transport,
        token_factory=token_factory("create_a", "session_a"),
    )

    shortcuts.start()

    assert transport.closed_requests == [request_path("create_a")]
    assert "请求路径" in (shortcuts.fallback_message or "")


def test_bind_proxy_error_closes_session_and_forces_stop_once() -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory("create_a", "session_a", "bind_a"),
    )
    transport.bind_shortcuts = lambda *_args: (_ for _ in ()).throw(RuntimeError("绑定代理失败"))  # type: ignore[method-assign]
    shortcuts.start()
    transport.emit_response(
        request_path("create_a"),
        0,
        {"session_handle": session_path("session_a")},
    )
    shortcuts.shutdown()

    assert "绑定代理失败" in (shortcuts.fallback_message or "")
    assert transport.closed_requests == [request_path("bind_a")]
    assert transport.closed_sessions == [session_path("session_a")]
    assert controller.calls == ["hold_stop"]


def test_malformed_bind_payload_is_treated_as_no_authoritative_bindings() -> None:
    shortcuts, transport, _controller, _session, bind_request = begin_binding()
    transport.emit_response(
        bind_request,
        0,
        {
            "shortcuts": [
                HOLD_SHORTCUT_ID,
                (),
                (TOGGLE_SHORTCUT_ID,),
                (9, {}),
                ("portal-extra", {}),
            ]
        },
    )

    assert shortcuts.bound_shortcuts == frozenset()
    assert "未绑定任何" in (shortcuts.fallback_message or "")


def test_signal_dispatch_filters_session_binding_and_press_state() -> None:
    shortcuts, transport, controller, session, bind_request = begin_binding()
    complete_binding(transport, bind_request, [HOLD_SHORTCUT_ID, TOGGLE_SHORTCUT_ID])

    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session_path("other"),
        HOLD_SHORTCUT_ID,
        1,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Deactivated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        2,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        "portal-extra",
        3,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        4,
        {},
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "ShortcutsChanged",
        PORTAL_OBJECT_PATH,
        session_path("other"),
        [],
    )
    assert shortcuts.bound_shortcuts == frozenset({HOLD_SHORTCUT_ID, TOGGLE_SHORTCUT_ID})
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "ShortcutsChanged",
        PORTAL_OBJECT_PATH,
        session,
        [(TOGGLE_SHORTCUT_ID, {}), ("portal-extra", {})],
    )
    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Deactivated",
        PORTAL_OBJECT_PATH,
        session,
        HOLD_SHORTCUT_ID,
        5,
        {},
    )

    assert shortcuts.bound_shortcuts == frozenset({TOGGLE_SHORTCUT_ID})
    assert shortcuts.fallback_message is None
    assert controller.calls == ["hold_start"]

    transport.emit(
        GLOBAL_SHORTCUTS_INTERFACE,
        "ShortcutsChanged",
        PORTAL_OBJECT_PATH,
        session,
        "malformed",
    )
    assert shortcuts.bound_shortcuts == frozenset()
    assert "已移除" in (shortcuts.fallback_message or "")


def test_rebind_shutdown_and_bus_loss_are_idempotent_across_generations() -> None:
    transport = _PortalTransport()
    controller = _Controller()
    shortcuts = PortalShortcuts(
        controller,
        transport=transport,
        token_factory=token_factory(
            "create_a",
            "session_a",
            "bind_a",
            "create_b",
            "session_b",
            "bind_b",
        ),
    )
    shortcuts.start()
    first_create_callback = next(
        item.callback
        for item in transport.subscriptions
        if item.interface == REQUEST_INTERFACE and item.path == request_path("create_a")
    )
    transport.emit_response(
        request_path("create_a"),
        0,
        {"session_handle": session_path("session_a")},
    )
    complete_binding(transport, request_path("bind_a"), [TOGGLE_SHORTCUT_ID])
    shortcuts.rebind()
    first_create_callback(0, {"session_handle": session_path("session_a")})
    transport.emit_response(
        request_path("create_b"),
        0,
        {"session_handle": session_path("session_b")},
    )
    complete_binding(transport, request_path("bind_b"), [HOLD_SHORTCUT_ID])

    transport.lose_name()
    transport.lose_name()
    shortcuts.shutdown()
    shortcuts.shutdown()
    shortcuts.close_pending_request()

    assert controller.calls == ["hold_stop", "hold_stop"]
    assert transport.closed_sessions == [session_path("session_a")]
    assert shortcuts.session_handle is None
    assert shortcuts.bound_shortcuts == frozenset()


def test_remote_close_errors_do_not_break_local_shutdown() -> None:
    shortcuts, transport, controller, _session, _bind_request = begin_binding()
    transport.close_request = lambda _path: (_ for _ in ()).throw(RuntimeError("close request"))  # type: ignore[method-assign]
    transport.close_session = lambda _path: (_ for _ in ()).throw(RuntimeError("close session"))  # type: ignore[method-assign]

    shortcuts.shutdown()
    shortcuts.shutdown()

    assert shortcuts.session_handle is None
    assert shortcuts.bound_shortcuts == frozenset()
    assert controller.calls == ["hold_stop"]


def test_gio_transport_packs_calls_and_closes_portal_objects() -> None:
    transport, connection, _gio, _glib = _gio_transport()

    assert transport.unique_name == ":1.77"
    assert transport.get_global_shortcuts_version() == 2
    assert transport.create_session({"handle_token": "create_a"}) == "/request/create"
    assert (
        transport.bind_shortcuts(
            "/session/a",
            [(HOLD_SHORTCUT_ID, {"description": "按住说话"})],
            "wayland:parent",
            {"handle_token": "bind_a"},
        )
        == "/request/bind"
    )
    transport.close_request("/request/create")
    transport.close_session("/session/a")

    by_method = {call[3]: call for call in connection.calls}
    get_parameters = by_method["Get"][4]
    assert get_parameters.signature == "(ss)"
    assert get_parameters.value == (GLOBAL_SHORTCUTS_INTERFACE, "version")
    create_parameters = by_method["CreateSession"][4]
    assert create_parameters.signature == "(a{sv})"
    create_options = create_parameters.value[0]
    assert create_options["handle_token"].signature == "s"
    assert create_options["handle_token"].value == "create_a"
    bind_parameters = by_method["BindShortcuts"][4]
    assert bind_parameters.signature == "(oa(sa{sv})sa{sv})"
    assert bind_parameters.value[0] == "/session/a"
    assert bind_parameters.value[1][0][0] == HOLD_SHORTCUT_ID
    assert bind_parameters.value[1][0][1]["description"].value == "按住说话"
    assert bind_parameters.value[2] == "wayland:parent"
    assert bind_parameters.value[3]["handle_token"].value == "bind_a"
    close_calls = [call for call in connection.calls if call[3] == "Close"]
    assert [(call[1], call[2]) for call in close_calls] == [
        ("/request/create", REQUEST_INTERFACE),
        ("/session/a", SESSION_INTERFACE),
    ]


def test_gio_signal_transport_unpacks_values_filters_name_loss_and_unsubscribes() -> None:
    transport, connection, _gio, _glib = _gio_transport()
    received: list[tuple[Any, ...]] = []
    losses: list[str] = []

    signal_id = transport.subscribe(
        GLOBAL_SHORTCUTS_INTERFACE,
        "Activated",
        PORTAL_OBJECT_PATH,
        lambda *args: received.append(args),
    )
    signal_handler = connection.signal_subscriptions[signal_id][6]
    signal_handler(
        None,
        None,
        None,
        None,
        None,
        _PackedVariant("(sv)", ("id", _PackedVariant("s", "value"))),
        None,
    )
    watch = transport.watch_name_loss(lambda: losses.append("lost"))
    owner_signal_id = watch[1]
    owner_handler = connection.signal_subscriptions[owner_signal_id][6]
    owner_handler(
        None,
        None,
        None,
        None,
        None,
        _PackedVariant("(sss)", (PORTAL_BUS_NAME, ":1.1", ":1.2")),
        None,
    )
    owner_handler(
        None,
        None,
        None,
        None,
        None,
        _PackedVariant("(sss)", ("other.name", ":1.1", "")),
        None,
    )
    owner_handler(
        None,
        None,
        None,
        None,
        None,
        _PackedVariant("(sss)", (PORTAL_BUS_NAME, ":1.1", "")),
        None,
    )
    connection.closed_handlers[watch[2]](connection)

    transport.unsubscribe(signal_id)
    transport.unsubscribe(watch)
    transport.unsubscribe(watch)

    assert received == [("id", "value")]
    assert losses == ["lost", "lost"]
    assert signal_id in connection.signal_unsubscriptions
    assert owner_signal_id in connection.signal_unsubscriptions
    assert connection.disconnected == [watch[2]]


def test_gio_transport_rejects_bus_without_unique_name() -> None:
    transport, _connection, _gio, _glib = _gio_transport(_GioConnection(unique_name=None))

    with pytest.raises(RuntimeError, match="唯一名称"):
        _ = transport.unique_name


def test_gio_transport_lazy_loads_injected_gi_once(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _GioConnection()
    required: list[tuple[str, str]] = []
    gi = ModuleType("gi")
    gi.require_version = lambda name, version: required.append((name, version))  # type: ignore[attr-defined]
    repository = ModuleType("gi.repository")
    gio = SimpleNamespace(
        BusType=SimpleNamespace(SESSION=1),
        bus_get_sync=lambda bus_type, cancellable: connection,
    )
    glib = SimpleNamespace()
    repository.Gio = gio  # type: ignore[attr-defined]
    repository.GLib = glib  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    transport = GioPortalTransport()

    assert transport.unique_name == ":1.77"
    assert transport.unique_name == ":1.77"
    assert required == [("Gio", "2.0")]


def test_gio_import_failure_becomes_portal_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)
    controller = _Controller()
    shortcuts = PortalShortcuts(controller, transport=GioPortalTransport())

    shortcuts.start()

    assert "无法加载 Gio" in (shortcuts.fallback_message or "")
    assert shortcuts.is_bound is False
    assert controller.calls == []
