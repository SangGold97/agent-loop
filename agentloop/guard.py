"""PreToolUse guard shared by Claude Code, Codex (same hook protocol: exit code 2 blocks a call) and agy (a deny
decision on stdout).

Guards against mistakes, not adversaries: agents never push, never touch human-owned files or
the user's Python, workers stay inside their own worktree, and meta sessions change project code only in their
goal's integration worktree (meta-chats not at all), and no session polls running work from inside its turn
(nor does a worker sleep long). Messages shown to workers name no platform concepts, so a
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
# Waiting on other work from inside a turn: a while/until loop that sleeps, following a file or command, and
# for a worker any sleep longer than LONG_SLEEP_S
FOLLOW = re.compile(r"\btail\b[^|;&\n]*\s(?:-[a-zA-Z]*[fF][a-zA-Z]*|--follow\S*)(?=\s|$)")
NO_POLL = ("waiting in a loop is disabled (sleep loops, tail -f, watch): run long work in the foreground or "
           "as `loop run -- <cmd>`, whose result comes back as a new message")
LONG_SLEEP_S = 30
NO_SLEEP = f"sleeping over {LONG_SLEEP_S} s is disabled; {NO_POLL.split(': ', 1)[1]}"
SEPARATORS = set(";&|(){}`\n")
SHELL_TOKEN = re.compile(r"[;&|(){}`\n]|[^\s;&|(){}`]+")
# Words a command follows; so does `-c` anywhere, as in `bash -c`
LEADS = {"do", "then", "else", "elif", "if", "while", "until", "!", "time", "exec", "nohup"}
DURATION = re.compile(r"(\d+\.?\d*|\.\d+)([smhd]?)")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
QUOTED = re.compile(r"'([^']*)'|\"((?:\\.|[^\"\\])*)\"")
# Package installs that change the human's own Python instead of a project venv
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


ADVISOR_SCOPE = ("in the workspace the advisor writes only in its own worktree and data/; "
                 "return advice in your final reply")


def check_path(path: Path, env: dict) -> str | None:
    ws = Path(env["LOOP_WS"]).resolve()
    path = path.resolve()
    if env.get("LOOP_ROLE") == "meta-advisor" and env.get("TMPDIR") and \
            path.is_relative_to(Path(env["TMPDIR"]).resolve()):
        return None
    if not path.is_relative_to(ws):
        return None
    rel = path.relative_to(ws)
    role = env.get("LOOP_ROLE")
    worker = role == "worker"
    if PROTECTED_RE.match(rel.as_posix()):
        return (f"{path} is read-only" if worker
                else f"{rel} is human-owned; propose changes in your plan or report instead")
    # A worker and an advisor each build in their own worktree and share data/
    if worker or role == "meta-advisor":
        data = ws / "data"
        if not any(path.is_relative_to(o) for o in (ws / "wt" / env.get("LOOP_SESSION", "?"), data)):
            return f"writes are limited to the current checkout, {data} and /tmp (not {path})" if worker \
                else ADVISOR_SCOPE
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
    return QUOTED.sub("''", cmd)


def outside_loop_run(code: str) -> str:
    """The shell text outside `loop run` commands, whose job may wait on whatever it likes. Only a `loop run`
    outside quotes counts, so a quoted pattern such as `grep -q "loop run: done" job.log` is still checked."""
    blank = QUOTED.sub(lambda m: " " * len(m.group(0)), code)
    out, last = [], 0
    for m in re.finditer(r"\bloop\s+run\b[^\n]*", blank):
        out.append(code[last:m.start()])
        last = m.end()
    return "".join(out) + code[last:]


def waits(code: str, worker: bool) -> str | None:
    """Why a shell command would wait on other work from inside the turn, if it would. The line is checked
    with its quoted text dropped, so a quoted "done" in a grep pattern neither ends nor hides a loop, and with
    only its quote marks dropped, for a quoted script (`bash -c 'while ...; do sleep 5; done'`)."""
    texts = [strip_quotes(code), QUOTED.sub(lambda m: m.group(0)[1:-1], code)]
    if any(FOLLOW.search(t) for t in texts) or \
            any(os.path.basename(words[0]) == "watch" for _, words in simple_commands(code)):
        return NO_POLL
    for text in texts:
        if reason := sleep_wait(SHELL_TOKEN.findall(text), worker):
            return reason
    return None


def sleep_wait(tokens: list[str], worker: bool) -> str | None:
    """Walk a shell line's tokens, tracking the loops open at each command: a sleep inside a while/until loop
    (its condition or body, nested loops included) polls; a worker's sleep outside one waits too when long.
    A short sleep elsewhere passes (a retry in a for loop, a server starting)."""
    loops, at_cmd, i = [], True, 0
    while i < len(tokens):
        tok, i = tokens[i], i + 1
        if tok in SEPARATORS:
            at_cmd = True
            continue
        if at_cmd and tok in ("while", "until", "for", "select"):
            loops.append(tok)
        elif at_cmd and tok == "done" and loops:
            loops.pop()
        elif at_cmd and tok == "timeout":  # `timeout [options] N cmd`: the command after N runs
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            i += 1
            continue
        elif at_cmd and tok == "sleep":
            if "while" in loops or "until" in loops:
                return NO_POLL
            # GNU sleep adds up its arguments; one it cannot read here ($T) counts as nothing
            secs = 0.0
            while i < len(tokens) and ((m := DURATION.fullmatch(tokens[i])) or tokens[i] in ("inf", "infinity")):
                secs += float(m[1]) * UNITS[m[2]] if m else float("inf")
                i += 1
            if worker and secs > LONG_SLEEP_S:
                return NO_SLEEP
        at_cmd = tok in LEADS if at_cmd else tok == "-c"
    return None


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
    if reason := waits(outside_loop_run(code), env.get("LOOP_ROLE") == "worker"):
        return reason
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
        return ADVISOR_SCOPE
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
