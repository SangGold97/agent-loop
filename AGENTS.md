## Commands

```bash
make setup                                   # .venv + editable install with dev extras (pytest, httpx)
make serve                                   # control plane + web UI on http://127.0.0.1:8765
make test                                    # all tests; a scripted fake agent stands in for the CLIs (no API cost)
.venv/bin/python -m pytest -q tests/test_units.py::test_guard_rules   # a single test
.venv/bin/agentloop init <name> [--repo URL] # create a workspace; `agentloop smoke` runs one tiny turn per CLI
```

No linter or formatter is configured. The web UI (`agentloop/web/`) is vanilla JS served as is, with no build step.

## Architecture

agent-loop wraps the headless Claude Code, Codex and Antigravity (`agy`) CLIs. Per goal, a meta-work session plans, spawns worker
sessions, merges their work and closes the goal; the human steers from the web UI. README.md explains the concepts
(workspace, goal, meta-work, meta-chat, worker, digest) and the guardrails.

- **Control plane**: `server.py` (FastAPI) serves the UI and `/api/agent/<cmd>`, which the `loop` CLI (`cli.py`)
  calls from meta sessions using the `LOOP_API` / `LOOP_TOKEN` env vars. `engine.py` holds the logic; state is SQLite
  (`db.py`, under `state/`).
- **Event loop per goal**: everything for the meta-work (kickoff, worker digests, advice, notes, budget, system messages)
  goes into the goal's `inbox` table. `Engine.meta_loop` drains it into one meta-work turn; items stay in flight
  (`consumed=2`) until the turn ends and go back to the queue on crash or restart.
- **Advisor**: `loop advise` queues a question in the same inbox; the engine dispatches a new `meta-advisor`
  session for one turn and returns its final text as `[Advice]`. Consultations share `max_meta_turns` and are
  read-only like a meta-chat (guard, `loop` API, change report after the turn).
- **Worker turn**: runs in its own worktree on `loop/<goal>/<role>-<sid>`, is snapshot-committed, and its final
  reply becomes a digest (`digest.py`) pushed to the inbox. `loop merge` merges into
  `loop/<goal>/integration`, checked out at `wt/<goal>` (`gitops.py`). The meta-work may also change code
  there; `Engine.checkpoint` commits those changes before a spawn, merge or `loop done` and after each turn.
- **Running a CLI**: `runner.py` starts each turn in `systemd-run --user --scope` (anything left in the scope when
  the CLI exits is killed and reported as `TurnResult.leftover`), inside the workspace's shared `.venv`
  (`workspace.ensure_venv`) and with its own `TMPDIR` under `runner.scratch_root()` that `Engine.run_turn` removes
  when the turn ends. It streams JSON lines through an adapter (`adapters/claude.py`, `codex.py`, `agy.py`) that maps
  vendor events to the normalized events listed in `adapters/base.py`. A new CLI means a new `Adapter` registered
  in `ADAPTERS`.
- **Guardrails**: `guard.py` is the PreToolUse hook for every CLI, wired through `templates/workspace/*/hooks.json`
  (`{{PY}}` is replaced by the venv Python); agy, which has no per-session settings, gets it from the plugin
  `workspace.agy_plugin` writes to `~/.gemini/config/plugins/agentloop/`. Claude gets it through `workspace.claude_settings`, which rebuilds
  `.loop/claude/<layer>-settings.json` each turn from `hooks.json` plus `claudeMdExcludes`, so agents never read
  instruction files above their own directory. After every turn `workspace.enforce_protected` reverts edits to
  human-owned files.
- **Config layering** (`config.py`): `DEFAULTS` < workspace `loop.yaml` < goal `goal.yaml` (only `GOAL_KEYS`).
- **Prompts sent to agents** live in code as well as templates: `Engine.kickoff_text`, `mode_text`,
  `language_rule`, the inbox messages pushed in `engine.py` (`[System]`, `[Budget exhausted]`, `[Handoff]`),
  `digest.envelope`, and the `loop` help text in `cli.py`.

### Templates and live data

- `templates/workspace/` is the harness copied into new workspaces (meta/worker `AGENTS.md`, hooks, skills). On
  server startup `workspace.sync_harness` pushes template changes into every existing workspace unless the human
  edited that file (hashes in `.loop/template.json`). Editing a template therefore changes live agents after the
  next restart.
- `workspaces/` and `state/` are live, gitignored data under `AGENTLOOP_HOME` (default: the repo root). Each workspace
  is its own git repo where every commit names its actor. Leave them alone when changing the platform.

### Tests

`tests/conftest.py` points `AGENTLOOP_HOME` at a temp dir, starts a real server on a free port and registers a
`fake` adapter that runs `tests/fake_agent.py`. That script emits Claude stream-json and drives the platform through
`loop`, so changes to the meta/worker protocol usually need a matching change there.

## Coding rules

- Follow PEP 8 for Python, with lines up to about 120 characters as in the existing code. Before any block with
  substantial logic, add a short comment saying why it exists or what it guarantees, like the comments in
  `engine.py`, so the next reader does not have to reconstruct the intent.
- Write code, comments, identifiers and commit messages in English, matching the repo.
- Before finishing, delete the temporary files, scratch scripts and debug output you created, so the diff holds only
  what the change needs.
- Change only what the user asked for and keep the behaviour of other modules as it is. If the request seems to
  need a change elsewhere, say so and ask first. Template and prompt-text changes count as behaviour changes because
  they alter what live agents do.

## Editing content

- When information changes (README, templates, docstrings, comments, this file), rewrite it so only the current
  truth remains. Remove outdated statements instead of leaving them next to the new ones.
- Keep replies, code and generated content concise, and say each thing once.

## Answers

- Provide concise answers and easy to understand inline chat in Vietnamese, clear explanations, and get straight to the point.
