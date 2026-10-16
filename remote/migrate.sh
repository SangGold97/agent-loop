#!/usr/bin/env bash
# Client, optional: move the live data of this install (workspaces, state, workspace session transcripts,
# workspace venv packages, the claude/codex CLI builds) to the server. The server's clone of this repo must sit
# at the same path, because worktrees, hooks and venvs store absolute paths.
# Usage: remote/migrate.sh [--skip-data] [server-name]
#   --skip-data   leave each workspace's data/ empty on the server, e.g. to download it again there
set -euo pipefail

SKIP_DATA=0
[ "${1:-}" = --skip-data ] && { SKIP_DATA=1; shift; }
HOST=${1:-agentloop-ws}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${AGENTLOOP_HOME:-$ROOT}

ssh "$HOST" true || { echo "cannot reach $HOST over SSH"; exit 1; }
ssh "$HOST" "test -d '$ROOT/.git'" || { echo "clone this repo to $ROOT on $HOST first"; exit 1; }
# Copy into a fresh server only. A server with its own state is never overwritten; an interrupted copy
# leaves .migrating behind and is resumed.
ssh "$HOST" "test ! -e '$DATA/state/agentloop.db' || test -e '$DATA/state/.migrating'" ||
    { echo "$HOST already has platform state; refusing to overwrite it"; exit 1; }
# A consistent copy needs both servers stopped (SQLite WAL, worktrees, in-flight inbox items);
# the [ ] keeps pgrep from matching the remote shell that runs it
if pgrep -u "$USER" -f 'agentloop[ ]serve' >/dev/null; then echo "stop the local server first"; exit 1; fi
ssh "$HOST" "! pgrep -u \$USER -f 'agentloop[ ]serve' >/dev/null" || { echo "stop the server on $HOST first"; exit 1; }
ssh "$HOST" "mkdir -p '$DATA/state' && touch '$DATA/state/.migrating'"

sync() { rsync -a --partial --mkpath --info=progress2 "$@"; }

excludes=(--exclude=.venv/ --exclude=__pycache__/)
[ $SKIP_DATA = 1 ] && excludes+=(--exclude='/*/data/*')
sync "${excludes[@]}" "$DATA/workspaces/" "$HOST:$DATA/workspaces/"
sync "$DATA/state/" "$HOST:$DATA/state/"

# Claude transcripts of workspace sessions, needed to --resume them; Codex keeps its own in each workspace
enc=$(printf %s "$DATA/workspaces" | sed 's/[^A-Za-z0-9]/-/g')
if compgen -G "$HOME/.claude/projects/$enc-*" >/dev/null; then
    sync --relative ~/.claude/projects/./"$enc"-* "$HOST":.claude/projects/
fi

# Workspace venvs are rebuilt there from their package lists: the base Python may differ, and torch comes from
# PyPI, whose Linux wheels are CUDA builds. Packages installed from a path or URL are left out.
for venv in "$DATA"/workspaces/*/.venv; do
    reqs=$("$venv/bin/python" -m pip freeze 2>/dev/null | grep -v -e ' @ ' -e '^-e ' | sed 's/+cpu$//') || true
    [ -n "$reqs" ] || continue
    printf '%s\n' "$reqs" | ssh "$HOST" "python3 -m venv '$venv' && '$venv/bin/pip' install -q -r /dev/stdin" ||
        echo "WARNING: rebuilding $venv on $HOST failed; the agents can install what they need"
done

# The native CLI builds in use, unless the server has its own; both auto-update from there. Logins are made
# afresh on the server: sharing rotating refresh tokens between two machines logs one of them out.
# (Each path is rooted at "$HOME/./", so rsync recreates it under the remote home.)
if [ -L ~/.local/bin/claude ] && ssh "$HOST" "test ! -e ~/.local/bin/claude"; then
    v=$(readlink ~/.local/bin/claude)
    sync --relative "${v/#$HOME/$HOME/.}" ~/./.local/bin/claude "$HOST":
fi
if [ -L ~/.codex/packages/standalone/current ] && ssh "$HOST" "test ! -e ~/.local/bin/codex"; then
    v=$(readlink ~/.codex/packages/standalone/current)
    sync --relative "${v/#$HOME/$HOME/.}" ~/./.codex/packages/standalone/current \
        ~/./.codex/packages/standalone/auto-update-version ~/./.local/bin/codex "$HOST":
fi
sync --ignore-existing ~/.gitconfig "$HOST":

ssh "$HOST" "rm '$DATA/state/.migrating'"
echo "migrated; next, on $HOST: $ROOT/remote/server-finish.sh"
