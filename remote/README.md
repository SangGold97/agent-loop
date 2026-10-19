# Remote mode

Run the platform on a stronger machine (the **server**, e.g. a workstation with a GPU) and use its web UI from
another one (the **client**) through Tailscale and an SSH tunnel. The platform never uses this directory. On a
single machine, ignore it and use `make serve`. Both modes can live side by side: the client's tunnel listens on
`127.0.0.1:18765`, which leaves `8765` free for a local platform.

Both machines run Ubuntu (native or WSL2) with systemd. To move an existing install, the server must use the same
user name and the same repo path as the client, because worktrees, hooks and venvs store absolute paths.

## Setup

1. **Server, Windows host only (WSL2)**: install the NVIDIA driver on Windows, not inside WSL. Then:
   - In `%UserProfile%\.wslconfig`, raise the RAM limit. WSL takes 50% by default, and the platform's RAM pool is
     sized from what WSL sees:
     ```ini
     [wsl2]
     memory=<about 85% of the RAM>GB
     vmIdleTimeout=-1
     ```
   - Keep WSL running without an open terminal (get the distro name from `wsl -l -v`):
     ```powershell
     $a = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu-24.04 --exec /bin/sleep infinity"
     Register-ScheduledTask -TaskName "WSL keepalive" -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
       -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero))
     ```
   - Turn off sleep in the Windows power settings, then run `wsl --shutdown`.
2. **Client**: join the tailnet with `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`.
   Then run `remote/client-setup.sh [server-name]` (default `agentloop-ws`). It adds the SSH aliases and the
   tunnel service, and prints the public key to give the server.
3. **Server**: clone this repo, at the client's path if you are moving an install. Then run
   `remote/server-bootstrap.sh "<client public key>" [server-name]`. It installs packages, sets up key-only SSH and
   joins the tailnet.
4. **Client, only to move an existing install**: stop the local server, then run `remote/migrate.sh [--skip-data]
   [server-name]`. It copies the workspaces, the state, the workspace session transcripts and the CLI builds, and
   rebuilds the workspace venvs there with CUDA torch. `--skip-data` leaves each workspace's `data/` empty there.
   An interrupted copy resumes when run again. A server that already has its own state is never overwritten.
5. **Server**: run `remote/server-finish.sh`, then the login and smoke commands it prints.

## Daily use

Nothing starts by itself on either side:
- **Server**: `make start` (or `make serve`) when the platform should run, and `make stop` when it should not
  (`make logs` follows the log); never both at once. Keep the server on the tailnet: after a `tailscale down` there, only
  someone at the server can bring it back.
- **Client**: `sudo tailscale up` (if you left the tailnet), then `systemctl --user start agentloop-tunnel` and open
  http://127.0.0.1:18765. `systemctl --user stop agentloop-tunnel` closes the tunnel, and `sudo tailscale down`
  leaves the tailnet. While the tunnel runs, it reconnects every 10 s whenever the server or the network drops.

After a migration, the client's own `workspaces/` and `state/` are stale: do not serve them again. To go back to
one machine, stop the client's tunnel and run `make serve` where the data is.
