"""Initial file-based flag planting and checker adapters."""

import os
import tempfile
import time
from pathlib import Path


class FileFlagPlantingAdapter:
    def plant(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".fitctf-flag-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


class FileServiceChecker:
    def check(self, path: Path, expected: str) -> tuple[str, str, int]:
        started = time.monotonic()
        try:
            actual = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "down", "FLAG_FILE_MISSING", int((time.monotonic() - started) * 1000)
        except OSError as exc:
            return "checker_error", type(exc).__name__, int((time.monotonic() - started) * 1000)
        latency = int((time.monotonic() - started) * 1000)
        if actual != expected:
            return "corrupt", "FLAG_MISMATCH", latency
        return "up", "OK", latency
