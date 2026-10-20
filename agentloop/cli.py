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
  stop    stop a running worker (and its jobs), or one job   loop stop w-1a2b3c | loop stop j-4d5e6f
  ls      list this goal's workers
  show    worker details / a turn's evidence   loop show w-1a2b3c --turn 2 [--full] [--log]
  merge   merge a worker branch into loop/<goal>/integration
  done    close the goal with a report      loop done --status success <<'EOF' ... EOF
  status  goal overview (meta-chat)
  note    propose a note to the human (meta-chat)  loop note "try X instead of Y"
  budget  budget usage
  infra   host resources, the RAM pool and GPU VRAM (workers too)
  run     run a RAM-heavy or GPU job (workers too)   loop run [--vram 8] [--detach] -- CMD"""


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


def run_job(vram: float, wait_min: float, cmd: list[str], job: str | None = None) -> int:
    """`loop run`: run `cmd` once the shared RAM pool has room, with a VRAM grant on one GPU if asked.

    The job runs in a scope of its own in the pool. Nothing is reserved: when the pool fills up, the platform stops
    the newest heavy process, and the kernel's OOM killer, should it come to that, takes them in the same order;
    this helper keeps the default OOM score. Only a job granted VRAM sees a GPU; the platform stops it if it is
    over that grant while the GPU is nearly full. If this helper dies, the platform stops the job. A job that ends
    on its own gets its peak RAM (sampled every 2 s) printed."""
    if not cmd:
        sys.exit("loop run: give the command after --, e.g. loop run -- python train.py")
    grant = call("ram", {"vram": vram, "wait": wait_min * 60, "pid": os.getpid(), "cmd": shlex.join(cmd),
                         "job": job}, full=True)
    print(f"loop run: {grant['text']}", file=sys.stderr, flush=True)
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("0")
    # A SIGTERM (e.g. the shell running this being stopped) must still end the job
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    rc, child, released = 127, None, {"text": "released"}
    try:
        child = subprocess.Popen([*grant["argv"], *cmd], env={**os.environ, **grant.get("env", {})})
        rc = child.wait()
    except OSError as exc:
        print(f"loop run: cannot start {cmd[0]}: {exc}", file=sys.stderr)
    finally:
        if child and child.poll() is None:
            child.kill()
        # The platform stops whatever is left in the job's scope when the job is released
        with contextlib.suppress(SystemExit):
            released = call("ram-release", {"grant": grant["grant"], "killed": rc == -signal.SIGKILL, "job": job},
                            full=True)
    if released["text"] != "released":  # the platform stopped the job for RAM or VRAM and says why
        print(f"loop run: {released['text']}", file=sys.stderr)
        return 137
    if rc == -signal.SIGKILL:
        print("loop run: the job was killed (SIGKILL), most likely by the kernel when the RAM pool ran out; run it "
              "again once `loop infra` shows room, or make it use less memory", file=sys.stderr)
        return 137
    peak = released.get("peak_gb")
    print(f"loop run: done (exit {rc})" + (f"; peak RAM {peak:.1f} GB" if peak is not None else ""), file=sys.stderr)
    return rc


def detach_job(vram: float, wait_min: float, cmd: list[str]) -> int:
    """`loop run --detach`: start the job under a `loop run` helper of its own, outside this turn, and return at
    once. When the job ends, the platform hands its result to this session in a new turn (an event for the
    meta-work)."""
    if not cmd:
        sys.exit("loop run: give the command after --, e.g. loop run --detach -- python train.py")
    job = call("job", {"vram": vram, "cmd": shlex.join(cmd)}, full=True)
    log, tmp = Path(job["log"]), Path(job["tmp"])
    log.parent.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMPDIR": str(tmp)}
    argv = [sys.executable, "-m", "agentloop.cli", "run", "--job", job["id"], "--vram", str(vram),
            "--wait", str(wait_min), "--", *cmd]
    try:
        if job["unit"]:
            # A user service is outside this turn's scope, so it outlives the turn and a platform restart
            subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={job['unit']}",
                            f"--working-directory={os.getcwd()}", "-p", f"StandardOutput=append:{log}",
                            "-p", f"StandardError=append:{log}",
                            *(f"--setenv={k}={v}" for k, v in env.items() if "\n" not in v), "--", *argv],
                           check=True)
        else:
            with log.open("ab") as fh:
                subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"loop run: cannot start job {job['id']}: {exc}")
    print(job["text"])
    return 0


def job_helper(job: str, vram: float, wait_min: float, cmd: list[str]) -> int:
    """The detached side of `loop run --detach`: run the job as `loop run` does, then report how it ended."""
    try:
        rc = run_job(vram, wait_min, cmd, job)
        text = f"exit {rc}"
    except SystemExit as exc:  # no room, or no platform: the job never started
        print(exc.code, file=sys.stderr, flush=True)
        rc, text = 1, f"not started ({exc.code})"
    with contextlib.suppress(SystemExit):
        call("job-end", {"job": job, "text": text})
    return rc


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="loop", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("spawn", help="start a new worker")
    sp.add_argument("--agent", required=True, help="worker CLI (claude, codex or agy); model and effort are fixed by the human")
    sp.add_argument("--role", required=True, help="what the worker does, e.g. coder, reviewer, research")
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
    run = sub.add_parser("run", help="run a RAM-heavy or GPU job")
    run.add_argument("--ram", type=float, help=argparse.SUPPRESS)  # no longer granted; accepted from older habits
    run.add_argument("--vram", type=float, default=0, help="GB of GPU memory the job needs at its peak; "
                     "only a job given --vram sees a GPU")
    run.add_argument("--wait", type=float, default=30, help="minutes to wait for room in the RAM pool and for the "
                     "VRAM (max 30)")
    run.add_argument("--detach", action="store_true", help="run the job in the background, past the end of this "
                     "turn; its result comes back as a new message when it ends")
    run.add_argument("--job", help=argparse.SUPPRESS)  # the detached job's helper
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- command and its arguments")
    a = p.parse_args(argv)

    if a.cmd == "run":
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        if a.ram is not None and not a.job:
            print("loop run: --ram is ignored; the platform watches the shared RAM pool instead", file=sys.stderr)
        if a.job:
            sys.exit(job_helper(a.job, a.vram, a.wait, cmd))
        sys.exit((detach_job if a.detach else run_job)(a.vram, a.wait, cmd))
    if a.cmd in ("spawn", "send", "advise"):
        payload = {"prompt": text_arg(a, "prompt")}
        if a.cmd == "spawn":
            payload.update({"agent": a.agent, "role": a.role})
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
