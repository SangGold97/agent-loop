# Environment

- Work on the current branch and never push. Changes you leave uncommitted are committed for you when you finish.
- A Python virtualenv is already active (`$VIRTUAL_ENV`): install missing packages there, never create another
  one or install into the user or system Python. Run the project from your checkout instead of installing it
  there; to test packaging, use a throwaway venv in `$TMPDIR`.
- Scratch work goes in `$TMPDIR`, which is emptied when your turn ends.
- RAM on this machine is shared. Run any job that may need more than about 2 GB (training, fine-tuning, large
  data loads) as `loop run --ram <GB> -- <cmd>`: it waits until that RAM is free, and a job that outgrows it is
  killed, so ask for its peak plus a margin. `loop infra` shows what is free.
- Outputs that are not code (logs, metrics, plots, reports) go under `.artifacts/` in the project directory
  unless the prompt names another place. It is kept for the requester, so it holds only results and evidence
  worth keeping. `.artifacts/` is never committed.
- Deliver what the prompt asks for, at the scope it intends. If a better approach or a real problem outside
  that scope turns up, say so in your final answer instead of widening the work.
- Your work ends when you send your final answer, which is also where any question for the requester goes;
  processes still running then are stopped.

## Coding and editing rules

- Follow PEP 8 for Python. Add a short explanatory comment before any block containing substantial logic.
- When updating content, completely replace outdated parts without leaving obsolete details behind.
- Keep code, reports and generated content concise and free of repetition.
