"""Workspace and mission files: creation from templates, actor-tagged commits, protection."""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

from . import gitops
from .config import PYTHON, TEMPLATES, workspace_config, workspaces_dir
from .guard import PROTECTED_RE

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
HUMAN_FILES = {"spec.md", "dod.md", "mission.yaml"}
MISSION_FILES = ("spec.md", "dod.md", "plan.md", "journal.md", "notes.md", "report.md", "mission.yaml")
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

PRE_COMMIT = """#!/bin/sh
# agent-loop: agents (LOOP_ROLE set) may not commit to protected branches
if [ -n "$LOOP_ROLE" ]; then
  branch=$(git symbolic-ref --short HEAD 2>/dev/null)
  case " {protected} " in *" $branch "*) echo "agent-loop: agents cannot commit to '$branch'" >&2; exit 1;; esac
fi
[ -x "$0.orig" ] && exec "$0.orig" "$@"
exit 0
"""
PRE_PUSH = """#!/bin/sh
# agent-loop: agents never push; the platform pushes only when the mission policy allows it
if [ -n "$LOOP_ROLE" ]; then echo "agent-loop: agents cannot push" >&2; exit 1; fi
[ -x "$0.orig" ] && exec "$0.orig" "$@"
exit 0
"""


def ws_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid workspace name: {name!r}")
    return workspaces_dir() / name


def list_workspaces() -> list[str]:
    root = workspaces_dir()
    return sorted(p.name for p in root.iterdir() if (p / "loop.yaml").exists()) if root.exists() else []


def mission_dir(ws: Path, mid: str) -> Path:
    if not NAME_RE.match(mid) and not re.match(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+$", mid):
        raise ValueError(f"invalid mission id: {mid!r}")
    return ws / "missions" / mid


def list_missions(ws: Path) -> list[str]:
    root = ws / "missions"
    return sorted((p.name for p in root.iterdir() if (p / "mission.yaml").exists()), reverse=True) \
        if root.exists() else []


def commit(ws: Path, actor: str, message: str) -> bool:
    """Stage everything and commit as `actor`; no-op on a clean tree."""
    with _locks[str(ws)]:
        gitops.git(ws, "add", "-A")
        if not gitops.git(ws, "diff", "--cached", "--name-only"):
            return False
        gitops.git(ws, "-c", f"user.name={actor}", "-c", f"user.email={re.sub(r'[^a-z0-9.-]', '', actor)}@agentloop",
                   "commit", "-q", "--no-verify", "-m", message)
        return True


def commit_external(ws: Path) -> bool:
    return commit(ws, "human", "human: edits made outside the UI")


def enforce_protected(ws: Path) -> list[str]:
    """Revert agent changes to human-owned files. Returns the reverted paths.

    The changed copy is kept under .loop/reverted/<ts>/ in case the human edited it mid-turn."""
    reverted, backup = [], ws / ".loop" / "reverted" / time.strftime("%Y%m%d-%H%M%S")
    with _locks[str(ws)]:
        for line in gitops.git(ws, "status", "--porcelain", "--untracked-files=all").splitlines():
            code, path = line[:2], line[3:].strip().strip('"')
            if not PROTECTED_RE.match(path):
                continue
            if (ws / path).is_file():
                (backup / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ws / path, backup / path)
            if code == "??":
                (ws / path).unlink(missing_ok=True)
            else:
                gitops.git(ws, "checkout", "HEAD", "--", path, check=False)
            reverted.append(path)
    return reverted


def install_git_hooks(repo: Path, protected: list[str]) -> None:
    hooks = Path(gitops.git(repo, "rev-parse", "--git-path", "hooks"))
    hooks = hooks if hooks.is_absolute() else repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    for name, body in (("pre-commit", PRE_COMMIT.replace("{protected}", " ".join(protected))),
                       ("pre-push", PRE_PUSH)):
        hook = hooks / name
        if hook.exists() and "agent-loop" not in hook.read_text(errors="ignore"):
            hook.rename(hook.with_name(name + ".orig"))
        hook.write_text(body)
        hook.chmod(0o755)


def create_workspace(name: str, repo: str | None = None, base_branch: str | None = None) -> Path:
    """Copy the template, set up the project repo (clone or empty) and init the workspace git."""
    ws = ws_path(name)
    if ws.exists():
        raise ValueError(f"workspace {name!r} already exists")
    shutil.copytree(TEMPLATES / "workspace", ws, symlinks=True)
    for layer in ("meta", "worker"):
        hooks = ws / layer / "hooks.json"
        hooks.write_text(hooks.read_text().replace("{{PY}}", PYTHON))
    for sub in ("missions", "wt", "data", ".loop"):
        (ws / sub).mkdir(exist_ok=True)
    try:
        if repo:
            gitops.git(ws, "clone", "-q", repo, "repo")
            if base_branch:
                gitops.git(ws / "repo", "checkout", "-q", base_branch)
        else:
            (ws / "repo").mkdir()
            gitops.git(ws / "repo", "init", "-q", "-b", "main")
            (ws / "repo" / "README.md").write_text(f"# {name}\n")
            gitops.git(ws / "repo", "add", "-A")
            gitops.git(ws / "repo", *gitops.BOT, "commit", "-q", "-m", "init")
        base = gitops.current_branch(ws / "repo")
        install_git_hooks(ws / "repo", sorted({"main", "master", base}))
        gitops.git(ws, "init", "-q", "-b", "main")
        commit(ws, "control-plane", f"control-plane: create workspace {name}")
    except Exception:
        shutil.rmtree(ws, ignore_errors=True)
        raise
    return ws


def create_mission(ws: Path, title: str, spec: str, dod: str, config: dict | None = None) -> str:
    title = (title or "").strip()
    if not title:
        raise ValueError("mission title is required")
    base_id = f"{date.today().isoformat()}-{gitops.slug(title, 40)}"
    mid, i = base_id, 2
    while (ws / "missions" / mid).exists():
        mid, i = f"{base_id}-{i}", i + 1
    mdir = ws / "missions" / mid
    shutil.copytree(TEMPLATES / "mission", mdir)
    (mdir / "runs").mkdir()
    (mdir / "spec.md").write_text(spec.strip() + "\n" if spec.strip() else (mdir / "spec.md").read_text())
    (mdir / "dod.md").write_text(dod.strip() + "\n" if dod.strip() else (mdir / "dod.md").read_text())
    defaults = workspace_config(ws)["defaults"]
    doc = {"title": title, "created": time.strftime("%Y-%m-%d %H:%M")}
    for key in ("meta", "advisor", "roles", "budget", "base_branch", "push"):
        doc[key] = (config or {}).get(key, defaults[key])
    (mdir / "mission.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    commit(ws, "control-plane", f"control-plane: new mission {mid}")
    return mid


def read_mission_files(ws: Path, mid: str) -> dict:
    mdir = mission_dir(ws, mid)
    return {n: (mdir / n).read_text() if (mdir / n).exists() else "" for n in MISSION_FILES}


def human_write(ws: Path, rel: str, text: str) -> str:
    """Write a human-owned file and commit it. Returns the diff (for notifying the orchestrator)."""
    target = (ws / rel).resolve()
    if not target.is_relative_to(ws.resolve()):
        raise ValueError("path escapes the workspace")
    name = target.name
    if rel != "loop.yaml" and not (name in HUMAN_FILES and target.parent.parent.name == "missions"):
        raise ValueError(f"{rel} is not a human-editable file")
    if name.endswith(".yaml"):
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("YAML must be a mapping")
    target.write_text(text if text.endswith("\n") else text + "\n")
    with _locks[str(ws)]:
        change = gitops.git(ws, "diff", "--", rel)
    commit(ws, "human", f"human: edit {rel}")
    return change


def codex_home(ws: Path, layer: str) -> Path:
    """Materialize a CODEX_HOME for a layer: shared auth, the layer's hooks, and worker instructions."""
    home = ws / ".loop" / "codex" / layer
    home.mkdir(parents=True, exist_ok=True)
    user_auth = Path.home() / ".codex" / "auth.json"
    auth = home / "auth.json"
    # Codex may replace the symlink with a refreshed token file: copy it back before relinking
    if auth.exists() and not auth.is_symlink():
        if not user_auth.exists() or auth.stat().st_mtime > user_auth.stat().st_mtime:
            shutil.copy2(auth, user_auth)
        auth.unlink()
    links = {"auth.json": user_auth, "hooks.json": ws / layer / "hooks.json"}
    if layer == "worker":
        links["AGENTS.md"] = ws / "worker" / "AGENTS.md"
        # A real skills dir with one link per harness skill: Codex writes its own .system skills
        # here, and they must not leak into the shared harness
        skills = home / "skills"
        if skills.is_symlink():
            skills.unlink()
        skills.mkdir(exist_ok=True)
        wanted = {d.name: d for d in (ws / "worker" / "skills").iterdir() if not d.name.startswith(".")}
        for old in skills.iterdir():
            if old.is_symlink() and old.name not in wanted:
                old.unlink()
        links.update({f"skills/{name}": src for name, src in wanted.items()})
    for name, src in links.items():
        dst = home / name
        if dst.is_symlink() and dst.resolve() == src.resolve():
            continue
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)
    custom = ws / layer / "codex.toml"
    config = custom.read_text() if custom.exists() else ""
    cfg_path = home / "config.toml"
    if not cfg_path.exists() or cfg_path.read_text() != config:
        cfg_path.write_text(config)
    return home


HARNESS_ROOTS = ("meta", "worker")
SKIP_DIRS = {"tmp", ".git", "__pycache__", ".system"}


def harness_files(ws: Path) -> list[str]:
    """Editable harness files (symlinked skill folders are listed once, at their real location)."""
    out = ["PLATFORM.md"]
    for root in HARNESS_ROOTS:
        for path in sorted((ws / root).rglob("*")):
            rel = path.relative_to(ws)
            if any(part in SKIP_DIRS for part in rel.parts) or any(p.is_symlink() for p in
                                                                      [ws / Path(*rel.parts[:i + 1]) for i in range(len(rel.parts))]):
                continue
            if path.is_file() and path.stat().st_size < 256_000 and path.name != ".gitkeep":
                out.append(rel.as_posix())
    return out


def harness_write(ws: Path, rel: str, text: str) -> None:
    """Create or update a harness file as the human (hooks.json must stay valid JSON)."""
    target = (ws / rel).resolve()
    top = Path(rel).parts[0] if Path(rel).parts else ""
    inside = target == (ws / "PLATFORM.md").resolve() or (
        top in HARNESS_ROOTS and target.is_relative_to((ws / top).resolve()))
    if not inside or ".." in Path(rel).parts:
        raise ValueError("only files under meta/, worker/ or PLATFORM.md are harness files")
    if any(part in SKIP_DIRS for part in Path(rel).parts):
        raise ValueError(f"{rel} is not a harness file")
    if target.name.endswith(".json"):
        json.loads(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if text.endswith("\n") else text + "\n")
    commit(ws, "human", f"human: harness {rel}")
