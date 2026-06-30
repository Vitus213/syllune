from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .config import CaptureConfig


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

