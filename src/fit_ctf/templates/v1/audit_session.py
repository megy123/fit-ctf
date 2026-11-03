"""SSH ForceCommand: relay a PTY and send all terminal input to the host collector."""

import base64
import errno
import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import tty
import uuid


def main(
    socket_path="/run/fitctf-audit/input.sock",
    control_path="/run/fitctf-audit-control/logging.json",
):
    session = str(uuid.uuid4())
    connection = None

    def record(data, retry=True):
        nonlocal connection
        try:
            with open(control_path, encoding="utf-8") as file:
                enabled = json.load(file)["enabled"]["terminal"]
        except FileNotFoundError:
            enabled = False
        if not enabled:
            return
        try:
            if connection is None:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(5)
                connection.connect(socket_path)
            connection.sendall(
                json.dumps(
                    {"session": session, "input": base64.b64encode(data).decode("ascii")}
                ).encode()
                + b"\n"
            )
            reply = b""
            while len(reply) < 3:
                chunk = connection.recv(3 - len(reply))
                if not chunk:
                    raise OSError("Audit collector disconnected")
                reply += chunk
            if reply != b"OK\n":
                raise OSError("Audit collector did not acknowledge input")
        except OSError as exc:
            if connection:
                connection.close()
            connection = None
            if retry:
                return record(data, retry=False)
            raise RuntimeError("Terminal audit unavailable; input was not executed") from exc

    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    shell = os.environ.get("SHELL", "/bin/bash")
    if original:
        record(original.encode() + b"\n")
    if original and not os.isatty(sys.stdin.fileno()):
        if original == "internal-sftp":
            candidates = ["/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server"]
            argv = [next(path for path in candidates if os.path.isfile(path))]
        else:
            argv = [shell, "-c", original]
        child = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        readers = {
            sys.stdin.fileno(): child.stdin,
            child.stdout.fileno(): sys.stdout.buffer,
            child.stderr.fileno(): sys.stderr.buffer,
        }
        try:
            while readers:
                if child.poll() is not None and set(readers) <= {sys.stdin.fileno()}:
                    break
                ready, _, _ = select.select(list(readers), [], [], 0.1)
                for fd in ready:
                    data = os.read(fd, 4096)
                    if not data:
                        readers.pop(fd)
                        if fd == sys.stdin.fileno():
                            child.stdin.close()
                        continue
                    if fd == sys.stdin.fileno():
                        record(data)
                    try:
                        readers[fd].write(data)
                        readers[fd].flush()
                    except BrokenPipeError:
                        readers.pop(fd)
                        if fd == sys.stdin.fileno():
                            child.stdin.close()
            return child.wait()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait()
            if connection:
                connection.close()
    pid, master = pty.fork()
    if pid == 0:
        os.execv(shell, [shell, "-c", original] if original else [shell, "-l"])

    is_tty = os.isatty(sys.stdin.fileno())
    saved = termios.tcgetattr(sys.stdin.fileno()) if is_tty else None

    def resize(*_):
        if is_tty:
            size = fcntl.ioctl(
                sys.stdin.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
            )
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)

    signal.signal(signal.SIGWINCH, resize)
    resize()
    try:
        if is_tty:
            tty.setraw(sys.stdin.fileno())
        readers = [master, sys.stdin.fileno()]
        while master in readers:
            ready, _, _ = select.select(readers, [], [])
            for fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    readers.remove(fd)
                    if fd != master:
                        os.write(master, b"\x04")
                    continue
                if fd == master:
                    os.write(sys.stdout.fileno(), data)
                else:
                    record(data)
                    os.write(master, data)
    finally:
        if saved:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        os.close(master)
        if connection:
            connection.close()
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
