# agent-loop

A local, human-on-the-loop platform that wraps **Claude Code**, **Codex** and **Antigravity** (`agy`) (headless CLIs).
You write a **spec** and a **goal**; a **meta-work** session (meta-agent) plans, writes prompts for
**worker** sessions, evaluates their evidence against the goal, and keeps iterating until it decides the goal is
done. You steer with notes, chat with a **meta-chat** session (meta-agent), review the report, and merge.

```
Human ──► UI (goals, live sessions, notes, meta-chat, infra)
            │
     control plane (FastAPI + SQLite)  ── budgets · guard · git · digests · infra caps
            │
   meta-work (meta-agent) ── `loop spawn / send / merge / done` ──► workers (Claude Code | Codex | agy)
                                                                  one git worktree + branch each
```

## Setup

```bash
make setup          # venv + editable install (needs git, and logged-in `claude` / `codex` / `agy` CLIs)
make serve          # http://127.0.0.1:8765
make start          # same, as the background service (make stop / make logs); needs remote/server-finish.sh
make test           # unit + end-to-end tests (scripted fake agent, no API cost)
```

Requirements: Linux/WSL with systemd (for the shared RAM pool), Python ≥ 3.10.
The platform uses your existing CLI logins; it never asks for keys.
To run it on another machine and use the UI from this one, see [remote/README.md](remote/README.md).

## Concepts

| | |
|---|---|
| **Workspace** (`workspaces/<name>/`, own git) | One project: `repo/` (the project, cloned or empty), `meta/` + `worker/` harnesses, `goals/`, `data/`, `wt/` worktrees, and `.venv/`, the Python virtualenv every session of the workspace runs in. Every change is committed with its actor (`human`, `meta-work(<sid>)`, `control-plane`, `meta-chat(<sid>)`). |
| **Goal** (`goals/<slug>/`) | `spec.md`, `goal.md`, `goal.yaml` (human-owned), `plan.md` (meta-work: the current state, including what was ruled out), `notes.md`, `report.md`, `runs/<worker>/` evidence. The slug of the title is the goal id, also used in its branches `loop/<slug>/*` and its integration worktree `wt/<slug>/`, so a title whose slug is already taken in the workspace is refused. |
| **Meta-work** | One meta session per goal, driven by events (kickoff, worker finished, note, goal changed, budget). It does simple work itself in the integration worktree, where the platform commits its changes as `meta-work(<sid>)` (before a spawn, merge or `loop done` and after each turn), and delegates the rest to workers. |
| **Meta-chat** | Prompt-by-prompt chat bound to a goal (agent/model/effort chosen per chat); runs commands and tools freely but does not change project code, and steers the goal only through notes you approve. A chat is stored once you send its first message and can be deleted from its bar. |
| **Meta-advisor** | Consulted through `loop advise`, with agent/model/effort from `goal.yaml → meta_advisor`. Each question starts a new read-only session for one turn right away, while the meta-work turn that asked goes on. Its final text returns to the meta-work inbox as `[Advice]`; the Advisor button beside META-WORK opens its session log, without a chat composer. |
| **Worker** | A Claude Code, Codex or agy session (the meta-work picks the CLI and role; model/effort come from `goal.yaml` → `workers`), in its own worktree on `loop/<goal>/<role>-<sid>`; accepted work is merged into `loop/<goal>/integration`. It receives the meta-work's prompt as is and a generic `worker/AGENTS.md`, nothing about the platform; `.venv/` and `.artifacts/` stay out of git, and `.artifacts/` is kept in `runs/<sid>/artifacts/`. `loop retire W` removes a finished worker's worktree but keeps its branch and log; a worker without a worktree shows as retired and can no longer be sent. |
| **Digest** | What a finished worker turn sends to the meta-work: a line naming the worker, role and turn, then its final reply as is (kept in `runs/<sid>/t<n>.out.md`). Its changes and the commands it ran are pulled on demand, with git and `loop show W --log`. |
| **goal.md** | Success criteria, free text. Lines `- verify: \`cmd\`` are run on the committed integration branch before a `success` close (a run that edits tracked files is rejected and its edits and new files are undone); everything else is judged by the meta-work. |

## Guardrails (mistakes, not adversaries)

- One PreToolUse guard (`agentloop/guard.py`) serves both CLIs: no `git push`, no writes to human-owned files,
  workers confined to their worktree and `data/`, meta-work project changes confined to its goal's integration
  worktree, none from meta-chats, extra regexes from `loop.yaml → guard`. A meta turn that still changes `repo/`
  (or a meta-chat turn that changes the integration worktree) raises an alert.
- Claude sessions connect only the MCP servers in `loop.yaml → mcp_servers` (URLs; default none).
- Git hooks in the project repo block agent commits to protected branches and any agent push.
- The platform never touches the base branch. Accepting a goal removes its worktrees and keeps
  `loop/<goal>/integration`; **push** on the goal page then pushes that branch to `origin` and opens a pull request
  into the base branch with your `gh` login; merging the PR is yours. Starting a new goal first fast-forwards the
  local base branch from `origin` (the start is refused when it cannot fast-forward), so the goal builds on merged
  PRs. The base branch is `base_branch` from `goal.yaml`, or else the branch `repo/` had checked out when the goal
  started.
- Agents read only their own instructions: Claude sessions get `--settings` from `.loop/claude/<layer>-settings.json`
  (the layer's `hooks.json` plus `claudeMdExcludes` for every directory above `meta/` or the worktree), so a
  `CLAUDE.md`/`AGENTS.md` in the platform repo or another parent never reaches them. Codex workers stop at the
  worktree's git root, and Codex meta sessions run with `project_root_markers=[]` so they read only `meta/`.
  agy sessions run agy's default agent, with your own agy skills, plugins and MCP servers, and stop at the git root
  as Codex does; their layer's instructions (mode text, `worker/AGENTS.md` and an index of `worker/skills/`) lead
  each turn's prompt, and the guard reaches them through `~/.gemini/config/plugins/agentloop/`, a plugin whose hook
  does nothing outside agent-loop sessions. agy has no autocompact setting.
- After every agent turn the control plane reverts edits to `spec.md`, `goal.md`, `goal.yaml`, `loop.yaml`, `hooks.json`
  (the changed copy is kept in `.loop/reverted/<time>/`, and the UI shows an alert).
- Budgets (concurrency, worker/meta turns) are enforced by the control plane; advisor consultations count toward
  `max_meta_turns` alongside meta-work turns. On exhaustion the meta-work gets one final turn to close the goal,
  after outstanding advice arrives. With no worker or advisor running, the meta-work is prompted to continue
  after each turn; after three turns in a row that leave the integration branch unchanged, the goal pauses.
- A CLI's usage limit is not a failure: the meta-work waits for the announced reset while workers on the other
  CLI keep running, a refused worker turn is not counted, and spawns on that CLI are refused until the reset.
  After each turn the platform records how much of the CLI's usage windows is used (Claude's `rate_limit_event`,
  Codex's rollout, `agy -p /usage`) and shows it on the Agent CLIs page and in `loop infra`; once a window passes
  90%, every running goal's meta-work is told so with its next turn.
- A meta-work turn carries its events and nothing else: the `[Budget]` line joins only once 80% of the worker or
  meta turns are spent, and resources reach it as events when they run low (RAM refusals, free disk under 10 GB,
  usage limits); `loop infra` and `loop budget` show the rest on demand.
- Every turn runs in its own `systemd-run --user --scope`. Processes still running when the CLI exits (a job the
  agent backgrounded) are stopped and named in the digest, detached jobs aside (below); orphans are stopped on
  restart. Each turn also gets its
  own `TMPDIR`, removed when the turn ends, so scratch files never pile up in `/tmp`. GPUs are hidden from every
  session; a job sees one only through `loop run --vram` (below).
- RAM is one pool for all sessions: their scopes share a slice capped at host RAM minus `pool_reserve_gb`, so an
  out-of-memory kill stays among agent processes. Nothing is reserved up front: heavy jobs run through
  `loop run -- <cmd>` in a scope of their own, and the platform watches the whole pool every 2 s. While it is
  over 90% used, `loop run` waits (one job starts per sample, so the next sees what the last one took), and
  spawns, sends and advice are refused; the refused goal waits without counting as idle and gets an event once
  the pool has room. Above 95%, the newest heavy process (a `loop run` job, or a tool process of a turn holding
  1 GB or more; never an agent CLI or a `loop run` helper) is stopped, one every 5 s, so the jobs that were there
  first keep running; its `loop run` prints why. The kernel's OOM killer, should the pool run out between
  samples, takes them in the same order (`oom_score_adj` from 1000 for the newest heavy one, 200 for other tool
  processes, 0 for CLIs and helpers). If the helper dies, the platform stops the job with it. Processes outside
  the platform can still exhaust the host's reserve. The digest reports a turn's memory peak and kills.
- CPU is shared by weight, never reserved or waited for: an idle core goes to whoever wants it. Every turn and job
  scope has the same `CPUWeight`, so under contention busy jobs split the CPU per scope rather than per thread, and
  the pool slice as a whole yields to the platform server and the human's own programs. A `loop run` job gets
  `OMP_NUM_THREADS`, `MKL_NUM_THREADS` and `OPENBLAS_NUM_THREADS` set to the cores the running jobs leave free (at
  least 2) and `OMP_WAIT_POLICY=PASSIVE`, unless its environment sets them. When the CPU averages 90% or more over
  10 minutes, running goals hear it once (again after it falls below 70%); `loop infra` shows that average.
- `loop run --detach` runs the job's helper as a user service outside the turn's scope and returns at once, so
  the agent ends its turn instead of polling. The job stays in the pool, survives a platform restart and is stopped
  after `job_timeout_h` (9 h); a `loop run` inside it belongs to the job, not to the turn. When it ends, the platform resumes the worker with its exit status and log tail,
  right after its turn if one is running; when that turn cannot start (budget, RAM, usage limit), or the job is
  the meta-work's, the result goes to the meta-work as an event. `loop stop` stops one job, or a worker with its
  jobs, and a goal that stops stops its jobs.
- VRAM is granted per job: `loop run --vram <GB> -- <cmd>` waits until one GPU has that much left
  to grant (its total minus `vram_reserve_gb`, minus every grant in full and what other programs use), then shows
  the job that GPU alone. VRAM has no hard cap, so a job may use more than its grant while the GPU has room; once
  less than `vram_reserve_gb` is free there, the newest job with processes on that GPU is stopped, as the RAM pool
  stops its newest heavy process (the newest job granted the GPU where the driver names no processes), and memory
  outside the platform's jobs stops nothing. Where the driver gives no memory per process (WSL), the jobs together
  are measured by what the GPU uses above its level without grants, which the job's record keeps. GPUs are
  read every 10 s on their own thread (CPU, RAM and disk every 2 s); while `nvidia-smi` hangs, the infra page marks the last reading stale and no job is
  stopped on it.
- Events handed to a meta-work turn stay "in flight" until it completes; a crash, restart or stop puts them back.
- The advisor is read-only like a meta-chat: the guard blocks its edits, its `loop` token permits read commands
  only, and a turn that still changes `repo/` or the integration worktree raises an alert. A stopped or
  interrupted consultation reports its status back to the inbox and is never resumed; queued requests survive a
  server restart. A quota-refused consultation is refunded from the shared meta turn budget.
- The API accepts writes only from the UI's own origin or non-browser clients. CLI updates (Agent CLIs page) are
  refused while sessions of that CLI are running, and each update is followed by a smoke turn.

## Customising per project

Edit the workspace harness from the UI (workspace page → Harness) or any editor:
`meta/AGENTS.md` (meta-work/meta-chat/advisor behaviour), `worker/AGENTS.md`, `*/skills/<name>/SKILL.md` (shared by all CLIs),
`*/memory/`, `*/docs/`, `*/hooks.json`, optional `*/codex.toml` (extra Codex config). The project's own
`CLAUDE.md`/`AGENTS.md` in `repo/` still apply to workers.
