from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from threading import RLock

from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Protocol
import wave

from .config import CaptureConfig


RAW_CHUNK_BYTES = 6_400
_SAMPLE_WIDTH_BYTES = 2


class _TemporaryFile(Protocol):
    name: str

    def close(self) -> None: ...


class RawCaptureSession:
    def __init__(
        self,
        config: CaptureConfig,
        *,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        temp_factory: Callable[..., _TemporaryFile] = tempfile.NamedTemporaryFile,
        wav_path: Path | None = None,
        shutdown_timeout: float = 1.0,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory
        self._temp_factory = temp_factory
        self._caller_wav_path = Path(wav_path) if wav_path is not None else None
        self._shutdown_timeout = shutdown_timeout
        self._lock = RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._wave: wave.Wave_write | None = None
        self._wav_path: Path | None = None
        self._owns_wav = wav_path is None
        self._claimed = False
        self._started = False
        self._closed = False
        self._iteration_active = False
        self._close_requested = False
        self._terminate_sent = False
        self._terminate_failure: BaseException | None = None

    @property
    def wav_path(self) -> Path:
        if self._wav_path is None:
            raise RuntimeError("录音尚未开始")
        return self._wav_path

    def __enter__(self) -> RawCaptureSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close(suppress_errors=exc_type is not None)

    def __iter__(self) -> Iterator[bytes]:
        return self.iter_chunks()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("录音会话已关闭")

            if self._caller_wav_path is None:
                temporary = self._temp_factory(
                    prefix="type4me-linux-raw-", suffix=".wav", delete=False
                )
                self._wav_path = Path(temporary.name)
                temporary.close()
            else:
                self._wav_path = self._caller_wav_path

            try:
                wav_handle = wave.open(str(self._wav_path), "wb")
                self._wave = wav_handle
                wav_handle.setnchannels(1)
                wav_handle.setsampwidth(_SAMPLE_WIDTH_BYTES)
                wav_handle.setframerate(16_000)
                self._process = self._popen_factory(
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
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except BaseException:
                self._close(suppress_errors=True)
                raise
            self._started = True

    def iter_chunks(self) -> Iterator[bytes]:
        with self._lock:
            if not self._started:
                raise RuntimeError("录音尚未开始")
            if self._closed:
                raise RuntimeError("录音会话已关闭")
            if self._iteration_active:
                raise RuntimeError("录音流已在消费中")
            process = self._process
            wav_handle = self._wave
            if process is None or process.stdout is None or wav_handle is None:
                raise RuntimeError("录音进程输出不可用")
            self._iteration_active = True

        pending = bytearray()
        try:
            while True:
                data = process.stdout.read(RAW_CHUNK_BYTES - len(pending))
                if not data:
                    return
                wav_handle.writeframesraw(data)
                pending.extend(data)
                if len(pending) == RAW_CHUNK_BYTES:
                    yield bytes(pending)
                    pending.clear()
        finally:
            with self._lock:
                self._iteration_active = False
                close_requested = self._close_requested
            if close_requested:
                self._close(suppress_errors=False)

    def claim_wav(self) -> Path:
        with self._lock:
            path = self.wav_path
            if self._closed and not path.exists():
                raise RuntimeError("录音文件已清理")
            self._claimed = True
            return path

    def release_wav(self) -> None:
        with self._lock:
            self._claimed = False
            remove = self._closed and self._owns_wav and self._wav_path is not None
            wav_path = self._wav_path
        if remove and wav_path is not None:
            wav_path.unlink(missing_ok=True)

    def request_stop(self) -> None:
        """请求录音进程退出，但由流消费者负责关闭读取管道。"""

        failure = self._signal_process()
        if failure is not None:
            raise failure

    def stop(self) -> Path:
        path = self.claim_wav()
        self.close()
        return path

    def cancel(self) -> None:
        self.close()

    def close(self) -> None:
        self._close(suppress_errors=False)

    def _close(self, *, suppress_errors: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._close_requested = True
            if self._iteration_active:
                deferred = True
                process = None
            else:
                deferred = False
                self._closed = True
                process = self._process

        if deferred:
            failure = self._signal_process()
            if failure is not None and not suppress_errors:
                raise failure
            return

        failure = self._signal_process()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        failure = failure or error
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except BaseException as error:
                    failure = failure or error
                try:
                    process.wait()
                except BaseException as error:
                    failure = failure or error
            except BaseException as error:
                failure = failure or error

        if self._wave is not None:
            try:
                self._wave.close()
            except BaseException as error:
                failure = failure or error

        with self._lock:
            remove = self._owns_wav and not self._claimed and self._wav_path is not None
            wav_path = self._wav_path
        if remove and wav_path is not None:
            try:
                wav_path.unlink(missing_ok=True)
            except BaseException as error:
                failure = failure or error

        if failure is not None and not suppress_errors:
            raise failure

    def _signal_process(self) -> BaseException | None:
        with self._lock:
            process = self._process
            if process is None:
                return None
            if self._terminate_sent:
                return self._terminate_failure
            self._terminate_sent = True
        try:
            process.terminate()
        except BaseException as error:
            with self._lock:
                self._terminate_failure = error
            return error
        return None


class Recorder:
    def __init__(self, config: CaptureConfig) -> None:
        self.config = config

    def record_seconds(self, seconds: float) -> Path:
        handle = tempfile.NamedTemporaryFile(prefix="type4me-linux-", suffix=".wav", delete=False)
        output = Path(handle.name)
        handle.close()
        subprocess.run(
            [
                self.config.command,
                "--rate",
                str(self.config.sample_rate),
                "--channels",
                str(self.config.channels),
                "--format",
                self.config.format,
                "--duration",
                str(seconds),
                str(output),
            ],
            check=True,
        )
        return output
