"""Git operations on the project repo: integration branch, worker worktrees, snapshots, merges."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
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
    # Transliterate accents (Vietnamese "lọc từ" -> "loc tu") before dropping non-ASCII
    ascii_text = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    ascii_text = ascii_text.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:limit].strip("-") or "w"


def current_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD")


def branch_exists(repo: Path, branch: str) -> bool:
    return bool(git(repo, "branch", "--list", branch))


def int_branch(gid: str) -> str:
    return f"loop/{gid}/integration"


def int_worktree(ws: Path, gid: str) -> Path:
    return ws / "wt" / gid


def goal_refs(ws: Path, gid: str) -> list[str]:
    """Branches loop/<gid>/* and tags exp/<gid>/* in the project repo, also those an accepted goal left behind."""
    repo = ws / "repo"
    if not repo.exists():
        return []
    return git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/loop/{gid}", f"refs/tags/exp/{gid}",
               check=False).splitlines()


def move_legacy_worktree(ws: Path, gid: str) -> bool:
    """Move a goal's integration worktree from its old place, wt/_int-<gid>, to wt/<gid>. Returns True if moved."""
    old, new = ws / "wt" / f"_int-{gid}", int_worktree(ws, gid)
    if not old.exists() or new.exists():
        return False
    git(ws / "repo", "worktree", "move", str(old), str(new))
    return True


def ensure_integration(ws: Path, gid: str, base: str | None) -> Path:
    """Create branch loop/<gid>/integration (from base) and its worktree wt/<gid>."""
    repo, branch = ws / "repo", int_branch(gid)
    wt = int_worktree(ws, gid)
    if not branch_exists(repo, branch):
        git(repo, "branch", branch, base or current_branch(repo))
    if not wt.exists():
        git(repo, "worktree", "prune")
        git(repo, "worktree", "add", str(wt), branch)
    # The meta-work may work here without any worker, so its checkpoints must skip these from the start
    ensure_excluded(repo, LOCAL_EXCLUDES)
    return wt


# Checkout-local dirs: a virtualenv, and outputs the platform collects into runs/<sid>/artifacts/
LOCAL_EXCLUDES = (".venv/", ".artifacts/")


def add_worker_worktree(ws: Path, gid: str, sid: str, role: str) -> tuple[Path, str]:
    repo, wt = ws / "repo", ws / "wt" / sid
    branch = f"loop/{gid}/{role}-{sid}"
    git(repo, "worktree", "add", "-b", branch, str(wt), int_branch(gid))
    ensure_excluded(repo, LOCAL_EXCLUDES)
    return wt, branch


def ensure_excluded(repo: Path, patterns: tuple[str, ...]) -> None:
    """Keep patterns out of every snapshot via the shared, never-committed info/exclude of the repo."""
    path = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    missing = [p for p in patterns if p not in text.splitlines()]
    if missing:
        sep = "\n" if text and not text.endswith("\n") else ""
        with path.open("a") as fh:
            fh.write(sep + "".join(f"{p}\n" for p in missing))


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def snapshot(path: Path, message: str, actor: str | None = None) -> bool:
    """Commit any uncommitted work (bypassing project hooks), as `actor` or the platform bot.
    Returns True if a commit was made."""
    if not git(path, "status", "--porcelain"):
        return False
    who = ["-c", f"user.name={actor}", "-c", f"user.email={re.sub(r'[^a-z0-9.-]', '', actor)}@agentloop"] \
        if actor else BOT
    git(path, "add", "-A")
    git(path, *who, "commit", "--no-verify", "-q", "-m", message)
    return True


def checkpoint(ws: Path, gid: str, actor: str, message: str) -> str:
    """Commit the meta-work's uncommitted changes in the integration worktree and return its HEAD.

    Raises instead of committing when the worktree is off the integration branch (a checkout or rebase in
    progress) or holds unresolved conflicts, so no caller goes on as if the work were saved."""
    wt, branch = int_worktree(ws, gid), int_branch(gid)
    on = git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if on != branch:
        raise RuntimeError(f"{wt} is on {on or 'a detached HEAD'}, not {branch}; switch back first")
    if conflicts := git(wt, "diff", "--name-only", "--diff-filter=U").splitlines():
        raise RuntimeError(f"unresolved conflicts in {wt}: {', '.join(conflicts)}; resolve or abort first")
    snapshot(wt, message, actor)
    return head(wt)


def untracked(path: Path) -> set[str]:
    """Untracked, non-ignored files: what a snapshot would add."""
    return set(git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}


def restore_after_run(path: Path, before: set[str]) -> tuple[str, list[str]]:
    """Undo what a command run did to a clean checkout: tracked edits are reset to HEAD and files it created
    (untracked now, not in `before`) are removed; ignored files stay. Returns the tracked diff and removed files."""
    changed = git(path, "diff", "HEAD")
    if changed:
        git(path, "reset", "--hard", "-q")
    created = sorted(untracked(path) - before)
    for rel in created:
        (path / rel).unlink(missing_ok=True)
        for parent in (path / rel).parents:  # directories the run created, now empty
            if parent == path or any(parent.iterdir()):
                break
            parent.rmdir()
    return changed, created


def merge(ws: Path, gid: str, branch: str) -> dict:
    """Merge a worker branch into the integration branch; abort and report on conflicts.

    The worktree may hold the meta-work's own changes, so the caller checkpoints it first; nothing is discarded."""
    wt = int_worktree(ws, gid)
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


def branches(ws: Path, gid: str) -> list[dict]:
    repo, into = ws / "repo", int_branch(gid)
    if not branch_exists(repo, into):
        return []
    out = []
    for line in git(repo, "for-each-ref", "--format=%(refname:short)|%(committerdate:relative)|%(subject)",
                    f"refs/heads/loop/{gid}/*").splitlines():
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


def cleanup(ws: Path, gid: str, workers: list[dict]) -> list[str]:
    """Remove worker worktrees; delete merged branches, tag unmerged ones as exp/<gid>/<sid>."""
    repo, into, tagged = ws / "repo", int_branch(gid), []
    for w in workers:
        remove_worktree(ws, Path(w["cwd"]))
        branch = w.get("branch")
        if not branch or not branch_exists(repo, branch):
            continue
        if not is_merged(repo, branch, into):
            tag = f"exp/{gid}/{w['id']}"
            git(repo, "tag", "-f", tag, branch, check=False)
            tagged.append(tag)
        git(repo, "branch", "-D", branch, check=False)
    remove_worktree(ws, int_worktree(ws, gid))
    git(repo, "worktree", "prune", check=False)
    return tagged


def remove_goal(ws: Path, gid: str, workers: list[dict]) -> None:
    """Delete what a goal left in the project repo: worktrees, loop/<gid>/* branches and exp/<gid>/* tags."""
    repo = ws / "repo"
    for wt in [Path(w["cwd"]) for w in workers] + [int_worktree(ws, gid)]:
        remove_worktree(ws, wt)
        shutil.rmtree(wt, ignore_errors=True)  # a worktree git refused to remove would keep its branch alive
    git(repo, "worktree", "prune", check=False)
    for kind, prefix in (("branch", f"refs/heads/loop/{gid}/"), ("tag", f"refs/tags/exp/{gid}/")):
        refs = git(repo, "for-each-ref", "--format=%(refname:short)", prefix, check=False).split()
        if refs:
            git(repo, kind, "-D" if kind == "branch" else "-d", *refs, check=False)


def has_origin(repo: Path) -> bool:
    return "origin" in git(repo, "remote", check=False).split()


def run_remote(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    """Run a command that talks to the remote; prompts are disabled so a missing credential fails, not hangs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_")}
    env.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        return subprocess.run(argv, cwd=repo, capture_output=True, text=True, env=env, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{argv[0]} {argv[1]} timed out") from None


def pull_base(ws: Path, base: str) -> None:
    """Fast-forward local base to origin/base, so a new goal branches from work merged through earlier PRs.

    No-op without an origin or when origin has no such branch; a base that cannot fast-forward (local commits not
    on origin, or uncommitted changes in the way) raises instead of being rewritten."""
    repo = ws / "repo"
    if not has_origin(repo):
        return
    proc = run_remote(repo, "git", "fetch", "-q", "origin", base)
    if proc.returncode:
        if "couldn't find remote ref" in proc.stderr:
            return
        raise RuntimeError(f"git fetch origin {base}: {proc.stderr.strip()}")
    if not branch_exists(repo, base):
        git(repo, "branch", base, "FETCH_HEAD")
    elif current_branch(repo) == base:
        proc = subprocess.run(["git", "merge", "--ff-only", "-q", "FETCH_HEAD"], cwd=repo, capture_output=True,
                              text=True, env={k: v for k, v in os.environ.items() if not k.startswith("LOOP_")})
        if proc.returncode:
            raise RuntimeError(f"cannot fast-forward {base} in repo/ to origin/{base}: "
                               f"{proc.stderr.strip() or proc.stdout.strip()}")
    elif is_merged(repo, base, "FETCH_HEAD"):
        git(repo, "update-ref", f"refs/heads/{base}", "FETCH_HEAD")
    else:
        raise RuntimeError(f"local {base} has commits that are not on origin/{base}; push or reconcile them first")


def push_goal(ws: Path, gid: str, base: str, title: str, body: str) -> dict:
    """Push loop/<gid>/integration to origin and open a pull request into base with the gh CLI.

    The base branch is left alone. The PR is optional: without gh, or for a non-GitHub remote, the reason comes back in `pr_error`."""
    repo, branch = ws / "repo", int_branch(gid)
    if not has_origin(repo):
        raise RuntimeError("repo/ has no remote 'origin'")
    if not branch_exists(repo, branch):
        raise RuntimeError(f"{branch} no longer exists")
    run = lambda *argv: run_remote(repo, *argv)
    proc = run("git", "push", "-u", "origin", branch)
    if proc.returncode:
        raise RuntimeError(f"git push: {proc.stderr.strip() or proc.stdout.strip()}")
    if not shutil.which("gh"):
        return {"branch": branch, "base": base, "pr": None, "pr_error": "gh CLI not found; open the PR yourself"}
    # An existing PR for the branch is reported with its URL too
    proc = run("gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body[:60000])
    url = re.search(r"https://\S+/pull/\d+", proc.stdout + proc.stderr)
    return {"branch": branch, "base": base, "pr": url.group(0) if url else None,
            "pr_error": None if url else (proc.stderr.strip() or proc.stdout.strip())[-500:]}
