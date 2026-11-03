"""Verify recorder mounts and command capture as a non-root container user."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fit_ctf.ad.audit import AuditLog
from fit_ctf.ad.audit_worker import Collector

engine = sys.argv[1] if len(sys.argv) > 1 else "docker"
script = Path(__file__).resolve().parents[2] / "src/fit_ctf/templates/v1/audit_session.py"
with tempfile.TemporaryDirectory(prefix="fitctf-terminal-") as directory:
    log = AuditLog(directory)
    log.set_enabled("terminal", True)
    socket_dir = log.terminal_directory("alice")
    collector = Collector(log, "terminal_smoke", engine)
    collector.register_sockets(log.settings())
    try:
        process = subprocess.run(
            [
                engine,
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--user",
                "1000:1000",
                "-e",
                "SHELL=/bin/sh",
                "-e",
                "HOME=/tmp",
                "-v",
                f"{socket_dir}:/run/fitctf-audit:ro",
                "-v",
                f"{log.control.parent}:/run/fitctf-audit-control:ro",
                "-v",
                f"{script}:/run/fitctf-audit-session.py:ro",
                "python:3.12-alpine",
                "python",
                "/run/fitctf-audit-session.py",
            ],
            input=b"echo in-container\nsh\necho nested-container\nexit\nexit\n",
            capture_output=True,
            timeout=30,
        )
        assert process.returncode == 0, process.stderr
        records = json.loads(log.path.read_text())
        commands = [r["command"] for r in records if r.get("event") == "command"]
        assert "echo in-container" in commands and "echo nested-container" in commands, records
        assert all(r["user"] == "alice" for r in records)
        assert os.stat(log.path).st_mode & 0o777 == 0o600
        print(
            json.dumps(
                {
                    "non_root_readonly_socket": True,
                    "nested_commands_recorded": True,
                    "command_records": len(commands),
                }
            )
        )
    finally:
        collector.stop.set()
        for server in collector.sockets.values():
            server.close()
