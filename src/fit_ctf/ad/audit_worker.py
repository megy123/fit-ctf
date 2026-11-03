"""Detached collector for engine events, container stderr/stdout and SSH input sockets."""

import argparse
import base64
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from fit_ctf.ad.audit import AuditLog, atomic_json, file_lock


class Collector:
    def __init__(self, log, project, engine):
        self.log, self.project, self.engine = log, project, engine
        self.messages = queue.Queue()
        self.processes = {}
        self.sockets = {}
        self.attached = set()
        self.ready_revision = object()
        self.stop = threading.Event()
        self.last_event = datetime.now(timezone.utc).isoformat()

    def belongs(self, info):
        labels = info.get("Config", {}).get("Labels", {}) or {}
        name = info.get("Name", "").lstrip("/")
        return labels.get("fit_ctf.project") == self.project or name.startswith(self.project + "_")

    def spawn(self, key, args):
        if key in self.processes and self.processes[key].poll() is None:
            return
        try:
            process = subprocess.Popen(
                [self.engine, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            self.log.write("infrastructure", message=str(exc), event="collector_error")
            return
        self.processes[key] = process

        def read(stream, kind):
            for line in iter(stream.readline, b""):
                self.messages.put(
                    (key, kind, line.decode("utf-8", errors="replace").rstrip("\r\n"))
                )
            stream.close()

        for stream, kind in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            threading.Thread(target=read, args=(stream, kind), daemon=True).start()

    def discover(self):
        result = subprocess.run(
            [self.engine, "ps", "-aq"], capture_output=True, text=True, timeout=10
        )
        if result.returncode:
            self.log.write("infrastructure", message=result.stderr.strip(), event="collector_error")
            return
        ids = result.stdout.split()
        if not ids:
            return
        result = subprocess.run(
            [self.engine, "inspect", *ids], capture_output=True, text=True, timeout=10
        )
        if result.returncode:
            self.log.write("infrastructure", message=result.stderr.strip(), event="collector_error")
            return
        for info in json.loads(result.stdout):
            if self.belongs(info):
                identifier = info.get("Id", info.get("ID"))
                started = info.get("State", {}).get("StartedAt", "")
                key = (identifier, started)
                if key in self.attached:
                    continue
                self.attached.add(key)
                # Do not replay the entire historical container output on each reconnect.
                self.spawn(key, ["logs", "--follow", "--since", self.last_event, identifier])

    def consume(self):
        while True:
            try:
                key, stream, line = self.messages.get_nowait()
            except queue.Empty:
                return
            if key == "events" and stream == "stdout":
                try:
                    event = json.loads(line)
                except ValueError:
                    self.log.write("infrastructure", message=line, event="collector_error")
                    continue
                attrs = event.get("Actor", {}).get("Attributes", event.get("Attributes", {})) or {}
                name = attrs.get("name", event.get("Name", ""))
                if attrs.get("fit_ctf.project") != self.project and not name.startswith(
                    self.project + "_"
                ):
                    continue
                self.log.write(
                    "infrastructure",
                    message=line,
                    event=event.get("Action", event.get("Status")),
                    container=event.get("id", event.get("ID")),
                )
            else:
                container = key[0] if isinstance(key, tuple) else key
                self.log.write("infrastructure", message=line, container=container, stream=stream)

    def terminal_connection(self, connection, username):
        pending = bytearray()
        session = None
        try:
            with connection.makefile("rb") as file:
                for raw in iter(lambda: file.readline(128 * 1024 + 1), b""):
                    if len(raw) > 128 * 1024:
                        break
                    try:
                        record = json.loads(raw)
                        session = str(record["session"])
                        data = base64.b64decode(record["input"], validate=True)
                    except (ValueError, KeyError, TypeError):
                        break
                    if not self.log.settings()["enabled"]["terminal"]:
                        pending.clear()
                        connection.sendall(b"OK\n")
                        continue
                    self.log.write(
                        "terminal",
                        user=username,
                        session=session,
                        event="input",
                        input=data.decode("utf-8", errors="replace"),
                        input_base64=record["input"],
                    )
                    for byte in data:
                        pending.append(byte)
                        if byte in (10, 13, 3, 4) or len(pending) >= 65536:
                            self.log.terminal_input(username, session, bytes(pending))
                            pending.clear()
                    connection.sendall(b"OK\n")
        finally:
            if pending:
                self.log.terminal_input(username, session, bytes(pending))
            connection.close()

    def register_sockets(self, settings):
        if not hasattr(socket, "AF_UNIX"):
            return
        for key, username in settings["users"].items():
            if key in self.sockets:
                continue
            path = self.log.root / "terminal" / key / "input.sock"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Linux sockaddr_un paths are limited to 108 bytes. Bind through a short
            # directory descriptor path; containers see the regular mounted path.
            if sys.platform.startswith("linux"):
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    server.bind(f"/proc/self/fd/{fd}/input.sock")
                finally:
                    os.close(fd)
            else:
                server.bind(str(path))
            os.chmod(path, 0o666)
            server.listen()
            server.settimeout(0.5)
            self.sockets[key] = server

            def accept(listener=server, user=username):
                while not self.stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    threading.Thread(
                        target=self.terminal_connection, args=(connection, user), daemon=True
                    ).start()

            threading.Thread(target=accept, daemon=True).start()

    def close_processes(self):
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.processes.clear()

    def run(self):
        infrastructure = False
        discovery = 0
        try:
            while not self.stop.is_set():
                settings = self.log.settings()
                if not any(settings["enabled"][c] for c in ("terminal", "infrastructure")):
                    break
                self.register_sockets(settings)
                if self.ready_revision != settings.get("revision"):
                    atomic_json(
                        self.log.root / "collector-ready.json",
                        {"revision": settings.get("revision"), "pid": os.getpid()},
                    )
                    self.ready_revision = settings.get("revision")
                enabled = settings["enabled"]["infrastructure"]
                if enabled:
                    if not infrastructure:
                        self.last_event = settings.get("enabled_since", {}).get(
                            "infrastructure", datetime.now(timezone.utc).isoformat()
                        )
                    self.spawn(
                        "events", ["events", "--since", self.last_event, "--format", "{{json .}}"]
                    )
                    if time.monotonic() >= discovery:
                        try:
                            self.discover()
                        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                            self.log.write(
                                "infrastructure", message=str(exc), event="collector_error"
                            )
                        discovery = time.monotonic() + 2
                elif infrastructure:
                    self.close_processes()
                    self.attached.clear()
                infrastructure = enabled
                self.consume()
                self.stop.wait(0.1)
        finally:
            self.close_processes()
            self.consume()
            for server in self.sockets.values():
                server.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--engine", choices=["docker", "podman"], required=True)
    args = parser.parse_args()
    log = AuditLog(args.logs)
    try:
        with file_lock(log.root / ".collector.lock", blocking=False):
            collector = Collector(log, args.project, args.engine)
            signal.signal(signal.SIGTERM, lambda *_: collector.stop.set())
            signal.signal(signal.SIGINT, lambda *_: collector.stop.set())
            collector.run()
    except BlockingIOError:
        return  # Another collector already owns this project.


if __name__ == "__main__":
    main()
