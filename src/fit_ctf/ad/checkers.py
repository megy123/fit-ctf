"""Author-defined flag locations and isolated three-method checker clients."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TickSettings(StrictConfig):
    seconds: int = Field(gt=0)
    count: int = Field(gt=0)
    flag_ttl_ticks: int = Field(default=1, gt=0)
    checker_timeout_seconds: float = Field(default=10, gt=0, le=300)
    concurrency: int = Field(default=8, gt=0, le=128)


class CheckerSettings(StrictConfig):
    entrypoint: str
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_entrypoint(self):
        path, separator, factory = self.entrypoint.rpartition(":")
        if not separator or not path.endswith(".py") or not factory.isidentifier():
            raise ValueError(
                "Checker entrypoint must be a Python file and factory: checker.py:create_checker"
            )
        return self


class FlagSettings(StrictConfig):
    cluster: str
    node: str
    service: str
    checker: CheckerSettings
    ticks: list[int] | None = None
    scoring_weight: float = Field(default=1, gt=0)


class FlagCatalogue(StrictConfig):
    version: Literal[2]
    ticks: TickSettings
    flags: dict[str, FlagSettings] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_schedule(self):
        import re

        for key, flag in self.flags.items():
            if re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
                raise ValueError(f"Invalid flag key: {key}")
            if flag.ticks is not None and (
                not flag.ticks
                or len(flag.ticks) != len(set(flag.ticks))
                or any(t < 0 or t >= self.ticks.count for t in flag.ticks)
            ):
                raise ValueError(f"Flag {key}: ticks must be unique indices from 0 to count - 1")
        return self


class CheckerClient:
    def __init__(self, entrypoint: str, context: dict, timeout: float):
        self.entrypoint, self.context, self.timeout = entrypoint, context, timeout

    def call(self, operation: str, flag: str | None = None) -> dict:
        request = {
            "entrypoint": self.entrypoint,
            "context": self.context,
            "operation": operation,
            "flag": flag,
        }
        payload = json.dumps(request)
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {
                "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("checker_runner.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            **kwargs,
        )
        try:
            output, _ = process.communicate(payload, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill()
            process.communicate()
            return {"status": "checker_error", "code": "CHECKER_TIMEOUT"}
        try:
            result = json.loads(output)
            if process.returncode or not isinstance(result, dict) or "status" not in result:
                raise ValueError()
            return result
        except ValueError:
            return {"status": "checker_error", "code": "CHECKER_PROCESS_FAILED"}


def resolve_entrypoint(entrypoint: str, base: Path) -> str:
    filename, factory = entrypoint.rsplit(":", 1)
    path = Path(filename)
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Checker file does not exist: {path}")
    return f"{path}:{factory}"
