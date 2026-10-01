"""PreToolUse guard shared by Claude Code and Codex (same hook protocol): exit code 2 blocks a call.

Guards against mistakes, not adversaries: agents never push, never touch human-owned files or
the user's Python, and workers stay inside their own worktree.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

PROTECTED_RE = re.compile(
    r"^(goals/[^/]+/(spec\.md|goal\.md|goal\.yaml)|loop\.yaml|(meta|worker)/hooks\.json)$")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "shell", "exec_command", "local_shell"}
WRITE_OPS = r"(>|\btee\b|\bsed\s+-i|\bmv\b|\brm\b|\bcp\b|\btruncate\b|\bchmod\b|\bln\b)"
PROTECTED_IN_CMD = r"(goals/\S*(spec|goal)\.md|goal\.yaml|LOOP_GOAL_DIR\S*(spec|goal)\.md|hooks\.json|loop\.yaml)"
# Package installs that change the human's own Python instead of a project venv
GLOBAL_INSTALL = re.compile(r"\bsudo\b|\bpip3?\b[^|;&\n]*\binstall\b[^|;&\n]*(--user|--break-system-packages)\b"
                            r"|\bconda\s+install\b(?![^|;&\n]*(-p|--prefix)\s)")


def _patch_paths(text: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", text, re.M)


def edit_paths(tool: str, inp: dict) -> list[str]:
    if tool in EDIT_TOOLS:
        return [p for p in (inp.get("file_path"), inp.get("notebook_path")) if p]
    if tool == "apply_patch":
        text = inp.get("command") or inp.get("patch") or inp.get("input") or json.dumps(inp)
        if isinstance(text, list):
            text = "\n".join(map(str, text))
        return [a or b for a, b in _patch_paths(str(text))]
    return []


def check_path(path: Path, env: dict) -> str | None:
    ws = Path(env["LOOP_WS"]).resolve()
    path = path.resolve()
    if not path.is_relative_to(ws):
        return None
    rel = path.relative_to(ws)
    if PROTECTED_RE.match(rel.as_posix()):
        return f"{rel} is human-owned; propose changes in your journal/report instead"
    role = env.get("LOOP_ROLE")
    if role == "worker":
        own = [ws / "wt" / env.get("LOOP_SESSION", "?"), ws / "data"]
        if env.get("LOOP_RUN_DIR"):
            own.append(Path(env["LOOP_RUN_DIR"]).resolve())
        if not any(path.is_relative_to(o.resolve()) for o in own):
            return f"workers may only write inside their worktree, their run dir and data/ (not {rel})"
    return None


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (prompt text passed to `loop spawn` etc. is data, not commands)."""
    lines, out, end = cmd.split("\n"), [], None
    for line in lines:
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = re.search(r"(?<!<)<<(?!<)-?\s*['\"]?([A-Za-z_]\w*)['\"]?", line)  # not <<< or $((1<<2))
        if m:
            end = m.group(1)
    return "\n".join(out)


def strip_quotes(cmd: str) -> str:
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "''", cmd)


def check_command(cmd: str, env: dict, cwd: Path | None = None) -> str | None:
    code = strip_heredocs(cmd)
    bare = strip_quotes(code)  # git checks ignore quoted text such as commit messages or prompts
    if re.search(r"\bgit\b[^|;&\n]*\bpush\b", bare):
        return "agents never push; the human (or the goal push policy) handles remotes"
    if re.search(WRITE_OPS, code) and re.search(PROTECTED_IN_CMD, code):
        return "this command would modify a human-owned file (spec/goal/goal.yaml/loop.yaml/hooks.json)"
    if GLOBAL_INSTALL.search(bare):
        return ("do not install into the user or system Python; create a venv in your worktree or "
                "$LOOP_RUN_DIR (python -m venv .venv) and install there")
    cmd = code
    for match in re.finditer(r"\brm\s+((?:-\S+\s+)*)(.+?)(?:$|[;&|])", cmd, re.M):
        if "r" not in match.group(1) and "f" not in match.group(1):
            continue
        try:
            targets = shlex.split(match.group(2))
        except ValueError:
            targets = match.group(2).split()
        for t in targets:
            t = re.sub(r"^\$\{?HOME\}?", "~", t)
            if t in ("/", "/*", "~", "~/", "~/*", "..", "../") or t.startswith(("/etc", "/usr")):
                return f"refusing dangerous rm target: {t}"
            if t.startswith("~"):
                t = os.path.expanduser(t)
            elif cwd and not t.startswith(("/", "$", "-")):  # relative targets resolve against the tool cwd
                t = str(cwd / t)
            if t.startswith("/"):
                inside = Path(t).resolve().is_relative_to(Path(env["LOOP_WS"]).resolve())
                if not inside and t.startswith("/tmp"):  # scratch space outside the workspace
                    continue
                reason = check_path(Path(t), env)
                if reason or not inside:
                    return reason or f"rm outside the workspace is not allowed: {t}"
    for pattern in json.loads(env.get("LOOP_GUARD_PATTERNS") or "[]"):
        if re.search(pattern, cmd):
            return f"blocked by workspace guard pattern: {pattern}"
    return None


def check(event: dict, env: dict) -> str | None:
    if not env.get("LOOP_ROLE") or not env.get("LOOP_WS"):
        return None
    tool, inp = event.get("tool_name", ""), event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or os.getcwd())
    if tool in SHELL_TOOLS:
        cmd = inp.get("command", "")
        return check_command(" ".join(cmd) if isinstance(cmd, list) else str(cmd), env, cwd)
    for p in edit_paths(tool, inp):
        reason = check_path(cwd / os.path.expanduser(p), env)
        if reason:
            return reason
    return None


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        reason = check(event, dict(os.environ))
    except Exception:  # never break the agent because the guard itself failed
        return
    if reason:
        print(f"agent-loop guard: {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
