#!/usr/bin/env bash
# Client, once: SSH aliases for the server and a user service that keeps the web UI tunnel up.
# Usage: remote/client-setup.sh [server-name] [local-port]
set -euo pipefail

HOST=${1:-agentloop-ws}
PORT=${2:-18765}  # not 8765, so a platform on this machine can keep its default port
SERVER_USER=${SERVER_USER:-$USER}
KEY=~/.ssh/id_ed25519

install -d -m 700 ~/.ssh
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -f "$KEY"
# The tunnel service runs unattended, so it cannot type a passphrase
ssh-keygen -y -P "" -f "$KEY" >/dev/null 2>&1 || echo "WARNING: $KEY has a passphrase; the tunnel service needs an ssh-agent"

touch ~/.ssh/config
if ! grep -qx "Host $HOST" ~/.ssh/config; then
    cat >> ~/.ssh/config <<EOF
# agent-loop server over Tailscale (MagicDNS name set by server-bootstrap.sh)
Host $HOST
    HostName $HOST
    User $SERVER_USER
    IdentityFile $KEY
    StrictHostKeyChecking accept-new
    ServerAliveInterval 30
    ServerAliveCountMax 3

EOF
fi
if ! grep -qx "Host agentloop-ui" ~/.ssh/config; then
    cat >> ~/.ssh/config <<EOF
# Web UI tunnel only: http://127.0.0.1:$PORT here -> the server's 127.0.0.1:8765.
Host agentloop-ui
    HostName $HOST
    User $SERVER_USER
    IdentityFile $KEY
    StrictHostKeyChecking accept-new
    ServerAliveInterval 30
    ServerAliveCountMax 3
    SessionType none
    ExitOnForwardFailure yes
    LocalForward 127.0.0.1:$PORT 127.0.0.1:8765

EOF
fi

install -d ~/.config/systemd/user
cat > ~/.config/systemd/user/agentloop-tunnel.service <<EOF
[Unit]
Description=Tunnel to the agent-loop web UI on $HOST (http://127.0.0.1:$PORT)

[Service]
ExecStart=/usr/bin/ssh -o BatchMode=yes agentloop-ui
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

echo "Public key for server-bootstrap.sh:"
cat "$KEY.pub"
