#!/usr/bin/env bash
set -e

# When Podman attaches the shared A&D network after container creation, it
# does not rewrite resolv.conf. Prefer that network's DNS service so players
# can resolve the team-service aliases from the login node.
if command -v ip >/dev/null 2>&1; then
    shared_dns="$(ip -4 route show default dev eth1 2>/dev/null | awk 'NR == 1 {print $3}')"
    if [ -n "${shared_dns:-}" ]; then
        printf 'nameserver %s\n' "$shared_dns" > /etc/resolv.conf
    fi
fi

# Start sshd
if [ -f /run/fitctf-audit-session.py ]; then
    exec /usr/sbin/sshd -D -o "ForceCommand=/usr/bin/python3 /run/fitctf-audit-session.py"
fi
exec /usr/sbin/sshd -D
