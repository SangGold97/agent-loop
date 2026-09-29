# agent-loop

A local, human-on-the-loop platform that wraps **Claude Code** and **Codex** (headless CLIs).
You write a **spec** and a **Definition of Done**; a **meta-agent** (orchestrator) plans, writes prompts for
**worker** sessions, evaluates their evidence against the DoD, and keeps iterating until it decides the mission is
done. You steer with notes, chat with an advisor meta-agent, review the report, and merge.

```
Human ──► UI (missions, live sessions, notes, advisor chat, infra)
            │
     control plane (FastAPI + SQLite)  ── budgets · guard · git · digests · infra caps
            │
   orchestrator (meta) ── `loop spawn / send / merge / done` ──► workers (Claude Code | Codex)
                                                                  one git worktree + branch each
```

## Setup

```bash
make setup          # venv + editable install (needs git, and logged-in `claude` / `codex` CLIs)
make serve          # http://127.0.0.1:8765
make test           # unit + end-to-end tests (scripted fake agent, no API cost)
```

Requirements: Linux/WSL with systemd (for per-worker memory caps), Python ≥ 3.10.
The platform uses your existing CLI logins; it never asks for keys.

## Concepts

| | |
|---|---|
| **Workspace** (`workspaces/<name>/`, own git) | One project: `repo/` (the project, cloned or empty), `meta/` + `worker/` harnesses, `missions/`, `data/`, `wt/` worktrees. Every change is committed with its actor (`human`, `meta(<sid>)`, `control-plane`, `advisor(<sid>)`). |
| **Mission** (`missions/<date>-<slug>/`) | `spec.md`, `dod.md`, `mission.yaml` (human-owned), `plan.md` + `journal.md` (orchestrator), `notes.md`, `report.md`, `runs/<worker>/` evidence. |
| **Orchestrator** | One meta session per mission, driven by events (kickoff, worker finished, note, DoD changed, budget). It never executes project work. |
| **Advisor** | Prompt-by-prompt chat bound to a mission; read-only; proposes notes you approve. |
| **Worker** | A Claude Code or Codex session per role (`mission.yaml` → `roles`), in its own worktree on `loop/<mission>/<label>-<sid>`; accepted work is merged into `loop/<mission>/integration`. |
| **Digest** | What a finished worker turn sends to the orchestrator: final report + diff, trimmed per `loop.yaml → digest`, full versions in `runs/`. |
| **DoD** | Free text. Lines `- verify: \`cmd\`` are run on the integration branch before a `success` close; everything else is judged by the orchestrator. |

## Guardrails (mistakes, not adversaries)

- One PreToolUse guard (`agentloop/guard.py`) serves both CLIs: no `git push`, no writes to human-owned files,
  meta-agents read-only on code, workers confined to their worktree/run dir/`data/`, extra regexes from `loop.yaml → guard`.
- Git hooks in the project repo block agent commits to protected branches and any agent push.
- After every agent turn the control plane reverts edits to `spec.md`, `dod.md`, `mission.yaml`, `loop.yaml`, `hooks.json`
  (the changed copy is kept in `.loop/reverted/<time>/`, and the UI shows an alert).
- Budgets (concurrency, worker/meta turns, hours, tokens) are enforced by the control plane; on exhaustion the
  orchestrator gets one final turn to close the mission.
- Every session runs in its own `systemd-run --user --scope` (orphans are stopped on restart); workers also get
  `MemoryMax`. Spawns are refused under `min_free_ram_gb`; GPUs are hidden unless a worker is spawned with `--gpu`
  (slots in `loop.yaml → infra`).
- Events handed to an orchestrator turn stay "in flight" until it completes; a crash, restart or stop puts them back.
- The API accepts writes only from the UI's own origin or non-browser clients. CLI updates (Agent CLIs page) are
  refused while sessions of that CLI are running, and each update is followed by a smoke turn.

## Customising per project

Edit the workspace harness from the UI (workspace page → Harness) or any editor:
`meta/AGENTS.md` (orchestrator/advisor behaviour), `worker/AGENTS.md`, `*/skills/<name>/SKILL.md` (shared by both CLIs),
`*/memory/`, `*/docs/`, `*/hooks.json`, optional `*/codex.toml` (extra Codex config). The project's own
`CLAUDE.md`/`AGENTS.md` in `repo/` still apply to workers.
