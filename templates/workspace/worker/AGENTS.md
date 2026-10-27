# Environment

- Work on the current branch and never push. Changes you leave uncommitted are committed for you when you finish.
- A Python virtualenv is already active (`$VIRTUAL_ENV`): install missing packages there, never create another
  one or install into the user or system Python. Run the project from your checkout instead of installing it
  there; to test packaging, use a throwaway venv in `$TMPDIR`.
- Scratch work goes in `$TMPDIR`, which is emptied when your turn ends.
- Run any command that may take over 2 minutes as `loop run -- <cmd>`, whatever it is: a build, a scan, a
  sweep, a scoring run or a search counts as much as training. If you would give a command a timeout over
  2 minutes, or cannot tell how long it will take, run it this way; a job that ends within 90 seconds still
  returns its output at once, so short runs lose nothing.
- Check `loop infra` for room before a heavy job. `loop run` waits while RAM is over 90% used, and above 95%
  the newest heavy process is stopped.
- Only a job run with `--vram <GB>` sees a GPU (`loop run --vram 8 -- <cmd>`); `$LOOP_VRAM_GB` holds its grant.
  It may use more while the GPU has room; when the GPU fills up, the newest job on it is stopped, so ask for its
  peak VRAM.
- `loop run` shows the job's output and returns its exit code if it ends within 90 seconds; otherwise the job keeps
  running in the background, past your turn, and its result arrives as a new message when it ends. Never poll it
  (no `sleep` loops, no repeated reads of its log).

## Answer rules

- Outputs that are not code (logs, metrics, plots, reports) go under `.artifacts/` in the project directory
  unless the prompt names another place. It is kept for the requester, so it holds only results and evidence
  worth keeping. `.artifacts/` is never committed.
- Deliver what the prompt asks for, at the scope it intends. If a better approach or a real problem outside
  that scope turns up, say so in your final answer instead of widening the work.
- Your work ends when you send your final answer, which is also where any question for the requester goes;
  processes still running then are stopped, except `loop run` jobs.

## Coding and editing rules

- Follow PEP 8 for Python, with lines up to about 88 characters. Before each blocks with substantial logic, add a short descriptive comment.
- When updating content, completely replace outdated parts without leaving obsolete details behind.
- Write code, comments, identifiers and commit messages in English, matching the repo.
- Before finishing, delete the temporary files, scratch scripts and debug output you created, only keep nessesary files. 
- Minimal code modifications for optimal efficiency. Edit files with your file-editing tool (such as `Edit` or `apply_patch`) rather than shell commands.