"""`loop`: the command-line interface meta-agents use to drive the platform."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HELP = """Commands for meta-agents (run `loop <command> -h` for options):
  spawn   start a new worker session           loop spawn --agent claude --role coder --label api <<'EOF' ... EOF
  send    continue an idle worker              loop send w-1a2b3c <<'EOF' ... EOF
  stop    stop a running worker                loop stop w-1a2b3c
  ls      list this goal's workers
  show    worker details / a turn's evidence   loop show w-1a2b3c --turn 2 [--full] [--log]
  merge   merge a worker branch into loop/<goal>/integration
  done    close the goal with a report      loop done --status success <<'EOF' ... EOF
  status  goal overview (meta-chat)
  note    propose a note to the human (meta-chat)  loop note "try X instead of Y"
  budget  budget usage
  infra   host resources"""


def call(cmd: str, payload: dict) -> str:
    api, token = os.environ.get("LOOP_API"), os.environ.get("LOOP_TOKEN")
    if not api or not token:
        sys.exit("loop: only meta-agent sessions started by agent-loop can use this command")
    req = urllib.request.Request(f"{api}/api/agent/{cmd}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Loop-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=1900) as resp:
            return json.loads(resp.read())["text"]
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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="loop", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("spawn", help="start a new worker")
    sp.add_argument("--agent", required=True, help="worker CLI (claude or codex); model and effort are fixed by the human")
    sp.add_argument("--role", required=True, help="what the worker does, e.g. coder, reviewer, research")
    sp.add_argument("--label", default="")
    sp.add_argument("--gpu", action="store_true")
    for name, sparser in (("spawn", sp), ("send", sub.add_parser("send", help="continue an idle worker"))):
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
    a = p.parse_args(argv)

    if a.cmd in ("spawn", "send"):
        payload = {"prompt": text_arg(a, "prompt")}
        payload.update({"agent": a.agent, "role": a.role, "label": a.label, "gpu": a.gpu} if a.cmd == "spawn" else {"sid": a.sid})
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
