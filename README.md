# agent-loop

A local, human-on-the-loop platform that wraps **Claude Code** and **Codex** (headless CLIs).
You write a **spec** and a **goal**; a **meta-work** session (meta-agent) plans, writes prompts for
**worker** sessions, evaluates their evidence against the goal, and keeps iterating until it decides the goal is
done. You steer with notes, chat with a **meta-chat** session (meta-agent), review the report, and merge.

```
Human ──► UI (goals, live sessions, notes, meta-chat, infra)
            │
     control plane (FastAPI + SQLite)  ── budgets · guard · git · digests · infra caps
            │
   meta-work (meta-agent) ── `loop spawn / send / merge / done` ──► workers (Claude Code | Codex)
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
| **Workspace** (`workspaces/<name>/`, own git) | One project: `repo/` (the project, cloned or empty), `meta/` + `worker/` harnesses, `goals/`, `data/`, `wt/` worktrees. Every change is committed with its actor (`human`, `meta-work(<sid>)`, `control-plane`, `meta-chat(<sid>)`). |
| **Goal** (`goals/<date>-<slug>/`) | `spec.md`, `goal.md`, `goal.yaml` (human-owned), `plan.md` + `journal.md` (meta-work), `notes.md`, `report.md`, `runs/<worker>/` evidence. |
| **Meta-work** | One meta session per goal, driven by events (kickoff, worker finished, note, goal changed, budget). It never executes project work. |
| **Meta-chat** | Prompt-by-prompt chat bound to a goal (agent/model/effort chosen per chat); runs commands and tools freely, steers workers only through notes you approve. |
| **Worker** | A Claude Code or Codex session (the meta-work picks the CLI and role; model/effort come from `goal.yaml` → `workers`), in its own worktree on `loop/<goal>/<label>-<sid>`; accepted work is merged into `loop/<goal>/integration`. It receives the meta-work's prompt as is and a generic `worker/AGENTS.md`, nothing about the platform; `.venv/` and `.artifacts/` stay out of git, and `.artifacts/` is kept in `runs/<sid>/artifacts/`. |
| **Digest** | What a finished worker turn sends to the meta-work: final reply + diff, trimmed per `loop.yaml → digest`, full versions in `runs/`. The commands a worker ran are pulled on demand with `loop show W --log`. |
| **goal.md** | Success criteria, free text. Lines `- verify: \`cmd\`` are run on the integration branch before a `success` close; everything else is judged by the meta-work. |

## Guardrails (mistakes, not adversaries)

- One PreToolUse guard (`agentloop/guard.py`) serves both CLIs: no `git push`, no writes to human-owned files,
  workers confined to their worktree and `data/`, extra regexes from `loop.yaml → guard`.
- Git hooks in the project repo block agent commits to protected branches and any agent push.
- After every agent turn the control plane reverts edits to `spec.md`, `goal.md`, `goal.yaml`, `loop.yaml`, `hooks.json`
  (the changed copy is kept in `.loop/reverted/<time>/`, and the UI shows an alert).
- Budgets (concurrency, worker/meta turns) are enforced by the control plane; on exhaustion the
  meta-work gets one final turn to close the goal.
- Every session runs in its own `systemd-run --user --scope` (orphans are stopped on restart); workers also get
  `MemoryMax`. Spawns are refused under `min_free_ram_gb`; GPUs are hidden unless a worker is spawned with `--gpu`
  (slots in `loop.yaml → infra`).
- Events handed to a meta-work turn stay "in flight" until it completes; a crash, restart or stop puts them back.
- The API accepts writes only from the UI's own origin or non-browser clients. CLI updates (Agent CLIs page) are
  refused while sessions of that CLI are running, and each update is followed by a smoke turn.

## Customising per project

Edit the workspace harness from the UI (workspace page → Harness) or any editor:
`meta/AGENTS.md` (meta-work/meta-chat behaviour), `worker/AGENTS.md`, `*/skills/<name>/SKILL.md` (shared by both CLIs),
`*/memory/`, `*/docs/`, `*/hooks.json`, optional `*/codex.toml` (extra Codex config). The project's own
`CLAUDE.md`/`AGENTS.md` in `repo/` still apply to workers.
