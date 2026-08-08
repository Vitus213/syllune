from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import stat
import tarfile
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

import type4me_linux.model_manager as model_manager_module
from type4me_linux.model_catalog import ModelSpec
from type4me_linux.model_manager import (
    HttpsTransport,
    ModelLockError,
    ModelManager,
    ModelManagerError,
)
from type4me_linux.paths import AppPaths


class MemoryTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def open(self, url: str) -> io.BytesIO:
        self.calls.append(url)
        return io.BytesIO(self.payload)


class InterruptedStream(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise OSError("interrupted")
        return super().read(min(size, 8))


class InterruptedTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def open(self, url: str) -> InterruptedStream:
        return InterruptedStream(self.payload)


def _sri(payload: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=None,
    )


def _tar(
    entries: list[tuple[str, bytes | None, bytes | None, int]],
    *,
    include_top: bool = True,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2", format=tarfile.PAX_FORMAT) as archive:
        if include_top:
            top = tarfile.TarInfo("bundle")
            top.type = tarfile.DIRTYPE
            top.mode = 0o755
            top.mtime = 0
            archive.addfile(top)
        for name, content, member_type, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.mtime = 0
            if member_type is not None:
                info.type = member_type
            if member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "bundle/model.bin"
                archive.addfile(info)
            elif member_type in (tarfile.CHRTYPE, tarfile.BLKTYPE):
                info.devmajor = 1
                info.devminor = 3
                archive.addfile(info)
            elif member_type == tarfile.DIRTYPE:
                archive.addfile(info)
            else:
                data = content or b""
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _valid_archive() -> bytes:
    return _tar(
        [
            ("bundle/model.bin", b"model-v1", None, 0o644),
            ("bundle/tokenizer", None, tarfile.DIRTYPE, 0o755),
            ("bundle/tokenizer/tokens.txt", b"a\nb\n", None, 0o644),
        ]
    )


def _spec(payload: bytes, *, version: str = "v1") -> ModelSpec:
    return ModelSpec(
        id="tiny-model",
        version=version,
        url="https://example.invalid/tiny-model.tar.bz2",
        sha256_sri=_sri(payload),
        size_bytes=len(payload),
        archive_type="tar.bz2",
        top_level_directory="bundle",
        allowed_members=("model.bin", "tokenizer/tokens.txt"),
        required_paths=("model.bin", "tokenizer/tokens.txt"),
        license_source="https://example.invalid/LICENSE",
        license_status="测试许可证状态。",
    )


def _manager(
    tmp_path: Path,
    spec: ModelSpec,
    transport: object,
    *,
    active=lambda: (),
) -> ModelManager:
    return ModelManager(
        _paths(tmp_path),
        catalog={spec.id: spec},
        transport=transport,  # type: ignore[arg-type]
        active_model_ids=active,
    )


def test_archive_install_manifest_activation_and_offline_check(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    transport = MemoryTransport(payload)
    manager = _manager(tmp_path, spec, transport)

    installed = manager.install(spec.id)

    assert installed.name.startswith("v1-")
    assert stat.S_IMODE(installed.stat().st_mode) == 0o700
    assert (installed / "model.bin").read_bytes() == b"model-v1"
    assert stat.S_IMODE((installed / "model.bin").stat().st_mode) == 0o600
    pointer = tmp_path / "data/models/tiny-model/current"
    assert pointer.is_symlink()
    assert os.readlink(pointer) == f"../versions/tiny-model/{installed.name}"
    assert not list((tmp_path / "cache/model-downloads").glob("*.partial"))

    offline = _manager(
        tmp_path,
        spec,
        lambda _url: (_ for _ in ()).throw(AssertionError("network used")),
    )
    report = offline.check(spec.id)
    assert report == {
        "id": "tiny-model",
        "installed": True,
        "ok": True,
        "version": installed.name,
        "path": str(installed),
        "missing": [],
        "extra": [],
        "corrupt": [],
        "errors": [],
    }


def test_single_file_install(tmp_path: Path) -> None:
    payload = b"onnx-data"
    spec = ModelSpec(
        id="tiny-vad",
        version="v1",
        url="https://example.invalid/vad.onnx",
        sha256_sri=_sri(payload),
        size_bytes=len(payload),
        archive_type="file",
        top_level_directory=None,
        allowed_members=("vad.onnx",),
        required_paths=("vad.onnx",),
        license_source="https://example.invalid/LICENSE",
        license_status="测试许可证状态。",
    )
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    installed = manager.install(spec.id)

    assert (installed / "vad.onnx").read_bytes() == payload
    assert manager.check(spec.id)["ok"] is True


def test_install_refuses_missing_digest_before_transport(tmp_path: Path) -> None:
    payload = _valid_archive()
    transport = MemoryTransport(payload)
    spec = replace(_spec(payload), sha256_sri=None)
    manager = _manager(tmp_path, spec, transport)

    with pytest.raises(ModelManagerError, match="缺少固定的 SHA-256"):
        manager.install(spec.id)
    assert transport.calls == []


def test_install_rejects_digest_mismatch(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = replace(_spec(payload), sha256_sri=_sri(b"wrong"))
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    with pytest.raises(ModelManagerError, match="SHA-256 校验失败"):
        manager.install(spec.id)
    assert manager.check(spec.id)["installed"] is False


@pytest.mark.parametrize(
    ("size_delta", "message"),
    [(1, "下载不完整"), (-1, "下载大小超过")],
)
def test_install_rejects_truncated_and_oversized_downloads(
    tmp_path: Path, size_delta: int, message: str
) -> None:
    payload = _valid_archive()
    spec = replace(_spec(payload), size_bytes=len(payload) + size_delta)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    with pytest.raises(ModelManagerError, match=message):
        manager.install(spec.id)


@pytest.mark.parametrize(
    ("entries", "allowed", "message"),
    [
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("/bundle/absolute", b"bad", None, 0o644),
            ],
            ("model.bin", "absolute"),
            "绝对或无效路径",
        ),
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("bundle/../escape", b"bad", None, 0o644),
            ],
            ("model.bin", "escape"),
            "路径穿越",
        ),
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("bundle/link", None, tarfile.SYMTYPE, 0o777),
            ],
            ("model.bin", "link"),
            "链接或设备",
        ),
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("bundle/hardlink", None, tarfile.LNKTYPE, 0o644),
            ],
            ("model.bin", "hardlink"),
            "链接或设备",
        ),
        (
            [
                ("bundle/model.bin", b"first", None, 0o644),
                ("bundle/model.bin", b"second", None, 0o644),
            ],
            ("model.bin",),
            "重复成员",
        ),
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("bundle/device", None, tarfile.CHRTYPE, 0o600),
            ],
            ("model.bin", "device"),
            "链接或设备",
        ),
        (
            [("bundle/model.bin", b"bad", None, 0o4755)],
            ("model.bin",),
            "setuid",
        ),
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("bundle/surprise.txt", b"bad", None, 0o644),
            ],
            ("model.bin",),
            "未列出的文件",
        ),
    ],
    ids=[
        "absolute",
        "traversal",
        "symlink",
        "hardlink",
        "duplicate",
        "device",
        "setuid",
        "unlisted",
    ],
)
def test_archive_header_rejections(
    tmp_path: Path,
    entries: list[tuple[str, bytes | None, bytes | None, int]],
    allowed: tuple[str, ...],
    message: str,
) -> None:
    payload = _tar(entries)
    spec = replace(
        _spec(payload),
        allowed_members=allowed,
        required_paths=("model.bin",),
    )
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    with pytest.raises(ModelManagerError, match=message):
        manager.install(spec.id)
    assert not list((tmp_path / "data/models/versions/tiny-model").glob(".staging-*"))


def test_interrupted_download_cleans_partial_and_staging(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, InterruptedTransport(payload))

    with pytest.raises(ModelManagerError, match="下载模型"):
        manager.install(spec.id)

    assert not list((tmp_path / "cache/model-downloads").glob("*.partial"))
    assert not list((tmp_path / "data/models/versions/tiny-model").glob(".staging-*"))


def test_interrupted_staging_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    def fail_manifest(*_args: object) -> None:
        raise OSError("fsync interrupted")

    monkeypatch.setattr(manager, "_write_manifest", fail_manifest)
    with pytest.raises(ModelManagerError, match="fsync interrupted"):
        manager.install(spec.id)

    assert not list((tmp_path / "data/models/versions/tiny-model").glob(".staging-*"))
    assert manager.check(spec.id)["installed"] is False


def test_check_reports_missing_extra_and_corrupt_without_network(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    (installed / "model.bin").write_bytes(b"corrupt")
    (installed / "tokenizer/tokens.txt").unlink()
    (installed / "extra.bin").write_bytes(b"extra")

    report = manager.check(spec.id)

    assert report["ok"] is False
    assert report["missing"] == ["tokenizer/tokens.txt"]
    assert report["extra"] == ["extra.bin"]
    assert report["corrupt"] == ["model.bin"]


def test_failed_update_keeps_previous_current_pointer(tmp_path: Path) -> None:
    payload_v1 = _valid_archive()
    spec_v1 = _spec(payload_v1, version="v1")
    manager_v1 = _manager(tmp_path, spec_v1, MemoryTransport(payload_v1))
    installed_v1 = manager_v1.install(spec_v1.id)
    pointer = tmp_path / "data/models/tiny-model/current"
    old_target = os.readlink(pointer)

    payload_v2 = _tar(
        [
            ("bundle/model.bin", b"model-v2", None, 0o644),
            ("bundle/tokenizer/tokens.txt", b"v2", None, 0o644),
        ]
    )
    spec_v2 = _spec(payload_v2, version="v2")
    manager_v2 = _manager(tmp_path, spec_v2, MemoryTransport(payload_v2[:-1]))

    with pytest.raises(ModelManagerError):
        manager_v2.update(spec_v2.id)

    assert os.readlink(pointer) == old_target
    assert pointer.resolve() == installed_v1
    assert manager_v1.check(spec_v1.id)["ok"] is True


def test_lock_contention_fails_without_downloading(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    transport = MemoryTransport(payload)
    manager = _manager(tmp_path, spec, transport)
    lock_path = tmp_path / "state/model-manager/tiny-model.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ModelLockError, match="另一个进程"):
            manager.install(spec.id)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert transport.calls == []


def test_remove_refuses_active_model_then_force_removes_it(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(
        tmp_path,
        spec,
        MemoryTransport(payload),
        active=lambda: {spec.id},
    )
    manager.install(spec.id)

    with pytest.raises(ModelManagerError, match="正在配置中使用"):
        manager.remove(spec.id)

    assert manager.check(spec.id)["ok"] is True
    assert manager.remove(spec.id, force=True) is True
    assert manager.remove(spec.id, force=True) is False


def test_remove_is_idempotent_when_model_is_absent_even_if_configured(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(
        tmp_path,
        spec,
        MemoryTransport(payload),
        active=lambda: {spec.id},
    )

    assert manager.remove(spec.id) is False


def test_remove_does_not_follow_version_symlink(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    model_versions = tmp_path / "data/models/versions/tiny-model"
    os.symlink(external, model_versions)

    assert manager.remove(spec.id, force=True) is True
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not model_versions.exists()


def test_non_https_catalog_source_is_rejected_before_transport(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = replace(_spec(payload), url="http://example.invalid/model.tar.bz2")
    transport = MemoryTransport(payload)
    manager = _manager(tmp_path, spec, transport)

    with pytest.raises(ModelManagerError, match="必须使用 HTTPS"):
        manager.install(spec.id)
    assert transport.calls == []


class RedirectResponse(io.BytesIO):
    def __init__(self, final_url: str) -> None:
        super().__init__(b"payload")
        self.final_url = final_url

    def geturl(self) -> str:
        return self.final_url


def _file_spec(payload: bytes) -> ModelSpec:
    return ModelSpec(
        id="tiny-file",
        version="v1",
        url="https://example.invalid/model.onnx",
        sha256_sri=_sri(payload),
        size_bytes=len(payload),
        archive_type="file",
        top_level_directory=None,
        allowed_members=("model.onnx",),
        required_paths=("model.onnx",),
        license_source="https://example.invalid/LICENSE",
        license_status="测试许可证状态。",
    )


def test_https_transport_rejects_insecure_redirect_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = RedirectResponse("http://mirror.invalid/model")
    observed: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> RedirectResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return response

    monkeypatch.setattr(model_manager_module.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ModelManagerError, match="重定向到非 HTTPS"):
        HttpsTransport(timeout_seconds=2.5).open("https://example.invalid/model")

    request = observed["request"]
    assert isinstance(request, model_manager_module.urllib.request.Request)
    assert request.full_url == "https://example.invalid/model"
    assert request.get_header("User-agent") == "type4me-linux/0.1"
    assert observed["timeout"] == 2.5
    assert response.closed


def test_https_transport_failure_is_reported_by_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)

    def fail_urlopen(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(model_manager_module.urllib.request, "urlopen", fail_urlopen)
    manager = ModelManager(_paths(tmp_path), catalog={spec.id: spec})

    with pytest.raises(ModelManagerError, match="下载模型.*offline"):
        manager.install(spec.id)
    assert not list((tmp_path / "cache/model-downloads").glob("*.partial"))


def test_callable_transport_and_iterable_catalog_install(tmp_path: Path) -> None:
    payload = b"tiny"
    spec = _file_spec(payload)
    calls: list[str] = []

    def transport(url: str) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(payload)

    manager = ModelManager(_paths(tmp_path), catalog=(spec,), transport=transport)

    assert manager.install(spec.id).joinpath("model.onnx").read_bytes() == payload
    assert calls == [spec.url]


@pytest.mark.parametrize(
    ("sri", "message"),
    [
        ("md5-deadbeef", "格式无效"),
        ("sha256-not-base64!", "格式无效"),
        ("sha256-YQ==", "长度无效"),
    ],
)
def test_install_rejects_malformed_sri_values(tmp_path: Path, sri: str, message: str) -> None:
    payload = _valid_archive()
    spec = replace(_spec(payload), sha256_sri=sri)
    transport = MemoryTransport(payload)

    with pytest.raises(ModelManagerError, match=message):
        _manager(tmp_path, spec, transport).install(spec.id)
    assert transport.calls == []


@pytest.mark.parametrize(
    ("catalog_key", "mutations", "message"),
    [
        ("missing-model", {}, "未知模型 ID"),
        ("alias", {"id": "tiny-model"}, "目录键与模型 ID 不一致"),
        ("../bad", {"id": "../bad"}, "模型 ID包含不安全字符"),
        ("tiny-model", {"version": "bad/version"}, "模型版本包含不安全字符"),
        ("tiny-model", {"size_bytes": 0}, "预期大小无效"),
    ],
)
def test_catalog_entry_validation_precedes_transport(
    tmp_path: Path,
    catalog_key: str,
    mutations: dict[str, object],
    message: str,
) -> None:
    payload = _valid_archive()
    spec = replace(_spec(payload), **mutations)
    transport = MemoryTransport(payload)
    catalog = {} if catalog_key == "missing-model" else {catalog_key: spec}
    manager = ModelManager(_paths(tmp_path), catalog=catalog, transport=transport)

    with pytest.raises(ModelManagerError, match=message):
        manager.install(catalog_key)
    assert transport.calls == []


def test_existing_valid_install_is_reactivated_without_download(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    installed = _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)
    current = tmp_path / "data/models/tiny-model/current"
    current.unlink()
    calls: list[str] = []

    def forbidden_transport(url: str) -> io.BytesIO:
        calls.append(url)
        raise AssertionError("existing valid payload must not be downloaded")

    reactivated = _manager(tmp_path, spec, forbidden_transport).update(spec.id)

    assert reactivated == installed
    assert current.resolve() == installed
    assert calls == []


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("../../outside", "越出模型目录"),
        ("../versions/tiny-model/bad/name", "越出模型目录"),
        ("../versions/tiny-model/bad?name", "包含不安全字符"),
        ("../versions/tiny-model/not-present", "指向不存在的版本"),
    ],
)
def test_check_reports_malformed_current_targets(tmp_path: Path, target: str, message: str) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    manager.install(spec.id)
    current = tmp_path / "data/models/tiny-model/current"
    current.unlink()
    os.symlink(target, current)

    report = manager.check(spec.id)

    assert report["installed"] is False
    assert message in report["errors"][0]


def test_check_reports_plain_current_pointer_and_unsafe_payload_type(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    current = tmp_path / "data/models/tiny-model/current"
    current.unlink()
    current.write_text("not a symlink", encoding="utf-8")

    assert "不是符号链接" in manager.check(spec.id)["errors"][0]

    current.unlink()
    installed.rename(installed.with_name(installed.name + "-backup"))
    installed.write_text("not a directory", encoding="utf-8")
    os.symlink(f"../versions/tiny-model/{installed.name}", current)
    assert "不是安全的普通目录" in manager.check(spec.id)["errors"][0]


def test_check_rejects_symlinked_model_path_components(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    manager.install(spec.id)
    pointer_dir = tmp_path / "data/models/tiny-model"
    moved = pointer_dir.with_name("tiny-model-real")
    pointer_dir.rename(moved)
    os.symlink(moved, pointer_dir)

    report = manager.check(spec.id)

    assert report["installed"] is False
    assert "不安全的符号链接" in report["errors"][0]


def test_existing_install_refuses_non_symlink_current_pointer(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    current = tmp_path / "data/models/tiny-model/current"
    current.unlink()
    current.mkdir()

    with pytest.raises(ModelManagerError, match="当前模型指针不是符号链接"):
        manager.install(spec.id)
    assert installed.is_dir()


def _rewrite_manifest(installed: Path, mutation: str) -> None:
    manifest_path = installed / "manifest.json"
    if mutation == "missing":
        manifest_path.unlink()
        return
    if mutation == "invalid-json":
        manifest_path.write_text("{", encoding="utf-8")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "metadata":
        manifest["version"] = "other"
    elif mutation == "files-list":
        manifest["files"] = []
    elif mutation == "unsafe-name":
        manifest["files"]["../escape"] = {"sha256": "0", "size": 1}
    elif mutation == "manifest-record":
        manifest["files"]["manifest.json"] = {"sha256": "0", "size": 1}
    elif mutation == "non-dict-record":
        manifest["files"]["bad.bin"] = "bad"
    elif mutation == "required-omitted":
        del manifest["files"]["tokenizer/tokens.txt"]
    elif mutation == "bad-hash-size":
        manifest["files"]["model.bin"] = {"sha256": None, "size": "8"}
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("mutation", "field", "message"),
    [
        ("missing", "missing", "manifest.json"),
        ("invalid-json", "errors", "无法读取安装清单"),
        ("metadata", "errors", "安装清单与模型目录不匹配"),
        ("files-list", "errors", "files 字段无效"),
        ("unsafe-name", "errors", "不安全路径"),
        ("manifest-record", "errors", "文件记录无效"),
        ("non-dict-record", "errors", "文件记录无效"),
        ("required-omitted", "missing", "tokenizer/tokens.txt"),
        ("bad-hash-size", "corrupt", "model.bin"),
    ],
)
def test_check_reports_malformed_manifest_contracts(
    tmp_path: Path, mutation: str, field: str, message: str
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    _rewrite_manifest(installed, mutation)

    report = manager.check(spec.id)

    assert report["ok"] is False
    assert any(message in value for value in report[field])


def test_check_rejects_manifest_symlink(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    manifest = installed / "manifest.json"
    external = tmp_path / "external-manifest.json"
    external.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    manifest.unlink()
    os.symlink(external, manifest)

    report = manager.check(spec.id)

    assert report["ok"] is False
    assert "安装清单不是普通文件" in report["errors"][0]


def test_resolve_requires_installed_intact_payload(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    with pytest.raises(ModelManagerError, match="未通过完整性校验"):
        manager.resolve(spec.id)

    installed = manager.install(spec.id)
    assert manager.resolve(spec.id) == installed
    (installed / "model.bin").write_bytes(b"corrupt")
    assert manager.check(spec.id)["ok"] is False
    with pytest.raises(ModelManagerError, match="未通过完整性校验"):
        manager.resolve(spec.id)


def test_resolve_caches_successful_checks_and_explicit_check_evicts_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    checks = 0
    original_check_payload = manager._check_payload

    def count_check_payload(path: Path, checked_spec: ModelSpec) -> dict[str, list[str]]:
        nonlocal checks
        checks += 1
        return original_check_payload(path, checked_spec)

    monkeypatch.setattr(manager, "_check_payload", count_check_payload)

    assert manager.resolve(spec.id) == installed
    assert manager.resolve(spec.id) == installed
    assert checks == 1
    assert manager.check(spec.id)["ok"] is True
    assert checks == 2

    (installed / "model.bin").write_bytes(b"corrupt")
    report = manager.check(spec.id)
    assert report["corrupt"] == ["model.bin"]
    assert checks == 3
    with pytest.raises(ModelManagerError, match="未通过完整性校验"):
        manager.resolve(spec.id)
    assert checks == 4


def test_force_remove_evicts_cached_model_resolution(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    manager.install(spec.id)
    manager.resolve(spec.id)
    assert manager.remove(spec.id, force=True) is True
    with pytest.raises(ModelManagerError, match="未通过完整性校验"):
        manager.resolve(spec.id)


@pytest.mark.parametrize(
    ("mutations", "message"),
    [
        ({"top_level_directory": None}, "缺少归档顶层目录声明"),
        ({"allowed_members": ()}, "归档成员声明为空"),
        ({"required_paths": ()}, "归档成员声明为空"),
        (
            {"allowed_members": ("model.bin", "model.bin")},
            "允许成员声明重复",
        ),
        (
            {"allowed_members": ("model.bin",), "required_paths": ("missing",)},
            "必需文件不在允许成员中",
        ),
        ({"top_level_directory": "bad/top"}, "顶层目录声明无效"),
        ({"top_level_directory": "bad?top"}, "归档顶层目录包含不安全字符"),
        ({"archive_type": "zip"}, "不支持的归档类型"),
    ],
)
def test_install_rejects_invalid_archive_catalog_contracts(
    tmp_path: Path, mutations: dict[str, object], message: str
) -> None:
    payload = _valid_archive()
    spec = replace(_spec(payload), **mutations)

    with pytest.raises(ModelManagerError, match=message):
        _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)


@pytest.mark.parametrize(
    ("entries", "include_top", "mutations", "message"),
    [
        (
            [("bundle/model.bin", b"ok", None, 0o644)],
            True,
            {
                "top_level_directory": "other",
                "allowed_members": ("model.bin",),
                "required_paths": ("model.bin",),
            },
            "不在唯一顶层目录内",
        ),
        (
            [
                ("bundle/model.bin", b"ok", None, 0o644),
                ("bundle/unused", None, tarfile.DIRTYPE, 0o755),
            ],
            True,
            {"allowed_members": ("model.bin",), "required_paths": ("model.bin",)},
            "未列出的目录",
        ),
        (
            [("bundle/model.bin", b"ok", None, 0o644)],
            True,
            {},
            "缺少必需文件",
        ),
        (
            [("bundle/model.bin", b"bad", None, 0o2644)],
            True,
            {"allowed_members": ("model.bin",), "required_paths": ("model.bin",)},
            "setuid/setgid",
        ),
        (
            [("bundle/model.bin", None, tarfile.FIFOTYPE, 0o600)],
            True,
            {"allowed_members": ("model.bin",), "required_paths": ("model.bin",)},
            "链接或设备",
        ),
        (
            [("bundle\\model.bin", b"bad", None, 0o644)],
            False,
            {"allowed_members": ("model.bin",), "required_paths": ("model.bin",)},
            "绝对或无效路径",
        ),
    ],
)
def test_archive_rejects_directory_type_mode_and_layout_edges(
    tmp_path: Path,
    entries: list[tuple[str, bytes | None, bytes | None, int]],
    include_top: bool,
    mutations: dict[str, object],
    message: str,
) -> None:
    archive = _tar(entries, include_top=include_top)
    spec = replace(_spec(archive), **mutations)

    with pytest.raises(ModelManagerError, match=message):
        _manager(tmp_path, spec, MemoryTransport(archive)).install(spec.id)


@pytest.mark.parametrize(
    ("mutations", "message"),
    [
        ({"top_level_directory": "bundle"}, "不能声明顶层目录"),
        ({"allowed_members": ("a", "b")}, "成员声明无效"),
        ({"required_paths": ("other.onnx",)}, "成员声明无效"),
        (
            {"allowed_members": ("../model.onnx",), "required_paths": ("../model.onnx",)},
            "模型成员路径无效",
        ),
    ],
)
def test_single_file_install_rejects_invalid_layout(
    tmp_path: Path, mutations: dict[str, object], message: str
) -> None:
    payload = b"tiny"
    spec = replace(_file_spec(payload), **mutations)

    with pytest.raises(ModelManagerError, match=message):
        _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)


class SizedArchive:
    def __init__(self, declared_size: int, content: bytes) -> None:
        top = tarfile.TarInfo("bundle")
        top.type = tarfile.DIRTYPE
        member = tarfile.TarInfo("bundle/model.bin")
        member.size = declared_size
        self.members = [top, member]
        self.member = member
        self.content = content

    def __enter__(self) -> SizedArchive:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getmembers(self) -> list[tarfile.TarInfo]:
        return self.members

    def extractfile(self, member: tarfile.TarInfo) -> io.BytesIO | None:
        if member is self.member:
            return io.BytesIO(self.content)
        return None


@pytest.mark.parametrize(
    ("declared_size", "content", "message"),
    [
        (1, b"too long", "大小异常"),
        (10, b"short", "被截断"),
    ],
)
def test_archive_member_size_mismatch_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_size: int,
    content: bytes,
    message: str,
) -> None:
    payload = b"archive-placeholder"
    spec = replace(
        _spec(payload),
        allowed_members=("model.bin",),
        required_paths=("model.bin",),
    )
    fake_archive = SizedArchive(declared_size, content)
    monkeypatch.setattr(
        model_manager_module.tarfile,
        "open",
        lambda *_args, **_kwargs: fake_archive,
    )

    with pytest.raises(ModelManagerError, match=message):
        _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)


class TextTransport:
    def open(self, _url: str) -> io.StringIO:
        return io.StringIO("not bytes")


def test_download_rejects_non_bytes_stream(tmp_path: Path) -> None:
    payload = b"tiny"
    spec = _file_spec(payload)

    with pytest.raises(ModelManagerError, match="非字节数据"):
        _manager(tmp_path, spec, TextTransport()).install(spec.id)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("symlink", "暂存目录包含符号链接"),
        ("fifo", "暂存目录包含特殊文件"),
        ("missing", "缺少必需的普通文件"),
    ],
)
def test_install_rejects_unsafe_or_incomplete_staged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    message: str,
) -> None:
    payload = b"tiny"
    spec = _file_spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))

    def stage_file(_spec: ModelSpec, _partial: Path, staging: Path) -> None:
        destination = staging / "model.onnx"
        if kind == "symlink":
            os.symlink(tmp_path / "outside", destination)
        elif kind == "fifo":
            os.mkfifo(destination)

    monkeypatch.setattr(manager, "_install_file", stage_file)

    with pytest.raises(ModelManagerError, match=message):
        manager.install(spec.id)


def test_install_refuses_partial_download_directory(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    partial = tmp_path / "cache/model-downloads/tiny-model-v1.partial"
    partial.mkdir()

    with pytest.raises(ModelManagerError, match="拒绝将目录当作文件删除"):
        manager.install(spec.id)
    assert partial.is_dir()


def test_remove_recursively_deletes_nested_payload_and_pointer_file(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    nested = installed / "untracked/deep"
    nested.mkdir(parents=True)
    (nested / "data.bin").write_bytes(b"data")
    pointer_dir = tmp_path / "data/models/tiny-model"
    current = pointer_dir / "current"
    current.unlink()
    current.write_text("plain pointer", encoding="utf-8")

    assert manager.remove(spec.id, force=True) is True
    assert not pointer_dir.exists()
    assert not installed.exists()


def test_remove_fsync_failure_is_propagated_after_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    manager.install(spec.id)
    original_fsync = model_manager_module.os.fsync
    models_root = tmp_path / "data/models"

    def fail_models_root(descriptor: int) -> None:
        target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if target == models_root:
            raise OSError("directory fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(model_manager_module.os, "fsync", fail_models_root)

    with pytest.raises(OSError, match="directory fsync failed"):
        manager.remove(spec.id, force=True)
    assert manager.remove(spec.id, force=True) is False


def test_check_reports_wrong_version_directory_name(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    renamed = installed.with_name("unexpected-version")
    installed.rename(renamed)
    current = tmp_path / "data/models/tiny-model/current"
    current.unlink()
    os.symlink(f"../versions/tiny-model/{renamed.name}", current)

    report = manager.check(spec.id)

    assert report["installed"] is True
    assert report["ok"] is False
    assert any("目录名称与目录摘要不匹配" in error for error in report["errors"])


def test_check_with_malformed_sri_reports_payload_error(tmp_path: Path) -> None:
    payload = _valid_archive()
    valid_spec = _spec(payload)
    manager = _manager(tmp_path, valid_spec, MemoryTransport(payload))
    manager.install(valid_spec.id)
    malformed = replace(valid_spec, sha256_sri="sha256-invalid!")
    checker = _manager(tmp_path, malformed, MemoryTransport(payload))

    report = checker.check(malformed.id)

    assert report["installed"] is True
    assert report["ok"] is False
    assert "模型版本目录无效" in report["errors"][0]


def test_https_transport_accepts_https_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = RedirectResponse("https://cdn.example.invalid/model")
    monkeypatch.setattr(
        model_manager_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )

    opened = HttpsTransport().open("https://example.invalid/model")

    assert opened is response
    opened.close()


def test_existing_valid_install_keeps_valid_current_without_download(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    transport = MemoryTransport(payload)
    manager = _manager(tmp_path, spec, transport)
    installed = manager.install(spec.id)
    original_target = os.readlink(tmp_path / "data/models/tiny-model/current")

    assert manager.install(spec.id) == installed
    assert os.readlink(tmp_path / "data/models/tiny-model/current") == original_target
    assert transport.calls == [spec.url]


def test_corrupt_existing_version_is_replaced_by_reinstall(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    (installed / "model.bin").write_bytes(b"corrupt")
    replacement_transport = MemoryTransport(payload)

    replaced = _manager(tmp_path, spec, replacement_transport).install(spec.id)

    assert replaced == installed
    assert (replaced / "model.bin").read_bytes() == b"model-v1"
    assert replacement_transport.calls == [spec.url]


def test_archive_rejects_unreadable_regular_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"archive-placeholder"
    spec = replace(
        _spec(payload),
        allowed_members=("model.bin",),
        required_paths=("model.bin",),
    )
    fake_archive = SizedArchive(1, b"x")
    fake_archive.extractfile = lambda _member: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        model_manager_module.tarfile,
        "open",
        lambda *_args, **_kwargs: fake_archive,
    )

    with pytest.raises(ModelManagerError, match="无法读取归档成员"):
        _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)


def test_check_reports_special_entry_and_hash_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    os.symlink(tmp_path / "outside", installed / "unexpected-link")
    original_hash = model_manager_module._hash_regular_file

    def fail_model_hash(path: Path) -> str:
        if path.name == "model.bin":
            raise OSError("read failed")
        return original_hash(path)

    monkeypatch.setattr(model_manager_module, "_hash_regular_file", fail_model_hash)

    report = manager.check(spec.id)

    assert "unexpected-link" in report["extra"]
    assert "model.bin" in report["corrupt"]
    assert any("read failed" in error for error in report["errors"])


def test_check_reports_directory_scan_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    original_scandir = model_manager_module.os.scandir

    def fail_tokenizer_scan(path: object) -> object:
        if Path(path) == installed / "tokenizer":
            raise OSError("scan failed")
        return original_scandir(path)

    monkeypatch.setattr(model_manager_module.os, "scandir", fail_tokenizer_scan)

    report = manager.check(spec.id)

    assert any("scan failed" in error for error in report["errors"])
    assert "tokenizer/tokens.txt" in report["missing"]


@pytest.mark.parametrize("member", ["/absolute.onnx", "bad\\name.onnx"])
def test_single_file_rejects_absolute_or_backslash_member(tmp_path: Path, member: str) -> None:
    payload = b"tiny"
    spec = replace(
        _file_spec(payload),
        allowed_members=(member,),
        required_paths=(member,),
    )

    with pytest.raises(ModelManagerError, match="模型成员路径无效"):
        _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)


def test_manager_rejects_symlinked_storage_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    external = tmp_path / "external-data"
    external.mkdir()
    os.symlink(external, paths.data)

    with pytest.raises(ModelManagerError, match="模型目录不是安全的普通目录"):
        ModelManager(paths, catalog={})


def test_manager_rejects_storage_root_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.data.mkdir()
    monkeypatch.setattr(model_manager_module.os, "geteuid", lambda: os.getuid() + 1)

    with pytest.raises(ModelManagerError, match="不属于当前用户"):
        ModelManager(paths, catalog={})


def test_manager_reports_storage_root_creation_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    paths = AppPaths(
        config=tmp_path / "config",
        data=blocker / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=None,
    )

    with pytest.raises(ModelManagerError, match="无法创建模型目录"):
        ModelManager(paths, catalog={})


def test_non_directory_existing_version_is_replaced_by_install(tmp_path: Path) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    manager.remove(spec.id, force=True)
    installed.parent.mkdir(parents=True)
    installed.write_text("unsafe payload", encoding="utf-8")

    replaced = manager.install(spec.id)

    assert replaced.is_dir()
    assert manager.check(spec.id)["ok"] is True


def test_check_handles_file_type_change_during_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    model_path = installed / "model.bin"
    original_lstat = Path.lstat
    model_lstat_calls = 0

    def changing_lstat(path: Path) -> os.stat_result:
        nonlocal model_lstat_calls
        metadata = original_lstat(path)
        if path == model_path:
            model_lstat_calls += 1
            if model_lstat_calls == 2:
                values = list(metadata)
                values[stat.ST_MODE] = stat.S_IFLNK | 0o777
                return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", changing_lstat)

    report = manager.check(spec.id)

    assert "model.bin" in report["corrupt"]
    assert any("不是普通文件" in error for error in report["errors"])


def test_check_handles_entry_stat_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _valid_archive()
    spec = _spec(payload)
    manager = _manager(tmp_path, spec, MemoryTransport(payload))
    installed = manager.install(spec.id)
    original_scandir = model_manager_module.os.scandir

    class BrokenEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.path = entry.path

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            raise OSError("entry disappeared")

    def broken_model_entry(path: object) -> object:
        scan = original_scandir(path)
        if Path(path) != installed:
            return scan
        with scan:
            entries = list(scan)
        return [BrokenEntry(entry) if entry.name == "model.bin" else entry for entry in entries]

    monkeypatch.setattr(model_manager_module.os, "scandir", broken_model_entry)

    report = manager.check(spec.id)

    assert "model.bin" in report["missing"]
    assert any("entry disappeared" in error for error in report["errors"])


def test_archive_rejects_dot_member_name(tmp_path: Path) -> None:
    payload = _tar([(".", None, tarfile.DIRTYPE, 0o755)], include_top=False)
    spec = replace(
        _spec(payload),
        allowed_members=("model.bin",),
        required_paths=("model.bin",),
    )

    with pytest.raises(ModelManagerError, match="归档包含无效路径"):
        _manager(tmp_path, spec, MemoryTransport(payload)).install(spec.id)
