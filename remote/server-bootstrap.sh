#!/usr/bin/env bash
# Server, once: packages, key-only SSH, linger and Tailscale, so the client can reach it.
# Usage: remote/server-bootstrap.sh "<client public key>" [server-name]
set -euo pipefail

PUBKEY=${1:?usage: $0 \"<client public key>\" [server-name]}
TS_NAME=${2:-agentloop-ws}

# The runner starts every turn with systemd-run --user, so systemd must be PID 1
[ "$(ps -p 1 -o comm=)" = systemd ] ||
    { echo "systemd is not PID 1 (WSL: add '[boot] systemd=true' to /etc/wsl.conf, then 'wsl --shutdown')"; exit 1; }

sudo apt-get update -q
sudo apt-get install -y -q git make rsync python3-venv python3-pip openssh-server curl

# SSH by key only; on WSL2 it is reachable through the tailnet alone, never through the Windows host
sudo tee /etc/ssh/sshd_config.d/agentloop.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sudo systemctl enable --now ssh
sudo systemctl restart ssh
install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF "$PUBKEY" ~/.ssh/authorized_keys || echo "$PUBKEY" >> ~/.ssh/authorized_keys

# Keep user services (the platform) running without an open login session
sudo loginctl enable-linger "$USER"

command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname="$TS_NAME"

nvidia-smi --query-gpu=name,memory.total --format=csv ||
    echo "WARNING: nvidia-smi failed (WSL: install the NVIDIA driver on Windows, not inside WSL)"
echo "bootstrap done: this machine is '$TS_NAME' on the tailnet"
