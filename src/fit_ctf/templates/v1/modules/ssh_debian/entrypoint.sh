#!/usr/bin/env bash
set -e

# Start sshd
if [ -f /run/fitctf-audit-session.py ]; then
    exec /usr/sbin/sshd -D -o "ForceCommand=/usr/bin/python3 /run/fitctf-audit-session.py"
fi
exec /usr/sbin/sshd -D
