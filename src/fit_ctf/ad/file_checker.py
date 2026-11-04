"""Three-method compatibility checker for existing file-backed competitions."""

import os
import tempfile
from pathlib import Path


class FileChecker:
    def __init__(self, context):
        self.path = Path(context["params"]["path"])

    def check_service(self) -> bool:
        # Legacy locations have no live service endpoint. New competitions should
        # supply their own checker that actually probes the running service.
        return True

    def put_flag(self, flag: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(flag + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_flag(self) -> str:
        return self.path.read_text(encoding="utf-8").strip()


def create_checker(context):
    return FileChecker(context)
