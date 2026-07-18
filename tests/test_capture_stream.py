from __future__ import annotations

import io
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO

import pytest
import type4me_linux.capture as capture_module

from type4me_linux.capture import RAW_CHUNK_BYTES, RawCaptureSession, Recorder
from type4me_linux.config import CaptureConfig


class _TrackedStream:
    def __init__(self, data: bytes = b"", *, read_limit: int | None = None) -> None:
        self._stream = io.BytesIO(data)
        self._read_limit = read_limit
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._read_limit is not None and size > self._read_limit:
            size = self._read_limit
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True
        self._stream.close()


class _ErrorStream(_TrackedStream):
    def __init__(self, first_read: bytes) -> None:
        super().__init__()
        self._first_read = first_read

    def read(self, size: int = -1) -> bytes:
        if self._first_read:
            data = self._first_read
            self._first_read = b""
            return data
        raise OSError("模拟采集读取失败")


class _FakeProcess:
    def __init__(
        self,
        stdout: _TrackedStream,
        *,
        wait_times_out: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = _TrackedStream("诊断".encode())
        self.wait_times_out = wait_times_out
        self.terminated = 0
        self.killed = 0
        self.wait_calls: list[float | None] = []

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.wait_times_out and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired("pw-record", timeout)
        return 0


class _SignallingFileIO(io.FileIO):
    def __init__(self, fd: int, read_started: Event) -> None:
        super().__init__(fd, mode="rb", closefd=True)
        self._read_started = read_started

    def readinto(self, buffer):  # type: ignore[no-untyped-def]
        self._read_started.set()
        return super().readinto(buffer)


class _LongLivedPipeProcess:
    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self.read_started = Event()
        self.stdout = io.BufferedReader(_SignallingFileIO(read_fd, self.read_started))
        self.stderr = _TrackedStream()
        self._write_fd: int | None = write_fd
        self.terminated = 0
        self.killed = 0
        self.wait_calls: list[float | None] = []
        self.reaped = False

    def terminate(self) -> None:
        self.terminated += 1
        self._close_writer()

    def kill(self) -> None:
        self.killed += 1
        self._close_writer()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.reaped = True
        return 0

    def _close_writer(self) -> None:
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None


def _pipe_session(tmp_path: Path, process: _LongLivedPipeProcess) -> RawCaptureSession:
    return RawCaptureSession(
        CaptureConfig(),
        popen_factory=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
        temp_factory=_temp_factory(tmp_path),  # type: ignore[arg-type]
        shutdown_timeout=0.25,
    )


class _PopenFactory:
    def __init__(self, process: _FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeProcess:
        self.calls.append((argv, kwargs))
        return self.process


def _temp_factory(tmp_path: Path):
    def create(**kwargs: object) -> BinaryIO:
        return tempfile.NamedTemporaryFile(dir=tmp_path, **kwargs)

    return create


def _session(
    tmp_path: Path,
    process: _FakeProcess,
    *,
    wav_path: Path | None = None,
    shutdown_timeout: float = 0.25,
) -> tuple[RawCaptureSession, _PopenFactory]:
    popen_factory = _PopenFactory(process)
    session = RawCaptureSession(
        CaptureConfig(),
        popen_factory=popen_factory,  # type: ignore[arg-type]
        temp_factory=_temp_factory(tmp_path),  # type: ignore[arg-type]
        wav_path=wav_path,
        shutdown_timeout=shutdown_timeout,
    )
    return session, popen_factory


def test_raw_capture_exact_argv_chunks_and_claimed_wav(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 51
    assert len(payload) == RAW_CHUNK_BYTES * 2 + 256
    process = _FakeProcess(_TrackedStream(payload, read_limit=733))
    session, popen_factory = _session(tmp_path, process)

    with session as capture:
        chunks = list(capture)
        wav_path = capture.claim_wav()

    assert [len(chunk) for chunk in chunks] == [RAW_CHUNK_BYTES, RAW_CHUNK_BYTES]
    assert b"".join(chunks) == payload[: RAW_CHUNK_BYTES * 2]
    assert popen_factory.calls == [
        (
            [
                "pw-record",
                "--raw",
                "--rate",
                "16000",
                "--channels",
                "1",
                "--format",
                "s16",
                "--latency",
                "200ms",
                "-",
            ],
            {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE},
        )
    ]
    assert wav_path.exists()
    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.getparams()[:4] == (1, 2, 16000, len(payload) // 2)
        assert wav_file.readframes(wav_file.getnframes()) == payload
    assert process.stdout.closed
    assert process.stderr.closed
    assert process.terminated == 1
    assert process.wait_calls == [0.25]
    session.release_wav()
    assert not wav_path.exists()


def test_unclaimed_owned_wav_is_removed(tmp_path: Path) -> None:
    process = _FakeProcess(_TrackedStream(b"\x01\x00" * 100))
    session, _ = _session(tmp_path, process)

    with session as capture:
        list(capture)
        wav_path = capture.wav_path
        assert wav_path.exists()

    assert not wav_path.exists()


def test_cancel_reaps_child_and_removes_owned_wav(tmp_path: Path) -> None:
    process = _FakeProcess(_TrackedStream(b"\x01\x00" * 100))
    session, _ = _session(tmp_path, process)
    session.start()
    wav_path = session.wav_path

    session.cancel()

    assert not wav_path.exists()
    assert process.stdout.closed
    assert process.stderr.closed
    assert process.terminated == 1
    assert process.wait_calls == [0.25]


def test_request_stop_leaves_active_buffered_reader_owned_by_consumer(
    tmp_path: Path,
) -> None:
    process = _LongLivedPipeProcess()
    session = _pipe_session(tmp_path, process)
    failures: list[BaseException] = []

    def consume() -> None:
        try:
            list(session)
        except BaseException as exc:
            failures.append(exc)

    session.start()
    wav_path = session.wav_path
    worker = Thread(target=consume)
    worker.start()
    assert process.read_started.wait(1)

    session.request_stop()
    session.request_stop()
    worker.join(1)

    assert not worker.is_alive()
    assert failures == []
    assert process.terminated == 1
    assert not process.stdout.closed
    assert process.wait_calls == []

    assert session.stop() == wav_path
    assert process.stdout.closed
    assert process.reaped
    session.release_wav()
    assert not wav_path.exists()


def test_active_buffered_reader_cancel_defers_cleanup_to_reader_owner(
    tmp_path: Path,
) -> None:
    process = _LongLivedPipeProcess()
    session = _pipe_session(tmp_path, process)
    failures: list[BaseException] = []

    def consume() -> None:
        try:
            list(session)
        except BaseException as exc:
            failures.append(exc)

    session.start()
    wav_path = session.wav_path
    worker = Thread(target=consume)
    worker.start()
    assert process.read_started.wait(1)

    session.cancel()
    worker.join(1)

    assert not worker.is_alive()
    assert failures == []
    assert process.terminated == 1
    assert process.stdout.closed
    assert process.stderr.closed
    assert process.reaped
    assert not wav_path.exists()


def test_read_error_reaps_child_and_removes_owned_wav(tmp_path: Path) -> None:
    process = _FakeProcess(_ErrorStream(b"\x01\x00" * 200))
    session, _ = _session(tmp_path, process)

    with pytest.raises(OSError, match="模拟采集读取失败"):
        with session as capture:
            wav_path = capture.wav_path
            list(capture)

    assert not wav_path.exists()
    assert process.stdout.closed
    assert process.stderr.closed
    assert process.terminated == 1
    assert process.wait_calls == [0.25]


def test_terminate_timeout_kills_and_reaps_child(tmp_path: Path) -> None:
    process = _FakeProcess(_TrackedStream(), wait_times_out=True)
    session, _ = _session(tmp_path, process, shutdown_timeout=0.01)

    with session:
        wav_path = session.wav_path

    assert not wav_path.exists()
    assert process.terminated == 1
    assert process.killed == 1
    assert process.wait_calls == [0.01, None]


def test_caller_wav_is_never_deleted(tmp_path: Path) -> None:
    caller_wav = tmp_path / "caller.wav"
    caller_wav.write_bytes("调用方原文件".encode())
    payload = b"\x34\x12" * 320
    process = _FakeProcess(_TrackedStream(payload))
    session, _ = _session(tmp_path, process, wav_path=caller_wav)

    with session as capture:
        assert list(capture) == []

    assert caller_wav.exists()
    with wave.open(str(caller_wav), "rb") as wav_file:
        assert wav_file.readframes(wav_file.getnframes()) == payload


def test_unstarted_and_closed_sessions_reject_invalid_operations(tmp_path: Path) -> None:
    process = _FakeProcess(_TrackedStream())
    session, _ = _session(tmp_path, process)

    with pytest.raises(RuntimeError, match="录音尚未开始"):
        _ = session.wav_path
    with pytest.raises(RuntimeError, match="录音尚未开始"):
        list(session.iter_chunks())

    session.start()
    session.start()
    session.cancel()
    session.cancel()

    assert process.terminated == 1
    session.start()
    assert process.terminated == 1
    with pytest.raises(RuntimeError, match="录音会话已关闭"):
        list(session.iter_chunks())
    with pytest.raises(RuntimeError, match="录音文件已清理"):
        session.claim_wav()

    closed, _ = _session(tmp_path, _FakeProcess(_TrackedStream()))
    closed.close()
    with pytest.raises(RuntimeError, match="录音会话已关闭"):
        closed.start()


def test_stop_claims_owned_wav_until_explicit_release(tmp_path: Path) -> None:
    payload = b"\x01\x00" * 100
    process = _FakeProcess(_TrackedStream(payload))
    session, _ = _session(tmp_path, process)
    session.start()
    list(session)

    wav_path = session.stop()

    assert wav_path.exists()
    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.readframes(wav_file.getnframes()) == payload
    session.release_wav()
    assert not wav_path.exists()


def test_caller_wav_survives_capture_read_failure(tmp_path: Path) -> None:
    caller_wav = tmp_path / "caller-error.wav"
    caller_wav.write_bytes(b"existing")
    process = _FakeProcess(_ErrorStream(b"\x01\x00" * 10))
    session, _ = _session(tmp_path, process, wav_path=caller_wav)

    with pytest.raises(OSError, match="模拟采集读取失败"):
        with session as capture:
            list(capture)

    assert caller_wav.exists()


def test_missing_process_stdout_is_reported_and_caller_wav_is_retained(
    tmp_path: Path,
) -> None:
    caller_wav = tmp_path / "caller-no-stdout.wav"
    process = _FakeProcess(_TrackedStream())
    process.stdout = None  # type: ignore[assignment]
    session, _ = _session(tmp_path, process, wav_path=caller_wav)

    session.start()
    with pytest.raises(RuntimeError, match="录音进程输出不可用"):
        list(session)
    session.close()

    assert caller_wav.exists()


@pytest.mark.parametrize("caller_owned", [False, True])
def test_process_start_failure_cleans_only_owned_wav(
    tmp_path: Path,
    caller_owned: bool,
) -> None:
    caller_wav = tmp_path / "caller-start-failure.wav" if caller_owned else None
    if caller_wav is not None:
        caller_wav.write_bytes(b"existing")

    def fail_popen(_argv: list[str], **_kwargs: object) -> _FakeProcess:
        raise OSError("pw-record 不存在")

    session = RawCaptureSession(
        CaptureConfig(),
        popen_factory=fail_popen,  # type: ignore[arg-type]
        temp_factory=_temp_factory(tmp_path),  # type: ignore[arg-type]
        wav_path=caller_wav,
    )

    with pytest.raises(OSError, match="pw-record 不存在"):
        session.start()

    if caller_wav is not None:
        assert caller_wav.exists()
    else:
        assert not list(tmp_path.glob("type4me-linux-raw-*.wav"))


class _FailingCloseStream(_TrackedStream):
    def close(self) -> None:
        raise OSError("管道关闭失败")


class _FailingProcess(_FakeProcess):
    def __init__(self, *, fail_at: str) -> None:
        stdout: _TrackedStream = _FailingCloseStream() if fail_at == "stream" else _TrackedStream()
        super().__init__(stdout, wait_times_out=fail_at in {"kill", "reap"})
        self.fail_at = fail_at

    def terminate(self) -> None:
        super().terminate()
        if self.fail_at == "terminate":
            raise OSError("终止失败")

    def kill(self) -> None:
        super().kill()
        if self.fail_at == "kill":
            raise OSError("强制终止失败")

    def wait(self, timeout: float | None = None) -> int:
        if self.fail_at == "reap" and self.wait_calls:
            self.wait_calls.append(timeout)
            raise OSError("回收失败")
        if self.fail_at == "wait":
            self.wait_calls.append(timeout)
            raise OSError("等待失败")
        return super().wait(timeout)


@pytest.mark.parametrize(
    ("fail_at", "message"),
    [
        ("stream", "管道关闭失败"),
        ("terminate", "终止失败"),
        ("wait", "等待失败"),
        ("kill", "强制终止失败"),
        ("reap", "回收失败"),
    ],
)
def test_shutdown_failures_are_reported_after_best_effort_cleanup(
    tmp_path: Path,
    fail_at: str,
    message: str,
) -> None:
    process = _FailingProcess(fail_at=fail_at)
    session, _ = _session(tmp_path, process, shutdown_timeout=0.01)
    session.start()
    wav_path = session.wav_path

    with pytest.raises(OSError, match=message):
        session.close()

    assert not wav_path.exists()
    assert process.terminated == 1
    if fail_at in {"kill", "reap"}:
        assert process.killed == 1


def test_context_body_exception_takes_precedence_over_cleanup_failure(
    tmp_path: Path,
) -> None:
    process = _FailingProcess(fail_at="terminate")
    session, _ = _session(tmp_path, process)

    with pytest.raises(ValueError, match="调用方失败"):
        with session:
            raise ValueError("调用方失败")


def test_batch_recorder_uses_configured_format_and_returns_owned_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    def run(argv: list[str], *, check: bool) -> None:
        calls.append((argv, check))

    monkeypatch.setattr(capture_module.subprocess, "run", run)
    recorder = Recorder(
        CaptureConfig(command="custom-recorder", sample_rate=8_000, channels=2, format="s24")
    )

    output = recorder.record_seconds(2.5)

    try:
        assert calls == [
            (
                [
                    "custom-recorder",
                    "--rate",
                    "8000",
                    "--channels",
                    "2",
                    "--format",
                    "s24",
                    "--duration",
                    "2.5",
                    str(output),
                ],
                True,
            )
        ]
        assert output.exists()
    finally:
        output.unlink(missing_ok=True)
