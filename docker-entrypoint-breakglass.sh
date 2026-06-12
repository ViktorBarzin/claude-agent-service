#!/bin/bash
# Entrypoint for the claude-breakglass deployment.
#
# Loads the breakglass SSH key into an in-pod ssh-agent (so neither the app nor
# the agent needs a key path, and the private key isn't passed around as a file
# after load), writes the `devvm`/`pve` SSH aliases the breakglass agent uses,
# then execs uvicorn. uvicorn — and the `claude` subprocesses it spawns —
# inherit SSH_AUTH_SOCK, so `ssh devvm` / `ssh pve <verb>` just work.
set -euo pipefail

HOME_DIR="${HOME:-/home/agent}"
SSH_DIR="$HOME_DIR/.ssh"
KEY_SRC="${BREAKGLASS_KEY_PATH:-/secrets/breakglass/private_key}"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

# SSH client config: the aliases the breakglass agent prompt refers to.
# Host-key checking off on purpose — a devvm rebuild rotates the host key and we
# must not get locked out mid-incident (trusted internal LAN; key auth stands).
cat > "$SSH_DIR/config" <<CFG
Host devvm
    HostName ${BREAKGLASS_DEVVM_HOST:-10.0.10.10}
    User ${BREAKGLASS_DEVVM_USER:-breakglass}
Host pve
    HostName ${BREAKGLASS_PVE_HOST:-192.168.1.127}
    User ${BREAKGLASS_PVE_USER:-root}
Host *
    BatchMode yes
    ConnectTimeout 10
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
CFG
chmod 600 "$SSH_DIR/config"

# Load the key into ssh-agent from a private tmpfs copy, then drop the copy
# (the agent keeps it in memory). The mounted secret is tmpfs, never disk.
if [[ -f "$KEY_SRC" ]]; then
    eval "$(ssh-agent -s)" >/dev/null
    TMP_KEY="$(mktemp /dev/shm/bgk.XXXXXX)"
    install -m600 "$KEY_SRC" "$TMP_KEY"
    ssh-add "$TMP_KEY" >/dev/null 2>&1 || echo "WARN: ssh-add failed" >&2
    shred -u "$TMP_KEY" 2>/dev/null || rm -f "$TMP_KEY"
    export SSH_AUTH_SOCK SSH_AGENT_PID
else
    echo "WARN: breakglass key not found at $KEY_SRC — SSH will not work" >&2
fi

exec python3 -m uvicorn app.breakglass.server:app \
    --host 0.0.0.0 --port 8080 --app-dir /srv
