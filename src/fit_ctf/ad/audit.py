"""Project audit switches and an atomic, process-safe JSON audit file."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = ("terminal", "infrastructure", "flags")


@contextmanager
def file_lock(path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if os.name == "nt":
            import msvcrt

            file.seek(0)
            if not file.read(1):
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".audit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class AuditLog:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "log.json"
        self.control = self.root / "control" / "logging.json"
        self.lock = self.root / ".audit.lock"

    def _settings(self):
        if self.control.exists():
            return json.loads(self.control.read_text(encoding="utf-8"))
        return {"enabled": dict.fromkeys(CATEGORIES, False), "users": {}}

    def settings(self):
        with file_lock(self.lock):
            return self._settings()

    def set_enabled(self, category, enabled):
        if category not in CATEGORIES:
            raise ValueError(f"Unknown audit category: {category}")
        with file_lock(self.lock):
            settings = self._settings()
            settings["enabled"][category] = enabled
            settings.setdefault("enabled_since", {})[category] = datetime.now(
                timezone.utc
            ).isoformat()
            settings["revision"] = uuid.uuid4().hex
            atomic_json(self.control, settings)
            self.control.chmod(0o644)
            if not self.path.exists():
                atomic_json(self.path, [])

    def terminal_directory(self, username):
        key = hashlib.sha256(username.encode()).hexdigest()[:20]
        directory = self.root / "terminal" / key
        directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock):
            settings = self._settings()
            settings["users"][key] = username
            atomic_json(self.control, settings)
            self.control.chmod(0o644)
        return directory

    def disable_all(self):
        with file_lock(self.lock):
            settings = self._settings()
            settings["enabled"] = dict.fromkeys(CATEGORIES, False)
            settings["revision"] = uuid.uuid4().hex
            atomic_json(self.control, settings)
            self.control.chmod(0o644)

    def write(self, category, **fields):
        if category not in CATEGORIES:
            raise ValueError(f"Unknown audit category: {category}")
        with file_lock(self.lock):
            if not self._settings()["enabled"][category]:
                return False
            entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "type": category}
            entry.update({k: v for k, v in fields.items() if k not in {"timestamp", "type"}})
            records = (
                json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
            )
            records.append(entry)
            atomic_json(self.path, records)
            return True

    def terminal_input(self, user, session, data):
        return self.write(
            "terminal",
            user=user,
            session=session,
            event="command",
            command=data.decode("utf-8", errors="replace").rstrip("\r\n"),
            input_base64=base64.b64encode(data).decode("ascii"),
        )


class AuditManager:
    def __init__(self, app):
        self.app = app

    def log(self, project):
        return AuditLog(self.app.paths.project_logs(project))

    def configure(self, project, category, enabled):
        log = self.log(project)
        log.set_enabled(category, enabled)
        settings = log.settings()["enabled"]
        if settings["terminal"] or settings["infrastructure"]:
            self.start_collector(project, log)
        return log

    def start_collector(self, project, log):
        revision = log.settings()["revision"]
        engine = "podman" if "podman" in type(self.app.c_client).__name__.lower() else "docker"
        stderr = (log.root / "collector.log").open("ab")
        try:
            kwargs = (
                {"start_new_session": True}
                if os.name != "nt"
                else {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
            )
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "fit_ctf.ad.audit_worker",
                    "--logs",
                    str(log.root.resolve()),
                    "--project",
                    project.name,
                    "--engine",
                    engine,
                ],
                stdin=subprocess.DEVNULL,
                stdout=stderr,
                stderr=stderr,
                **kwargs,
            )
        finally:
            stderr.close()
        deadline = time.monotonic() + 15
        ready = log.root / "collector-ready.json"
        while time.monotonic() < deadline:
            if ready.exists():
                try:
                    if json.loads(ready.read_text())["revision"] == revision:
                        return
                except (ValueError, KeyError):
                    pass
            time.sleep(0.05)
        raise RuntimeError(f"Audit collector did not start; see {log.root / 'collector.log'}")

    def infrastructure(self, project_name, message, **fields):
        project = self.app.prj_mgr.get_doc_by_filter(name=project_name, active=True)
        if project:
            self.log(project).write("infrastructure", message=message, **fields)

    def submission(self, project, attacker, value, result, *, user=None, source="api", **fields):
        team = self.app.team_mgr.get_doc_by_id(attacker.team_id.id) if attacker else None
        self.log(project).write(
            "flags",
            team=team.name if team else None,
            user=user,
            flag=value,
            result=result,
            source=source,
            user_verified=False,
            **fields,
        )
