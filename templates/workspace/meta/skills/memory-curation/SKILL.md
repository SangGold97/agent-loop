---
name: memory-curation
description: Add to, revise or prune long-term memory (meta/memory, worker/memory), or write a skill - use it when evidence clearly refutes an entry, a durable lesson emerges, a procedure has recurred enough to become a skill, or the human asks for a memory review
---

# Curating long-term memory

Later goals read memory before they start, so every line costs attention in every future
session. Memory meets three standards:

- **Concise:** few files, short ones; a file earns its place only if it changes what an agent
  would do.
- **Current:** each file holds the latest state; what changed is replaced, not appended,
  while what still holds, such as a refutation or an open lead, is compressed rather than
  dropped for being old.
- **Accurate:** every claim matches the code, the platform and the latest evidence.

Before adding something, look for the file whose scope it fits and extend that one; a new
file suits a scope no file covers yet, while a single fact or one goal's events usually fit
an existing file.

## Review file by file

Start from `README.md`, then take each file in turn and ask:

1. **Purpose.** What decision does it change, and for whom? If nothing would change without
   it, delete it.
2. **Still true?** Check its claims against the code, the platform (`../PLATFORM.md`, `loop`
   help), current results and the latest evidence, not against the memory's own wording. A
   lesson built on a wrong premise is rewritten from the facts or deleted.
3. **Right place.** Choices the human makes themselves (model selection, submissions,
   budgets) stay out of memory. How a mode should behave belongs in `AGENTS.md`, which is
   human-owned: propose wording there instead of writing a memory. A recurring procedure with a
   verifiable outcome belongs in a skill (see Skills); memory keeps only the lesson and names
   the skill.
4. **Overlap.** What `AGENTS.md`, a skill, `docs/`, the goal's own files or another memory
   already says can go.
   Files on the same scope can merge into one, renamed to fit what it now holds.
5. **Form.** Guidance the agent reasons with, not hard rules, mandatory steps, fixed timers or
   forced plan writes. Record what was observed and its evidence, not how to reason. Give a
   reason only where the lesson is not obvious without it, as a clause rather than a "Why"
   section.

## Shape

- One file per scope: a cross-goal lesson (how to orchestrate, how to operate), or one ledger
  per task or topic holding its current state.
- A ledger opens with a dated state and the identifiers that make it reproducible (commit,
  hash, path, command), then what holds, what was refuted and by what evidence, directions
  tagged `[measured]` / `[model]` / `[paper]`, and open leads. A superseded result shrinks to
  one line naming what replaced it.
- Frontmatter: `name` (the file name), `description` (one line used to judge relevance),
  `metadata.type`. Link related files with `[[name]]`. Use absolute dates.
- `README.md` lists every file with a one-line hook.
- Short sentences, no repetition, no ready-made prompt text.
- `../worker/memory/` stays generic engineering guidance: never mention agent-loop,
  meta-agents or this platform there.

## Skills

Write a skill only for a procedure that has recurred, with clear steps and a verifiable
outcome: `skills/<name>/SKILL.md` (or `../worker/skills/`), frontmatter `name` and a
`description` of what it does and when to use it (all an agent sees until it applies), then
the steps, commands and how to verify the result. Change one only on evidence it is wrong, and
note the change in `plan.md` for the human. Worker skills stay generic.

## Applying

- With the human in the chat, propose per file (keep, trim, merge, move or delete, with the
  new text) and write only after they approve; take their adjustments as given.
- After editing, update `README.md` for every add, merge, rename or delete, and grep
  `memory/`, `docs/`, `skills/` and `../worker/` for links or references to removed names.
