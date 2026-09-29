"""Git operations on the project repo: integration branch, worker worktrees, snapshots, merges."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

BOT = ["-c", "user.name=agentloop", "-c", "user.email=agentloop@local"]


def git(cwd: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_")}
    run_env.update(env or {})
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=run_env)
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def slug(text: str, limit: int = 24) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "w"


def current_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD")


def branch_exists(repo: Path, branch: str) -> bool:
    return bool(git(repo, "branch", "--list", branch))


def int_branch(mid: str) -> str:
    return f"loop/{mid}/integration"


def ensure_integration(ws: Path, mid: str, base: str | None) -> Path:
    """Create branch loop/<mid>/integration (from base) and its worktree wt/_int-<mid>."""
    repo, branch = ws / "repo", int_branch(mid)
    wt = ws / "wt" / f"_int-{mid}"
    if not branch_exists(repo, branch):
        git(repo, "branch", branch, base or current_branch(repo))
    if not wt.exists():
        git(repo, "worktree", "prune")
        git(repo, "worktree", "add", str(wt), branch)
    return wt


def add_worker_worktree(ws: Path, mid: str, sid: str, label: str) -> tuple[Path, str]:
    repo, wt = ws / "repo", ws / "wt" / sid
    branch = f"loop/{mid}/{slug(label)}-{sid}"
    git(repo, "worktree", "add", "-b", branch, str(wt), int_branch(mid))
    return wt, branch


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def snapshot(path: Path, message: str) -> bool:
    """Commit any uncommitted work (bypassing project hooks). Returns True if a commit was made."""
    if not git(path, "status", "--porcelain"):
        return False
    git(path, "add", "-A")
    git(path, *BOT, "commit", "--no-verify", "-q", "-m", message)
    return True


def diff(path: Path, start: str, end: str) -> tuple[str, str]:
    if start == end:
        return "", ""
    return git(path, "diff", "--stat", start, end), git(path, "diff", start, end)


def merge(ws: Path, mid: str, branch: str) -> dict:
    """Merge a worker branch into the integration branch; abort and report on conflicts."""
    wt = ws / "wt" / f"_int-{mid}"
    # Only merges write here; stray files (verify-run output, an interrupted merge) are discarded
    git(wt, "merge", "--abort", check=False)
    git(wt, "reset", "--hard", "-q")
    git(wt, "clean", "-fdq")
    before = head(wt)
    out = subprocess.run(["git", *BOT, "merge", "--no-ff", "--no-edit", branch], cwd=wt,
                         capture_output=True, text=True)
    if out.returncode:
        conflicts = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
        git(wt, "merge", "--abort", check=False)
        return {"ok": False, "conflicts": conflicts, "message": (out.stdout + out.stderr).strip()[-2000:]}
    after = head(wt)
    return {"ok": True, "commit": after, "changed": before != after,
            "stat": git(wt, "diff", "--stat", before, after) if before != after else ""}


def is_merged(repo: Path, branch: str, into: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", branch, into], cwd=repo).returncode == 0


def branches(ws: Path, mid: str) -> list[dict]:
    repo, into = ws / "repo", int_branch(mid)
    if not branch_exists(repo, into):
        return []
    out = []
    for line in git(repo, "for-each-ref", "--format=%(refname:short)|%(committerdate:relative)|%(subject)",
                    f"refs/heads/loop/{mid}/*").splitlines():
        name, when, subject = line.split("|", 2)
        ahead = git(repo, "rev-list", "--count", f"{into}..{name}") if name != into else "0"
        out.append({"branch": name, "when": when, "subject": subject, "ahead": int(ahead),
                    "merged": name == into or is_merged(repo, name, into)})
    return out


def log(path: Path, ref: str = "HEAD", n: int = 30) -> list[dict]:
    fmt = "%h|%an|%ar|%s"
    lines = git(path, "log", f"-{n}", f"--format={fmt}", ref, check=False).splitlines()
    return [dict(zip(("hash", "author", "when", "subject"), ln.split("|", 3))) for ln in lines if ln]


def remove_worktree(ws: Path, wt: Path) -> None:
    if wt.exists():
        git(ws / "repo", "worktree", "remove", "--force", str(wt), check=False)


def cleanup(ws: Path, mid: str, workers: list[dict]) -> list[str]:
    """Remove worker worktrees; delete merged branches, tag unmerged ones as exp/<mid>/<sid>."""
    repo, into, tagged = ws / "repo", int_branch(mid), []
    for w in workers:
        remove_worktree(ws, Path(w["cwd"]))
        branch = w.get("branch")
        if not branch or not branch_exists(repo, branch):
            continue
        if not is_merged(repo, branch, into):
            tag = f"exp/{mid}/{w['id']}"
            git(repo, "tag", "-f", tag, branch, check=False)
            tagged.append(tag)
        git(repo, "branch", "-D", branch, check=False)
    remove_worktree(ws, ws / "wt" / f"_int-{mid}")
    git(repo, "worktree", "prune", check=False)
    return tagged
