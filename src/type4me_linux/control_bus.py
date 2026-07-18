from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

BUS_NAME = "io.github.vitus.Type4Me"
OBJECT_PATH = "/io/github/vitus/Type4Me"
INTERFACE_NAME = "io.github.vitus.Type4Me.Controller"

_METHOD_ACTIONS = {
    "Toggle": "toggle",
    "HoldStart": "hold_start",
    "HoldStop": "hold_stop",
    "Cancel": "cancel",
    "ShowWindow": "show_window",
}

_INTROSPECTION_XML = f"""
<node>
  <interface name="{INTERFACE_NAME}">
    <method name="Toggle"/>
    <method name="HoldStart"/>
    <method name="HoldStop"/>
    <method name="Cancel"/>
    <method name="ShowWindow"/>
  </interface>
</node>
"""


@runtime_checkable
class ControllerProtocol(Protocol):
    def toggle(self) -> None: ...

    def hold_start(self) -> None: ...

    def hold_stop(self) -> None: ...

    def cancel(self) -> None: ...

    def show_window(self) -> None: ...


class ControlBusUnavailableError(RuntimeError):
    """常驻控制器未持有其约定的总线名称。"""


class ControlBusClientTransport(Protocol):
    def call(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method_name: str,
    ) -> None: ...


class ControlBusExporter(Protocol):
    def start(self, dispatch: Callable[[str], None]) -> None: ...

    def stop(self) -> None: ...


class ControlBusService:
    """通过结构化协议导出控制器，不依赖其具体实现。"""

    def __init__(
        self,
        controller: ControllerProtocol,
        *,
        exporter: ControlBusExporter | None = None,
    ) -> None:
        self._controller = controller
        self._exporter = exporter or GioControlBusExporter()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._exporter.start(self.dispatch)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._exporter.stop()

    def dispatch(self, method_name: str) -> None:
        try:
            action = _METHOD_ACTIONS[method_name]
        except KeyError as exc:
            raise ValueError(f"未知的控制器 D-Bus 方法：{method_name}") from exc
        getattr(self._controller, action)()


class ControlBusClient:
    def __init__(self, transport: ControlBusClientTransport | None = None) -> None:
        self._transport = transport or GioControlBusClientTransport()

    def toggle(self) -> None:
        self._call("Toggle")

    def hold_start(self) -> None:
        self._call("HoldStart")

    def hold_stop(self) -> None:
        self._call("HoldStop")

    def cancel(self) -> None:
        self._call("Cancel")

    def show_window(self) -> None:
        self._call("ShowWindow")

    def _call(self, method_name: str) -> None:
        self._transport.call(BUS_NAME, OBJECT_PATH, INTERFACE_NAME, method_name)


class GioControlBusClientTransport:
    """仅在实际调用会话总线时加载 Gio 的客户端适配器。"""

    def __init__(self) -> None:
        self._connection: object | None = None
        self._gio: object | None = None
        self._glib: object | None = None

    def call(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method_name: str,
    ) -> None:
        Gio, GLib, connection = self._ensure_connection()
        try:
            owner_reply = connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (bus_name,)),
                GLib.VariantType.new("(b)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            (has_owner,) = owner_reply.unpack()
        except Exception as exc:
            raise RuntimeError(f"无法查询常驻服务状态：{exc}") from exc
        if not has_owner:
            raise ControlBusUnavailableError(
                f"常驻服务未运行，D-Bus 名称 {bus_name} 当前没有所有者。"
            )

        try:
            connection.call_sync(
                bus_name,
                object_path,
                interface_name,
                method_name,
                None,
                GLib.VariantType.new("()"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as exc:
            raise ControlBusUnavailableError(
                f"无法调用常驻服务 {bus_name} 的 {method_name}：{exc}"
            ) from exc

    def _ensure_connection(self):  # type: ignore[no-untyped-def]
        if self._connection is None:
            try:
                import gi

                gi.require_version("Gio", "2.0")
                from gi.repository import Gio, GLib
            except (ImportError, ValueError) as exc:
                raise RuntimeError("无法加载 Gio，不能连接常驻服务。") from exc
            self._gio = Gio
            self._glib = GLib
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._gio, self._glib, self._connection


class GioControlBusExporter:
    """供常驻应用进程使用的延迟加载 Gio 服务适配器。"""

    def __init__(self) -> None:
        self._connection: object | None = None
        self._registration_id = 0
        self._owner_id = 0
        self._gio: object | None = None

    def start(self, dispatch: Callable[[str], None]) -> None:
        if self._registration_id:
            return
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except (ImportError, ValueError) as exc:
            raise RuntimeError("无法加载 Gio，不能导出常驻控制服务。") from exc

        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML)

        def on_method_call(
            _connection,
            _sender,
            _object_path,
            _interface_name,
            method_name,
            _parameters,
            invocation,
        ) -> None:  # type: ignore[no-untyped-def]
            try:
                dispatch(method_name)
            except Exception as exc:
                invocation.return_dbus_error(
                    f"{INTERFACE_NAME}.Error",
                    f"控制器操作失败：{exc}",
                )
                return
            invocation.return_value(None)

        registration_id = connection.register_object(
            OBJECT_PATH,
            node.interfaces[0],
            on_method_call,
            None,
            None,
        )
        if not registration_id:
            raise RuntimeError(f"无法注册 D-Bus 对象 {OBJECT_PATH}。")

        owner_id = Gio.bus_own_name_on_connection(
            connection,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            None,
            None,
        )
        self._connection = connection
        self._registration_id = registration_id
        self._owner_id = owner_id
        self._gio = Gio

    def stop(self) -> None:
        if self._connection is not None and self._registration_id:
            self._connection.unregister_object(self._registration_id)
        if self._gio is not None and self._owner_id:
            self._gio.bus_unown_name(self._owner_id)
        self._connection = None
        self._registration_id = 0
        self._owner_id = 0
        self._gio = None
