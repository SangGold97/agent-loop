# Workspace map (agent-loop)

This workspace is run by agent-loop. The workspace root is `$LOOP_WS`; paths below are relative to it.

| Path | Purpose | Written by |
|---|---|---|
| `loop.yaml` | Workspace defaults: agents, models, budgets, limits | human |
| `meta/` | Meta-agent harness: `AGENTS.md`, `skills/`, `memory/`, `docs/` | human; meta may improve memory/docs/skills |
| `worker/` | Worker harness: `AGENTS.md`, `skills/`, `memory/`, `docs/`, `claude-plugin/` | human; meta may improve memory/docs/skills |
| `*/hooks.json` | Guard hooks (Claude Code and Codex share the format) | human only |
| `goals/<id>/spec.md`, `goal.md`, `goal.yaml` | Context and requirements, success criteria, goal config | human only |
| `goals/<id>/plan.md` | Current strategy and checklist | meta-work |
| `goals/<id>/journal.md` | Append-only decision log | meta-work |
| `goals/<id>/notes.md` | Human guidance for the meta-work | platform (from the human) |
| `goals/<id>/report.md` | Completion report | platform (from `loop done`) |
| `goals/<id>/runs/<sid>/` | Per-worker evidence: `t<n>.prompt.md`, `t<n>.out.md`, `t<n>.diff`, `artifacts/` | platform, worker |
| `repo/` | The project's git repository | workers (through worktrees), meta-work; only the human merges `main` |
| `wt/<sid>/` | One git worktree per worker, branch `loop/<goal>/<label>-<sid>` | worker |
| `wt/_int-<goal>/` | Integration branch `loop/<goal>/integration` | platform (`loop merge`), meta-work |
| `data/` | Large shared data (datasets, checkpoints) | human, workers that fetch data |
| `.loop/` | Runtime logs and state | platform |

Memory: `meta/memory/` holds cross-goal lessons for meta-agents, `worker/memory/` for workers.
Keep one short file per lesson and list it in that folder's `README.md`.
