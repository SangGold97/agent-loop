#!/usr/bin/env bash
# Server, after the clone (and migrate.sh, if any): the platform venv and a user service that serves it.
# The service is enabled only after the CLI logins (see what this prints).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
make setup

# PATH must reach the CLIs (~/.local/bin), WSL's nvidia-smi and this shell's python3 (e.g. conda's), which
# workspace venvs are built from; a user service inherits none of them
py_dir=$(dirname "$(command -v python3)")
install -d ~/.config/systemd/user
cat > ~/.config/systemd/user/agentloop.service <<EOF
[Unit]
Description=agent-loop control plane and web UI

[Service]
WorkingDirectory=$ROOT
Environment=PATH=%h/.local/bin:$py_dir:/usr/lib/wsl/lib:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
${AGENTLOOP_HOME:+Environment=AGENTLOOP_HOME=$AGENTLOOP_HOME}
ExecStart=$ROOT/.venv/bin/agentloop serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

for cli in claude codex; do
    [ -e ~/.local/bin/$cli ] || command -v $cli >/dev/null || echo "WARNING: install the $cli CLI first (vendor docs)"
done
cat <<EOF
Next:
  claude                      # log in once (/login), then exit
  codex login --device-auth   # log in once (plain 'codex login' at this machine's own browser)
  $ROOT/.venv/bin/agentloop smoke
  systemctl --user enable --now agentloop
EOF
