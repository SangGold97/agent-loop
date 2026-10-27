"""PreToolUse guard shared by Claude Code, Codex (same hook protocol: exit code 2 blocks a call) and agy (a deny
decision on stdout).

Guards against mistakes, not adversaries: agents never push, never touch human-owned files or
the user's Python, workers stay inside their own worktree, and meta sessions change project code only in their
goal's integration worktree (meta-chats not at all), and no session polls running work from inside its turn. Messages shown to workers name no platform concepts, so a
worker experiences these as ordinary limits of its environment.
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
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit",
              "write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"}
SHELL_TOOLS = {"Bash", "shell", "exec_command", "local_shell", "run_command"}
PROTECTED_IN_CMD = re.compile(
    r"(goals/\S*(spec|goal)\.md|goal\.yaml|LOOP_GOAL_DIR\S*(spec|goal)\.md|hooks\.json|loop\.yaml)")
# Output redirections (`>`, `>>`, `2>`, `&>`, `>|`) and their target; `2>&1` and `>/dev/null` write nothing
REDIRECT = re.compile(r"(?:\d*|&)>{1,2}\|?\s*(&?[^\s;&|<>()]+)")
MODIFYING = {"tee", "mv", "rm", "truncate", "chmod", "ln"}  # any argument of these may be written or removed
IN_PLACE = re.compile(r"-[a-zA-Z]*i|--in-place")  # sed -i, perl -pi
# Git subcommands that change a checkout's files, index or HEAD
GIT_WRITES = {"add", "am", "apply", "checkout", "cherry-pick", "clean", "commit", "merge", "mv", "pull", "rebase",
              "reset", "restore", "revert", "rm", "stash", "switch"}
# Package installs that change the human's own Python instead of a project venv
# Waiting on other work from inside a turn: a while/until loop that sleeps, or following a file or command
POLL_LOOP = re.compile(r"\b(?:while|until)\b(?:(?!\bdone\b).)*?\bdo\b(?:(?!\bdone\b).)*?\bsleep\b", re.S)
FOLLOW = re.compile(r"\btail\b[^|;&\n]*\s(?:-[a-zA-Z]*[fF][a-zA-Z]*|--follow\S*)(?=\s|$)")
NO_POLL = ("waiting in a loop is disabled (sleep loops, tail -f, watch): run long work in the foreground or "
           "as `loop run -- <cmd>`, whose result comes back as a new message")
GLOBAL_INSTALL = re.compile(r"\bsudo\b|\bpip3?\b[^|;&\n]*\binstall\b[^|;&\n]*(--user|--break-system-packages)\b"
                            r"|\bconda\s+install\b(?![^|;&\n]*(-p|--prefix)\s)")


def _patch_paths(text: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", text, re.M)


def edit_paths(tool: str, inp: dict) -> list[str]:
    if tool in EDIT_TOOLS:
        return [p for p in (inp.get("file_path"), inp.get("notebook_path"), inp.get("TargetFile")) if p]
    if tool == "apply_patch":
        text = inp.get("command") or inp.get("patch") or inp.get("input") or json.dumps(inp)
        if isinstance(text, list):
            text = "\n".join(map(str, text))
        return [a or b for a, b in _patch_paths(str(text))]
    return []


ADVISOR_READ_ONLY = "the advisor writes only in $TMPDIR; return advice in your final reply"


def check_path(path: Path, env: dict) -> str | None:
    # The advisor's only writable place is its turn's own TMPDIR, which the platform removes after the turn
    if env.get("LOOP_ROLE") == "meta-advisor":
        tmp = env.get("TMPDIR")
        return None if tmp and path.resolve().is_relative_to(Path(tmp).resolve()) else ADVISOR_READ_ONLY
    ws = Path(env["LOOP_WS"]).resolve()
    path = path.resolve()
    if not path.is_relative_to(ws):
        return None
    rel = path.relative_to(ws)
    role = env.get("LOOP_ROLE")
    worker = role == "worker"
    if PROTECTED_RE.match(rel.as_posix()):
        return (f"{path} is read-only" if worker
                else f"{rel} is human-owned; propose changes in your plan or report instead")
    if worker:
        data = ws / "data"
        if not any(path.is_relative_to(o) for o in (ws / "wt" / env.get("LOOP_SESSION", "?"), data)):
            return f"writes are limited to the current checkout, {data} and /tmp (not {path})"
    elif rel.parts and rel.parts[0] in ("repo", "wt"):
        own = Path("wt") / env.get("LOOP_GOAL", "?")
        if role == "meta-chat":
            return "a meta-chat does not change project code; propose the change to the meta-work with `loop note`"
        if not rel.is_relative_to(own):
            return f"{rel} is not yours to change; make project changes in {ws / own}"
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


def simple_commands(code: str) -> list[tuple[str, list[str]]]:
    """The simple commands of a shell line as (text, words), leading VAR=value assignments dropped."""
    out = []
    for part in re.split(r"[;&|\n]+", code):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and re.match(r"\w+=", words[0]):
            words.pop(0)
        if words:
            out.append((part, words))
    return out


def file_args(words: list[str]) -> list[str]:
    """Arguments a command writes or removes: the cp destination, any argument of MODIFYING, sed/perl -i files."""
    name, args = os.path.basename(words[0]), words[1:]
    if name == "cp":
        return [a for a in args if not a.startswith("-")][-1:]
    if name in MODIFYING or (name in ("sed", "perl") and any(IN_PLACE.match(a) for a in args)):
        return args
    return []


def protected_write(code: str) -> str | None:
    """The human-owned file a shell command would write, if any. Only write targets count, so reading one
    (`cat goal.md 2>/dev/null`, `cp goal.md /tmp/`) passes."""
    for m in REDIRECT.finditer(code):
        target = m.group(1).strip("'\"")
        if not target.startswith("&") and PROTECTED_IN_CMD.search(target):
            return target
    for _, words in simple_commands(code):
        for t in file_args(words):
            if PROTECTED_IN_CMD.search(t):
                return t
    return None


def _resolve(arg: str, cwd: Path | None) -> Path | None:
    """A shell path argument as a path, or None when it cannot be known (variables, options, devices)."""
    if not arg or arg.startswith(("$", "&", "-", "/dev/")):
        return None
    if arg.startswith("~"):
        return Path(os.path.expanduser(arg))
    return Path(arg) if arg.startswith("/") else (cwd / arg if cwd else None)


def scope_write(code: str, env: dict, cwd: Path | None) -> str | None:
    """Meta sessions: why a shell command would write outside their scope (see check_path), if it would.

    Follows `cd` along the line and checks write targets and state-changing git commands; other ways to
    write (a script, a build) are not seen here and are reported by the platform after the turn."""
    for part, words in simple_commands(code):
        name, args = os.path.basename(words[0]), words[1:]
        if name == "cd":
            cwd = _resolve(args[0] if args else "~", cwd)
            continue
        targets = [_resolve(m.group(1).strip("'\""), cwd) for m in REDIRECT.finditer(strip_quotes(part))]
        targets += [_resolve(a, cwd) for a in file_args(words)]
        if name == "git":
            where, rest = cwd, list(args)
            while rest and rest[0].startswith("-"):
                opt = rest.pop(0)
                if opt == "-C" and rest:
                    where = _resolve(rest.pop(0), where)
                elif opt in ("-c", "--git-dir", "--work-tree") and rest:
                    rest.pop(0)
            if rest and rest[0] in GIT_WRITES:
                targets.append(where)
        for t in targets:
            if t and (reason := check_path(t, env)):
                return reason
    return None


def check_command(cmd: str, env: dict, cwd: Path | None = None) -> str | None:
    code = strip_heredocs(cmd)
    bare = strip_quotes(code)  # git checks ignore quoted text such as commit messages or prompts
    if re.search(r"\bgit\b[^|;&\n]*\bpush\b", bare):
        return "pushing is disabled here; commit locally and leave remotes to the repository owner"
    if target := protected_write(code):
        return (f"{target} is read-only" if env.get("LOOP_ROLE") == "worker"
                else f"{target} is human-owned; propose changes in your plan or report instead")
    if env.get("LOOP_ROLE") in ("meta-work", "meta-chat", "meta-advisor") and \
            (reason := scope_write(code, env, cwd)):
        return reason
    if GLOBAL_INSTALL.search(bare):
        return "do not install into the user or system Python; install into the active virtualenv ($VIRTUAL_ENV)"
    # A `loop run` job may wait on whatever it likes; only the turn itself must not poll
    own = re.sub(r"\bloop\s+run\b[^\n]*", "", code)
    if POLL_LOOP.search(own) or FOLLOW.search(own) or \
            any(os.path.basename(words[0]) == "watch" for _, words in simple_commands(own)):
        return NO_POLL
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
    if "toolCall" in event:  # agy names the call differently and runs its hooks in the plugin dir, not the cwd
        tool, inp = event["toolCall"].get("name", ""), event["toolCall"].get("args") or {}
        cwd = Path(inp.get("Cwd") or (event.get("workspacePaths") or [cwd])[0])
    # An advisor edit whose target cannot be read from the call is refused rather than let through
    if env.get("LOOP_ROLE") == "meta-advisor" and tool in EDIT_TOOLS | {"apply_patch"} and \
            not edit_paths(tool, inp):
        return ADVISOR_READ_ONLY
    if tool in SHELL_TOOLS:
        cmd = inp.get("command") or inp.get("cmd") or inp.get("CommandLine") or ""
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
    if reason and "toolCall" in event:
        print(json.dumps({"decision": "deny", "reason": reason}))
    elif reason:
        print(f"blocked: {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
