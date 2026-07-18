from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import urllib.request
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from urllib.parse import urlsplit

from .model_catalog import MODEL_CATALOG, ModelSpec
from .paths import AppPaths


_MANIFEST_NAME = "manifest.json"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class ModelManagerError(RuntimeError):
    """模型安装或校验失败。"""


class ModelLockError(ModelManagerError):
    """另一个进程正在操作同一模型。"""


class DownloadTransport(Protocol):
    def open(self, url: str) -> BinaryIO: ...


class HttpsTransport:
    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        self._timeout_seconds = timeout_seconds

    def open(self, url: str) -> BinaryIO:
        response = urllib.request.urlopen(  # noqa: S310 - URL 是目录中固定的 HTTPS 地址
            urllib.request.Request(url, headers={"User-Agent": "type4me-linux/0.1"}),
            timeout=self._timeout_seconds,
        )
        final_url = response.geturl()
        if urlsplit(final_url).scheme.lower() != "https":
            response.close()
            raise ModelManagerError(f"下载重定向到非 HTTPS 地址：{final_url}")
        return response


class ModelManager:
    def __init__(
        self,
        paths: AppPaths,
        *,
        catalog: Mapping[str, ModelSpec] | Iterable[ModelSpec] = MODEL_CATALOG,
        transport: DownloadTransport | Callable[[str], BinaryIO] | None = None,
        active_model_ids: Callable[[], Collection[str]] | None = None,
    ) -> None:
        self.paths = paths
        if isinstance(catalog, Mapping):
            self._catalog = dict(catalog)
        else:
            self._catalog = {spec.id: spec for spec in catalog}
        self._transport = transport or HttpsTransport()
        self._active_model_ids = active_model_ids or (lambda: ())

        self._models_root = paths.data / "models"
        self._versions_root = self._models_root / "versions"
        self._downloads_root = paths.cache / "model-downloads"
        self._state_root = paths.state / "model-manager"
        for root in (paths.data, paths.cache, paths.state):
            _ensure_private_directory(root)
        for root in (
            self._models_root,
            self._versions_root,
            self._downloads_root,
            self._state_root,
        ):
            _ensure_private_directory(root)

    def install(self, model_id: str) -> Path:
        spec = self._spec(model_id)
        with self._lock(model_id):
            return self._install_locked(spec)

    def update(self, model_id: str) -> Path:
        spec = self._spec(model_id)
        with self._lock(model_id):
            return self._install_locked(spec)

    def check(self, model_id: str) -> dict[str, object]:
        spec = self._spec(model_id)
        pointer = self._current_pointer(spec.id)
        result: dict[str, object] = {
            "id": spec.id,
            "installed": False,
            "ok": False,
            "version": None,
            "path": None,
            "missing": [],
            "extra": [],
            "corrupt": [],
            "errors": [],
        }
        try:
            target_name, payload = self._read_current_pointer(spec.id, pointer)
        except FileNotFoundError:
            result["errors"] = ["模型尚未安装。"]
            return result
        except ModelManagerError as exc:
            result["errors"] = [str(exc)]
            return result

        result["installed"] = True
        result["version"] = target_name
        result["path"] = str(payload)
        details = self._check_payload(payload, spec)
        result.update(details)
        result["ok"] = not any(result[key] for key in ("missing", "extra", "corrupt", "errors"))
        return result

    def remove(self, model_id: str, *, force: bool = False) -> bool:
        spec = self._spec(model_id)
        with self._lock(model_id):
            pointer_dir = self._models_root / spec.id
            version_dir = self._versions_root / spec.id
            present = _lexists(pointer_dir) or _lexists(version_dir)
            if not present:
                return False
            if not force and spec.id in set(self._active_model_ids()):
                raise ModelManagerError(
                    f"模型“{spec.id}”正在配置中使用；请先切换模型或使用 force。"
                )
            _remove_no_follow(pointer_dir)
            _remove_no_follow(version_dir)
            _fsync_directory(self._models_root)
            _fsync_directory(self._versions_root)
            return True

    def resolve(self, model_id: str) -> Path:
        result = self.check(model_id)
        if not result["ok"]:
            raise ModelManagerError(f"模型“{model_id}”未通过完整性校验。")
        return Path(str(result["path"]))

    def _spec(self, model_id: str) -> ModelSpec:
        try:
            spec = self._catalog[model_id]
        except KeyError as exc:
            raise ModelManagerError(f"未知模型 ID：{model_id}") from exc
        _validate_component(spec.id, "模型 ID")
        _validate_component(spec.version, "模型版本")
        if spec.id != model_id:
            raise ModelManagerError(f"目录键与模型 ID 不一致：{model_id}")
        if urlsplit(spec.url).scheme.lower() != "https":
            raise ModelManagerError(f"模型源必须使用 HTTPS：{spec.url}")
        if spec.size_bytes <= 0:
            raise ModelManagerError(f"模型“{spec.id}”的预期大小无效。")
        return spec

    def _install_locked(self, spec: ModelSpec) -> Path:
        expected_digest = _decode_sri(spec)
        digest_hex = expected_digest.hex()
        version_name = f"{spec.version}-{digest_hex[:12]}"
        model_versions = self._versions_root / spec.id
        _ensure_private_directory(model_versions)
        destination = model_versions / version_name

        if _lexists(destination):
            details = self._check_payload(destination, spec)
            if not any(details[key] for key in ("missing", "extra", "corrupt", "errors")):
                self._activate(spec.id, destination)
                return destination

        partial = self._downloads_root / f"{spec.id}-{spec.version}.partial"
        staging = model_versions / f".staging-{uuid.uuid4().hex}"
        try:
            _unlink_no_follow(partial)
            actual_digest = self._download(spec, partial)
            if actual_digest != expected_digest:
                raise ModelManagerError(f"模型“{spec.id}”的 SHA-256 校验失败。")

            staging.mkdir(mode=0o700)
            if spec.archive_type == "tar.bz2":
                self._extract_archive(spec, partial, staging)
            elif spec.archive_type == "file":
                self._install_file(spec, partial, staging)
            else:
                raise ModelManagerError(f"模型“{spec.id}”使用不支持的归档类型。")

            files = self._hash_staged_files(staging, spec)
            self._write_manifest(staging, spec, digest_hex, files)
            _fsync_directory(staging)

            if _lexists(destination):
                _remove_no_follow(destination)
            os.replace(staging, destination)
            _fsync_directory(model_versions)
            self._activate(spec.id, destination)
            return destination
        except ModelManagerError:
            raise
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise ModelManagerError(f"安装模型“{spec.id}”失败：{exc}") from exc
        finally:
            _unlink_no_follow(partial)
            _remove_no_follow(staging)

    def _download(self, spec: ModelSpec, partial: Path) -> bytes:
        digest = hashlib.sha256()
        total = 0
        try:
            source = (
                self._transport.open(spec.url)
                if hasattr(self._transport, "open")
                else self._transport(spec.url)  # type: ignore[operator]
            )
            descriptor = os.open(
                partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with contextlib.closing(source), os.fdopen(descriptor, "wb", closefd=True) as output:
                while True:
                    chunk = source.read(min(1024 * 1024, spec.size_bytes - total + 1))
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ModelManagerError("下载传输返回了非字节数据。")
                    total += len(chunk)
                    if total > spec.size_bytes:
                        raise ModelManagerError(f"模型“{spec.id}”下载大小超过目录上限。")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        except ModelManagerError:
            raise
        except Exception as exc:
            raise ModelManagerError(f"下载模型“{spec.id}”失败：{exc}") from exc
        if total != spec.size_bytes:
            raise ModelManagerError(
                f"模型“{spec.id}”下载不完整：预期 {spec.size_bytes} 字节，实际 {total} 字节。"
            )
        return digest.digest()

    def _extract_archive(self, spec: ModelSpec, archive: Path, staging: Path) -> None:
        if not spec.top_level_directory:
            raise ModelManagerError(f"模型“{spec.id}”缺少归档顶层目录声明。")
        _validate_archive_contract(spec)
        with tarfile.open(archive, mode="r:bz2") as bundle:
            members = bundle.getmembers()
            validated: list[tuple[tarfile.TarInfo, str | None]] = []
            seen: set[str] = set()
            allowed_files = set(spec.allowed_members)
            allowed_directories = _allowed_directories(allowed_files)
            top = spec.top_level_directory

            for member in members:
                normalized = _safe_archive_name(member.name)
                if normalized in seen:
                    raise ModelManagerError(f"归档包含重复成员：{normalized}")
                seen.add(normalized)
                parts = PurePosixPath(normalized).parts
                if not parts or parts[0] != top:
                    raise ModelManagerError(f"归档成员不在唯一顶层目录内：{normalized}")
                relative = PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else None
                if member.mode & (stat.S_ISUID | stat.S_ISGID):
                    raise ModelManagerError(f"归档成员包含 setuid/setgid 权限：{normalized}")
                if member.isdir():
                    if relative is not None and relative not in allowed_directories:
                        raise ModelManagerError(f"归档包含未列出的目录：{relative}")
                elif member.isreg():
                    if relative is None or relative not in allowed_files:
                        raise ModelManagerError(f"归档包含未列出的文件：{relative or normalized}")
                else:
                    raise ModelManagerError(f"归档包含不安全的链接或设备成员：{normalized}")
                validated.append((member, relative))

            archived_files = {relative for member, relative in validated if member.isreg()}
            missing_allowed = set(spec.required_paths) - archived_files
            if missing_allowed:
                raise ModelManagerError("归档缺少必需文件：" + "、".join(sorted(missing_allowed)))

            for member, relative in validated:
                if relative is None:
                    continue
                output_path = staging.joinpath(*PurePosixPath(relative).parts)
                if member.isdir():
                    _ensure_private_directory(output_path)
                    continue
                _ensure_private_directory(output_path.parent)
                source = bundle.extractfile(member)
                if source is None:
                    raise ModelManagerError(f"无法读取归档成员：{relative}")
                fd = os.open(
                    output_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                written = 0
                try:
                    with os.fdopen(fd, "wb", closefd=True) as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > member.size:
                                raise ModelManagerError(f"归档成员大小异常：{relative}")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    source.close()
                if written != member.size:
                    raise ModelManagerError(f"归档成员被截断：{relative}")

    def _install_file(self, spec: ModelSpec, partial: Path, staging: Path) -> None:
        if spec.top_level_directory is not None:
            raise ModelManagerError(f"单文件模型“{spec.id}”不能声明顶层目录。")
        if len(spec.allowed_members) != 1 or tuple(spec.required_paths) != tuple(
            spec.allowed_members
        ):
            raise ModelManagerError(f"单文件模型“{spec.id}”的成员声明无效。")
        relative = _safe_relative_path(spec.allowed_members[0])
        destination = staging.joinpath(*relative.parts)
        _ensure_private_directory(destination.parent)
        with partial.open("rb") as source:
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb", closefd=True) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())

    def _hash_staged_files(self, staging: Path, spec: ModelSpec) -> dict[str, dict[str, object]]:
        files: dict[str, dict[str, object]] = {}
        for path in sorted(staging.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ModelManagerError(f"暂存目录包含符号链接：{path}")
            if stat.S_ISDIR(metadata.st_mode):
                path.chmod(0o700)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ModelManagerError(f"暂存目录包含特殊文件：{path}")
            relative = path.relative_to(staging).as_posix()
            path.chmod(0o600)
            files[relative] = {"sha256": _hash_regular_file(path), "size": metadata.st_size}
        for required in spec.required_paths:
            relative = _safe_relative_path(required).as_posix()
            if relative not in files:
                raise ModelManagerError(f"模型缺少必需的普通文件：{relative}")
        return files

    def _write_manifest(
        self,
        staging: Path,
        spec: ModelSpec,
        digest_hex: str,
        files: dict[str, dict[str, object]],
    ) -> None:
        manifest = {
            "schema_version": 1,
            "model_id": spec.id,
            "version": spec.version,
            "source_url": spec.url,
            "source_sri": spec.sha256_sri,
            "source_sha256": digest_hex,
            "files": files,
        }
        path = staging / _MANIFEST_NAME
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as output:
            json.dump(manifest, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

    def _activate(self, model_id: str, destination: Path) -> None:
        pointer_dir = self._models_root / model_id
        _ensure_private_directory(pointer_dir)
        current = pointer_dir / "current"
        if _lexists(current):
            metadata = current.lstat()
            if not stat.S_ISLNK(metadata.st_mode):
                raise ModelManagerError(f"当前模型指针不是符号链接：{current}")
            self._validate_pointer_target(model_id, os.readlink(current))
        target = f"../versions/{model_id}/{destination.name}"
        temporary = pointer_dir / f".current-{uuid.uuid4().hex}"
        try:
            os.symlink(target, temporary)
            os.replace(temporary, current)
            _fsync_directory(pointer_dir)
        finally:
            _unlink_no_follow(temporary)

    def _read_current_pointer(self, model_id: str, pointer: Path) -> tuple[str, Path]:
        pointer_dir = pointer.parent
        model_versions = self._versions_root / model_id
        try:
            pointer_dir_metadata = pointer_dir.lstat()
            model_versions_metadata = model_versions.lstat()
            metadata = pointer.lstat()
        except FileNotFoundError:
            raise
        if (
            not stat.S_ISDIR(pointer_dir_metadata.st_mode)
            or stat.S_ISLNK(pointer_dir_metadata.st_mode)
            or not stat.S_ISDIR(model_versions_metadata.st_mode)
            or stat.S_ISLNK(model_versions_metadata.st_mode)
        ):
            raise ModelManagerError("当前模型路径包含不安全的符号链接。")
        if not stat.S_ISLNK(metadata.st_mode):
            raise ModelManagerError(f"当前模型指针不是符号链接：{pointer}")
        target = os.readlink(pointer)
        target_name = self._validate_pointer_target(model_id, target)
        payload = model_versions / target_name
        try:
            payload_metadata = payload.lstat()
        except FileNotFoundError as exc:
            raise ModelManagerError("当前模型指针指向不存在的版本。") from exc
        if not stat.S_ISDIR(payload_metadata.st_mode) or stat.S_ISLNK(payload_metadata.st_mode):
            raise ModelManagerError("当前模型版本不是安全的普通目录。")
        return target_name, payload

    @staticmethod
    def _validate_pointer_target(model_id: str, target: str) -> str:
        parts = PurePosixPath(target).parts
        if len(parts) != 4 or parts[:3] != ("..", "versions", model_id):
            raise ModelManagerError(f"当前模型指针越出模型目录：{target}")
        _validate_component(parts[3], "模型版本目录")
        return parts[3]

    def _current_pointer(self, model_id: str) -> Path:
        return self._models_root / model_id / "current"

    def _check_payload(self, payload: Path, spec: ModelSpec) -> dict[str, list[str]]:
        result = {"missing": [], "extra": [], "corrupt": [], "errors": []}
        try:
            payload_metadata = payload.lstat()
            digest_hex = _decode_sri(spec).hex()
        except (OSError, ModelManagerError) as exc:
            result["errors"].append(f"模型版本目录无效：{exc}")
            return result
        if not stat.S_ISDIR(payload_metadata.st_mode) or stat.S_ISLNK(payload_metadata.st_mode):
            result["errors"].append("模型版本不是安全的普通目录。")
            return result
        expected_directory_name = f"{spec.version}-{digest_hex[:12]}"
        if payload.name != expected_directory_name:
            result["errors"].append("模型版本目录名称与目录摘要不匹配。")

        manifest_path = payload / _MANIFEST_NAME
        try:
            metadata = manifest_path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ModelManagerError("安装清单不是普通文件。")
            descriptor = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as source:
                manifest = json.load(source)
        except FileNotFoundError:
            result["missing"].append(_MANIFEST_NAME)
            return result
        except (OSError, json.JSONDecodeError, ModelManagerError) as exc:
            result["errors"].append(f"无法读取安装清单：{exc}")
            return result

        if (
            manifest.get("schema_version") != 1
            or manifest.get("model_id") != spec.id
            or manifest.get("version") != spec.version
            or manifest.get("source_sri") != spec.sha256_sri
            or manifest.get("source_sha256") != digest_hex
        ):
            result["errors"].append("安装清单与模型目录不匹配。")

        raw_files = manifest.get("files")
        if not isinstance(raw_files, dict):
            result["errors"].append("安装清单中的 files 字段无效。")
            return result
        expected: dict[str, dict[str, object]] = {}
        for name, value in raw_files.items():
            try:
                normalized = _safe_relative_path(name).as_posix()
            except (ModelManagerError, TypeError):
                result["errors"].append(f"安装清单包含不安全路径：{name}")
                continue
            if normalized == _MANIFEST_NAME or not isinstance(value, dict):
                result["errors"].append(f"安装清单文件记录无效：{name}")
                continue
            expected[normalized] = value

        expected_dirs = _allowed_directories(set(expected))
        actual_files: dict[str, Path] = {}
        stack = [payload]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                result["errors"].append(f"无法扫描模型目录：{exc}")
                continue
            for entry in entries:
                relative = Path(entry.path).relative_to(payload).as_posix()
                if relative == _MANIFEST_NAME:
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    result["errors"].append(f"无法检查文件“{relative}”：{exc}")
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if relative not in expected_dirs:
                        result["extra"].append(relative + "/")
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(entry_stat.st_mode):
                    actual_files[relative] = Path(entry.path)
                else:
                    result["extra"].append(relative)

        for name in sorted(set(expected) - set(actual_files)):
            result["missing"].append(name)
        for name in sorted(set(actual_files) - set(expected)):
            result["extra"].append(name)
        for name in sorted(set(expected) & set(actual_files)):
            record = expected[name]
            expected_hash = record.get("sha256")
            expected_size = record.get("size")
            try:
                metadata = actual_files[name].lstat()
                actual_hash = _hash_regular_file(actual_files[name])
            except (OSError, ModelManagerError) as exc:
                result["corrupt"].append(name)
                result["errors"].append(f"无法校验文件“{name}”：{exc}")
                continue
            if metadata.st_size != expected_size or actual_hash != expected_hash:
                result["corrupt"].append(name)

        for required in spec.required_paths:
            normalized = _safe_relative_path(required).as_posix()
            if normalized not in expected and normalized not in result["missing"]:
                result["missing"].append(normalized)
        for key in ("missing", "extra", "corrupt", "errors"):
            result[key] = sorted(set(result[key]))
        return result

    @contextlib.contextmanager
    def _lock(self, model_id: str):
        lock_path = self._state_root / f"{model_id}.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ModelLockError(f"模型“{model_id}”正由另一个进程操作。") from exc
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _decode_sri(spec: ModelSpec) -> bytes:
    value = spec.sha256_sri
    if not value:
        raise ModelManagerError(f"模型“{spec.id}”缺少固定的 SHA-256 摘要，禁止安装。")
    if not value.startswith("sha256-"):
        raise ModelManagerError(f"模型“{spec.id}”的 SRI SHA-256 格式无效。")
    try:
        digest = base64.b64decode(value.removeprefix("sha256-"), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ModelManagerError(f"模型“{spec.id}”的 SRI SHA-256 格式无效。") from exc
    if len(digest) != hashlib.sha256().digest_size:
        raise ModelManagerError(f"模型“{spec.id}”的 SRI SHA-256 长度无效。")
    return digest


def _validate_archive_contract(spec: ModelSpec) -> None:
    if not spec.allowed_members or not spec.required_paths:
        raise ModelManagerError(f"模型“{spec.id}”的归档成员声明为空。")
    normalized_allowed = {_safe_relative_path(path).as_posix() for path in spec.allowed_members}
    if len(normalized_allowed) != len(spec.allowed_members):
        raise ModelManagerError(f"模型“{spec.id}”的允许成员声明重复。")
    normalized_required = {_safe_relative_path(path).as_posix() for path in spec.required_paths}
    if not normalized_required <= normalized_allowed:
        raise ModelManagerError(f"模型“{spec.id}”的必需文件不在允许成员中。")
    if spec.top_level_directory is None or "/" in spec.top_level_directory:
        raise ModelManagerError(f"模型“{spec.id}”的顶层目录声明无效。")
    _validate_component(spec.top_level_directory, "归档顶层目录")


def _safe_archive_name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name:
        raise ModelManagerError(f"归档包含绝对或无效路径：{name}")
    raw_parts = name.split("/")
    if ".." in raw_parts:
        raise ModelManagerError(f"归档包含路径穿越成员：{name}")
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ModelManagerError(f"归档包含无效路径：{name}")
    return PurePosixPath(*parts).as_posix()


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ModelManagerError(f"模型成员路径无效：{value}")
    path = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ModelManagerError(f"模型成员路径无效：{value}")
    return path


def _allowed_directories(files: set[str]) -> set[str]:
    directories: set[str] = set()
    for name in files:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _validate_component(value: str, label: str) -> None:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ModelManagerError(f"{label}包含不安全字符：{value}")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ModelManagerError(f"模型目录不是安全的普通目录：{path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ModelManagerError(f"模型目录不属于当前用户：{path}")
        path.chmod(0o700)
    except OSError as exc:
        raise ModelManagerError(f"无法创建模型目录“{path}”：{exc}") from exc


def _hash_regular_file(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ModelManagerError(f"模型文件不是普通文件：{path}")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb", closefd=True) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _unlink_no_follow(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        raise ModelManagerError(f"拒绝将目录当作文件删除：{path}")
    path.unlink()


def _remove_no_follow(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        path.unlink()
        return
    with os.scandir(path) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        _remove_no_follow(child)
    path.rmdir()
