"""PreToolUse guard shared by Claude Code and Codex (same hook protocol): exit code 2 blocks a call.

Guards against mistakes, not adversaries: agents never push, never touch human-owned files or
the user's Python, and workers stay inside their own worktree. Messages shown to workers name no platform
concepts, so a worker experiences these as ordinary limits of its environment.
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
PROTECTED_IN_CMD = re.compile(
    r"(goals/\S*(spec|goal)\.md|goal\.yaml|LOOP_GOAL_DIR\S*(spec|goal)\.md|hooks\.json|loop\.yaml)")
# Output redirections (`>`, `>>`, `2>`, `&>`, `>|`) and their target; `2>&1` and `>/dev/null` write nothing
REDIRECT = re.compile(r"(?:\d*|&)>{1,2}\|?\s*(&?[^\s;&|<>()]+)")
MODIFYING = {"tee", "mv", "rm", "truncate", "chmod", "ln"}  # any argument of these may be written or removed
IN_PLACE = re.compile(r"-[a-zA-Z]*i|--in-place")  # sed -i, perl -pi
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
    worker = env.get("LOOP_ROLE") == "worker"
    if PROTECTED_RE.match(rel.as_posix()):
        return (f"{path} is read-only" if worker
                else f"{rel} is human-owned; propose changes in your journal/report instead")
    if worker:
        data = ws / "data"
        if not any(path.is_relative_to(o) for o in (ws / "wt" / env.get("LOOP_SESSION", "?"), data)):
            return f"writes are limited to the current checkout, {data} and /tmp (not {path})"
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


def protected_write(code: str) -> str | None:
    """The human-owned file a shell command would write, if any. Only write targets count, so reading one
    (`cat goal.md 2>/dev/null`, `cp goal.md /tmp/`) passes."""
    for m in REDIRECT.finditer(code):
        target = m.group(1).strip("'\"")
        if not target.startswith("&") and PROTECTED_IN_CMD.search(target):
            return target
    for part in re.split(r"[;&|\n]+", code):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and re.match(r"\w+=", words[0]):  # leading VAR=value assignments
            words.pop(0)
        if not words:
            continue
        name, args = os.path.basename(words[0]), words[1:]
        if name == "cp":
            targets = [a for a in args if not a.startswith("-")][-1:]  # only the destination is written
        elif name in MODIFYING or (name in ("sed", "perl") and any(IN_PLACE.match(a) for a in args)):
            targets = args
        else:
            continue
        for t in targets:
            if PROTECTED_IN_CMD.search(t):
                return t
    return None


def check_command(cmd: str, env: dict, cwd: Path | None = None) -> str | None:
    code = strip_heredocs(cmd)
    bare = strip_quotes(code)  # git checks ignore quoted text such as commit messages or prompts
    if re.search(r"\bgit\b[^|;&\n]*\bpush\b", bare):
        return "pushing is disabled here; commit locally and leave remotes to the repository owner"
    if target := protected_write(code):
        return (f"{target} is read-only" if env.get("LOOP_ROLE") == "worker"
                else f"{target} is human-owned; propose changes in your journal/report instead")
    if GLOBAL_INSTALL.search(bare):
        return ("do not install into the user or system Python; create a virtualenv in the project directory "
                "(python -m venv .venv) and install there")
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
            return f"blocked by guard pattern: {pattern}"
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
        print(f"blocked: {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
