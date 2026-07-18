from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

import pytest

from type4me_linux.control_bus import (
    BUS_NAME,
    INTERFACE_NAME,
    OBJECT_PATH,
    ControlBusClient,
    ControlBusService,
    ControlBusUnavailableError,
    GioControlBusClientTransport,
    GioControlBusExporter,
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


class _Exporter:
    def __init__(self) -> None:
        self.dispatch: Callable[[str], None] | None = None
        self.starts = 0
        self.stops = 0

    def start(self, dispatch: Callable[[str], None]) -> None:
        self.starts += 1
        self.dispatch = dispatch

    def stop(self) -> None:
        self.stops += 1


class _ClientTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def call(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method_name: str,
    ) -> None:
        self.calls.append((bus_name, object_path, interface_name, method_name))


def test_exact_identifiers_and_service_method_export() -> None:
    assert BUS_NAME == "io.github.vitus.Type4Me"
    assert OBJECT_PATH == "/io/github/vitus/Type4Me"
    assert INTERFACE_NAME == "io.github.vitus.Type4Me.Controller"

    controller = _Controller()
    exporter = _Exporter()
    service = ControlBusService(controller, exporter=exporter)
    service.start()
    service.start()
    assert exporter.dispatch is not None

    for method in ("Toggle", "HoldStart", "HoldStop", "Cancel", "ShowWindow"):
        exporter.dispatch(method)

    service.stop()
    service.stop()
    assert controller.calls == ["toggle", "hold_start", "hold_stop", "cancel", "show_window"]
    assert (exporter.starts, exporter.stops) == (1, 1)


def test_service_rejects_unknown_method() -> None:
    service = ControlBusService(_Controller(), exporter=_Exporter())
    with pytest.raises(ValueError, match="未知的控制器 D-Bus 方法"):
        service.dispatch("StartAnotherRecorder")


def test_client_maps_all_actions_to_exact_dbus_methods() -> None:
    transport = _ClientTransport()
    client = ControlBusClient(transport)

    client.toggle()
    client.hold_start()
    client.hold_stop()
    client.cancel()
    client.show_window()

    assert transport.calls == [
        (BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "Toggle"),
        (BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "HoldStart"),
        (BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "HoldStop"),
        (BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "Cancel"),
        (BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "ShowWindow"),
    ]


def test_service_absence_error_is_exit_worthy_and_chinese() -> None:
    class _AbsentTransport:
        def call(self, *_args: str) -> None:
            raise ControlBusUnavailableError(
                f"常驻服务未运行，D-Bus 名称 {BUS_NAME} 当前没有所有者。"
            )

    with pytest.raises(ControlBusUnavailableError, match="常驻服务未运行") as caught:
        ControlBusClient(_AbsentTransport()).toggle()
    assert BUS_NAME in str(caught.value)


class _Variant:
    def __init__(self, signature: str, value: tuple[object, ...]) -> None:
        self.signature = signature
        self.value = value

    def unpack(self) -> tuple[object, ...]:
        return self.value


class _VariantType:
    @staticmethod
    def new(signature: str) -> str:
        return signature


def _install_fake_gi(monkeypatch, gio: object) -> None:  # type: ignore[no-untyped-def]
    gi = ModuleType("gi")
    gi.require_version = lambda namespace, version: None  # type: ignore[attr-defined]
    repository = ModuleType("gi.repository")
    repository.Gio = gio  # type: ignore[attr-defined]
    repository.GLib = SimpleNamespace(Variant=_Variant, VariantType=_VariantType)  # type: ignore[attr-defined]
    gi.repository = repository  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)


class _Invocation:
    def __init__(self) -> None:
        self.value_calls: list[object] = []
        self.errors: list[tuple[str, str]] = []

    def return_value(self, value: object) -> None:
        self.value_calls.append(value)

    def return_dbus_error(self, name: str, message: str) -> None:
        self.errors.append((name, message))


class _GioConnection:
    def __init__(self, *, owner: bool = True, registration_id: int = 17) -> None:
        self.owner = owner
        self.registration_id = registration_id
        self.calls: list[tuple[object, ...]] = []
        self.callback: Callable[..., None] | None = None
        self.unregistered: list[int] = []
        self.owner_error: Exception | None = None
        self.method_error: Exception | None = None

    def call_sync(self, *args: object) -> _Variant:
        self.calls.append(args)
        if args[3] == "NameHasOwner":
            if self.owner_error is not None:
                raise self.owner_error
            return _Variant("(b)", (self.owner,))
        if self.method_error is not None:
            raise self.method_error
        return _Variant("()", ())

    def register_object(self, *args: object) -> int:
        self.callback = args[2]  # type: ignore[assignment]
        return self.registration_id

    def unregister_object(self, registration_id: int) -> None:
        self.unregistered.append(registration_id)


def _fake_gio(connection: _GioConnection) -> SimpleNamespace:
    owned: list[tuple[object, ...]] = []
    unowned: list[int] = []
    node = SimpleNamespace(interfaces=(object(),))
    return SimpleNamespace(
        BusType=SimpleNamespace(SESSION="session"),
        DBusCallFlags=SimpleNamespace(NONE="none"),
        BusNameOwnerFlags=SimpleNamespace(NONE="none"),
        DBusNodeInfo=SimpleNamespace(new_for_xml=lambda xml: node),
        bus_get_sync=lambda bus_type, cancellable: connection,
        bus_own_name_on_connection=lambda *args: owned.append(args) or 23,
        bus_unown_name=lambda owner_id: unowned.append(owner_id),
        owned=owned,
        unowned=unowned,
    )


def test_gio_exporter_registers_dispatches_and_releases_resources(monkeypatch) -> None:
    connection = _GioConnection()
    gio = _fake_gio(connection)
    _install_fake_gi(monkeypatch, gio)
    calls: list[str] = []
    exporter = GioControlBusExporter()

    exporter.start(calls.append)
    exporter.start(calls.append)
    assert connection.callback is not None
    assert len(gio.owned) == 1
    assert gio.owned[0][1] == BUS_NAME

    success = _Invocation()
    connection.callback(None, None, OBJECT_PATH, INTERFACE_NAME, "Toggle", None, success)
    assert calls == ["Toggle"]
    assert success.value_calls == [None]
    assert success.errors == []

    def reject(_method: str) -> None:
        raise ValueError("不支持")

    exporter.stop()
    exporter.stop()
    assert connection.unregistered == [17]
    assert gio.unowned == [23]

    second_connection = _GioConnection()
    second_gio = _fake_gio(second_connection)
    _install_fake_gi(monkeypatch, second_gio)
    failing_exporter = GioControlBusExporter()
    failing_exporter.start(reject)
    assert second_connection.callback is not None
    failure = _Invocation()
    second_connection.callback(None, None, OBJECT_PATH, INTERFACE_NAME, "Unknown", None, failure)
    assert failure.value_calls == []
    assert failure.errors == [(f"{INTERFACE_NAME}.Error", "控制器操作失败：不支持")]
    failing_exporter.stop()


def test_gio_exporter_reports_import_and_registration_failures(monkeypatch) -> None:
    broken_gi = ModuleType("gi")

    def reject_version(_namespace: str, _version: str) -> None:
        raise ValueError("版本不可用")

    broken_gi.require_version = reject_version  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", broken_gi)
    monkeypatch.delitem(sys.modules, "gi.repository", raising=False)
    with pytest.raises(RuntimeError, match="无法加载 Gio"):
        GioControlBusExporter().start(lambda _method: None)

    connection = _GioConnection(registration_id=0)
    _install_fake_gi(monkeypatch, _fake_gio(connection))
    with pytest.raises(RuntimeError, match=OBJECT_PATH):
        GioControlBusExporter().start(lambda _method: None)


def test_service_start_failure_can_be_retried() -> None:
    class FlakyExporter(_Exporter):
        def start(self, dispatch: Callable[[str], None]) -> None:
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("总线不可用")
            self.dispatch = dispatch

    exporter = FlakyExporter()
    service = ControlBusService(_Controller(), exporter=exporter)
    with pytest.raises(RuntimeError, match="总线不可用"):
        service.start()
    service.start()
    service.stop()
    assert (exporter.starts, exporter.stops) == (2, 1)


def test_gio_client_checks_owner_dispatches_and_reuses_connection(monkeypatch) -> None:
    connection = _GioConnection()
    gio = _fake_gio(connection)
    bus_get_calls: list[tuple[object, object]] = []
    gio.bus_get_sync = lambda *args: bus_get_calls.append(args) or connection
    _install_fake_gi(monkeypatch, gio)
    transport = GioControlBusClientTransport()

    transport.call(BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "Toggle")
    transport.call(BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "Cancel")

    assert len(bus_get_calls) == 1
    assert [call[3] for call in connection.calls] == [
        "NameHasOwner",
        "Toggle",
        "NameHasOwner",
        "Cancel",
    ]
    assert connection.calls[0][4].signature == "(s)"
    assert connection.calls[0][4].value == (BUS_NAME,)
    assert connection.calls[1][0:4] == (BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "Toggle")


@pytest.mark.parametrize(
    ("connection", "error_type", "message"),
    [
        (_GioConnection(owner=False), ControlBusUnavailableError, "没有所有者"),
        (_GioConnection(), RuntimeError, "无法查询常驻服务状态"),
        (_GioConnection(), ControlBusUnavailableError, "无法调用常驻服务"),
    ],
)
def test_gio_client_maps_ownership_and_call_errors(
    monkeypatch,
    connection: _GioConnection,
    error_type: type[Exception],
    message: str,
) -> None:
    if error_type is RuntimeError:
        connection.owner_error = OSError("会话总线断开")
    elif connection.owner:
        connection.method_error = OSError("远端拒绝")
    _install_fake_gi(monkeypatch, _fake_gio(connection))

    with pytest.raises(error_type, match=message) as caught:
        GioControlBusClientTransport().call(BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "Toggle")
    assert isinstance(caught.value.__cause__, OSError) is (connection.owner is True)


def test_gio_client_reports_unavailable_binding(monkeypatch) -> None:
    broken_gi = ModuleType("gi")
    broken_gi.require_version = lambda *_args: (_ for _ in ()).throw(ValueError("缺少 Gio"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", broken_gi)
    monkeypatch.delitem(sys.modules, "gi.repository", raising=False)

    with pytest.raises(RuntimeError, match="无法加载 Gio"):
        GioControlBusClientTransport().call(BUS_NAME, OBJECT_PATH, INTERFACE_NAME, "ShowWindow")
