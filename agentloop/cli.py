"""`loop`: the command-line interface meta-agents use to drive the platform."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HELP = """Commands for meta-agents (run `loop <command> -h` for options):
  spawn   start a new worker session           loop spawn --agent claude --role coder <<'EOF' ... EOF
  send    continue an idle worker              loop send w-1a2b3c <<'EOF' ... EOF
  advise  request advice; the reply arrives as an event   loop advise <<'EOF' ...question... EOF
  stop    stop a running worker                loop stop w-1a2b3c
  ls      list this goal's workers
  show    worker details / a turn's evidence   loop show w-1a2b3c --turn 2 [--full] [--log]
  merge   merge a worker branch into loop/<goal>/integration
  done    close the goal with a report      loop done --status success <<'EOF' ... EOF
  status  goal overview (meta-chat)
  note    propose a note to the human (meta-chat)  loop note "try X instead of Y"
  budget  budget usage
  infra   host resources and the RAM pool (workers too)
  run     run a RAM-heavy job with a RAM grant (workers too)   loop run --ram 10 -- python train.py"""


def call(cmd: str, payload: dict, full: bool = False):
    api, token = os.environ.get("LOOP_API"), os.environ.get("LOOP_TOKEN")
    if not api or not token:
        sys.exit("loop: only sessions started by agent-loop can use this command")
    req = urllib.request.Request(f"{api}/api/agent/{cmd}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Loop-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=1900) as resp:
            data = json.loads(resp.read())
            return data if full else data["text"]
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read()).get("error") or exc.reason
        except ValueError:
            msg = exc.reason
        sys.exit(f"loop {cmd}: {msg}")
    except urllib.error.URLError as exc:
        sys.exit(f"loop {cmd}: cannot reach the platform at {api} ({exc.reason})")
    except TimeoutError:
        sys.exit(f"loop {cmd}: no answer from the platform within 30 min; check `loop status` before retrying")


def text_arg(args, name: str) -> str:
    """Text from --<name>, --<name>-file, or stdin (heredoc)."""
    value = getattr(args, name, None)
    path = getattr(args, f"{name}_file", None)
    if value:
        return value
    if path:
        with open(path) as fh:
            return fh.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    sys.exit(f"loop: provide the {name} via --{name}, --{name}-file or stdin")


def run_job(gb: float, wait_min: float, cmd: list[str]) -> int:
    """`loop run`: hold a RAM grant from the shared pool while `cmd` runs.

    The job runs in a scope of its own capped at its grant, so a job that outgrows its grant is killed by the
    kernel inside that scope and costs no other process. This helper and the job keep the default OOM score while
    the session's other processes go first, so a job that keeps to its grant is not killed for another's
    overuse. The grant ends with the job; if this helper dies, the platform stops the job and frees the grant."""
    if not cmd:
        sys.exit("loop run: give the command after --, e.g. loop run --ram 10 -- python train.py")
    grant = call("ram", {"gb": gb, "wait": wait_min * 60, "pid": os.getpid(), "cmd": shlex.join(cmd)}, full=True)
    print(f"loop run: {grant['text']}", file=sys.stderr, flush=True)
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("0")
    # A SIGTERM (e.g. the shell running this being stopped) must still end the job and release the grant
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    rc, child = 127, None
    try:
        child = subprocess.Popen([*grant["argv"], *cmd])
        rc = child.wait()
    except OSError as exc:
        print(f"loop run: cannot start {cmd[0]}: {exc}", file=sys.stderr)
    finally:
        if child and child.poll() is None:
            child.kill()
        # The platform stops whatever is left in the job's scope when the grant is released
        with contextlib.suppress(SystemExit):
            call("ram-release", {"grant": grant["grant"], "killed": rc == -signal.SIGKILL})
    if rc == -signal.SIGKILL:
        print(f"loop run: the job was killed (SIGKILL), most likely for outgrowing its {gb} GB grant; run it "
              "with a larger --ram or make it use less memory", file=sys.stderr)
        return 137
    print(f"loop run: done (exit {rc})", file=sys.stderr)
    return rc


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="loop", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("spawn", help="start a new worker")
    sp.add_argument("--agent", required=True, help="worker CLI (claude, codex or agy); model and effort are fixed by the human")
    sp.add_argument("--role", required=True, help="what the worker does, e.g. coder, reviewer, research")
    sp.add_argument("--gpu", action="store_true")
    for name, sparser in (("spawn", sp), ("send", sub.add_parser("send", help="continue an idle worker")),
                          ("advise", sub.add_parser("advise", help="request advice asynchronously"))):
        if name == "send":
            sparser.add_argument("sid")
        sparser.add_argument("--prompt")
        sparser.add_argument("--prompt-file", dest="prompt_file")
    for name in ("stop", "merge"):
        sub.add_parser(name).add_argument("sid")
    show = sub.add_parser("show")
    show.add_argument("sid")
    show.add_argument("--turn", type=int)
    show.add_argument("--full", action="store_true")
    show.add_argument("--log", action="store_true", help="commands the worker ran, with the end of each output")
    done = sub.add_parser("done")
    done.add_argument("--status", required=True, choices=["success", "partial", "blocked"])
    done.add_argument("--report")
    done.add_argument("--report-file", dest="report_file")
    sub.add_parser("note").add_argument("text")
    for name in ("ls", "status", "budget", "infra"):
        sub.add_parser(name)
    run = sub.add_parser("run", help="run a RAM-heavy job with a RAM grant")
    run.add_argument("--ram", type=float, required=True, help="GB the job needs at its peak")
    run.add_argument("--wait", type=float, default=30, help="minutes to wait for the RAM (max 30)")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- command and its arguments")
    a = p.parse_args(argv)

    if a.cmd == "run":
        sys.exit(run_job(a.ram, a.wait, a.command[1:] if a.command[:1] == ["--"] else a.command))
    if a.cmd in ("spawn", "send", "advise"):
        payload = {"prompt": text_arg(a, "prompt")}
        if a.cmd == "spawn":
            payload.update({"agent": a.agent, "role": a.role, "gpu": a.gpu})
        elif a.cmd == "send":
            payload["sid"] = a.sid
    elif a.cmd in ("stop", "merge"):
        payload = {"sid": a.sid}
    elif a.cmd == "show":
        payload = {"sid": a.sid, "turn": a.turn, "full": a.full, "log": a.log}
    elif a.cmd == "done":
        payload = {"status": a.status, "report": text_arg(a, "report")}
    elif a.cmd == "note":
        payload = {"text": a.text}
    else:
        payload = {}
    print(call(a.cmd, payload))


if __name__ == "__main__":
    main()
