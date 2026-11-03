"""Verify real engine events and stderr from a short-lived crashing container."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fit_ctf.ad.audit import AuditLog
from fit_ctf.ad.audit_worker import Collector

engine = sys.argv[1] if len(sys.argv) > 1 else "docker"
project = "audit_smoke_" + uuid.uuid4().hex[:8]
with tempfile.TemporaryDirectory(prefix="fitctf-audit-") as directory:
    log = AuditLog(directory)
    log.set_enabled("infrastructure", True)
    collector = Collector(log, project, engine)
    thread = threading.Thread(target=collector.run)
    thread.start()
    try:
        time.sleep(1)
        subprocess.run(
            [
                engine,
                "run",
                "-d",
                "--name",
                project,
                "--network",
                "none",
                "--label",
                f"fit_ctf.project={project}",
                "alpine:3.20",
                "sh",
                "-c",
                "echo simulated-infrastructure-failure >&2; exit 7",
            ],
            check=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            records = json.loads(log.path.read_text())
            crash = any(r.get("event") == "die" for r in records)
            stderr = any(r.get("message") == "simulated-infrastructure-failure" for r in records)
            if crash and stderr:
                break
            time.sleep(0.2)
        assert crash and stderr, records
        count = len(records)
        log.set_enabled("infrastructure", False)
        subprocess.run([engine, "start", project], check=True)
        time.sleep(1)
        assert len(json.loads(log.path.read_text())) == count
        print(
            json.dumps(
                {
                    "crash_event": crash,
                    "stderr_forwarded": stderr,
                    "disable_stopped_records": True,
                    "records": count,
                }
            )
        )
        if os.environ.get("FITCTF_AUDIT_EXAMPLE"):
            Path(os.environ["FITCTF_AUDIT_EXAMPLE"]).write_text(log.path.read_text())
    finally:
        collector.stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive()
        subprocess.run([engine, "rm", "-f", project], check=True)
