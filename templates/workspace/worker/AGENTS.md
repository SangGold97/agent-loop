# Environment

- Work on the current branch and never push. Changes you leave uncommitted are committed for you when you finish.
- Missing tools or packages: create a virtualenv in the project directory (`python -m venv .venv`) and install
  there; never install into the user or system Python (`pip --user`, `sudo`, conda base).
- Scratch work goes in `$TMPDIR`, which is emptied when your turn ends.
- Outputs that are not code (logs, metrics, plots, reports) go under `.artifacts/` in the project directory
  unless the prompt names another place. It is kept for the requester, so it holds only results and evidence
  worth keeping. `.venv/` and `.artifacts/` are never committed.
- Your work ends when you send your final answer, which is also where any question for the requester goes;
  processes still running then are stopped.

## Coding and editing rules

- Follow PEP 8 for Python. Add a short explanatory comment before any block containing substantial logic.
- When updating content, completely replace outdated parts without leaving obsolete details behind.
- Keep code, reports and generated content concise and free of repetition.
