# Working style

- You run unattended: nobody answers questions while you work. Make reasonable assumptions, state them,
  and finish the task. If a decision only the requester can make blocks you, stop and ask it clearly at the end.
- Commit meaningful steps on the current branch with clear messages. Never push.
- Missing tools or packages: create a virtualenv in the project directory (`python -m venv .venv`) and install
  there; never install into the user or system Python (`pip --user`, `sudo`, conda base).
- Outputs that are not code (logs, metrics, plots, reports) go under `.artifacts/` in the project directory
  unless the prompt names another place. `.venv/` and `.artifacts/` are never committed.
- Check your own work (run tests, inspect outputs) before you finish. Report honestly: failures and
  uncertainty are useful information.

## Coding and editing rules

- Follow PEP 8 for Python. Add a short explanatory comment before any block containing substantial logic.
- Keep temporary experiments and prototypes out of your commits (for example under `/tmp`), and delete what is
  no longer needed when you finish.
- When updating content, completely replace outdated parts without leaving obsolete details behind.
- Keep code, reports and generated content concise and free of repetition.
- Stay within the task in your prompt and don't change the behavior of other modules.
