"""Scripted stand-in for Claude Code: emits stream-json and drives the platform through `loop`."""
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil


def emit(obj):
    print(json.dumps(obj), flush=True)


def loop(*args, stdin=None):
    out = subprocess.run(["loop", *args], input=stdin, capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


def _spec(gdir):
    return (gdir / "spec.md").read_text() if (gdir / "spec.md").exists() else ""


# Live input, as Claude Code with --input-format stream-json: user messages on stdin, one per line, which stays open
# while the turn runs; a session with a mailbox flag stands in for a CLI without it (plain prompt, then EOF)
LIVE = "LOOP_MAILBOX" not in os.environ
_buf = b""


def read_message(timeout=None):
    """One user message from stdin, echoed back as Claude Code does with --replay-user-messages; None at EOF or
    after `timeout` seconds without one."""
    global _buf
    while b"\n" not in _buf:
        if timeout is not None and not select.select([0], [], [], timeout)[0]:
            return None
        chunk = os.read(0, 65536)
        if not chunk:
            return None
        _buf += chunk
    line, _buf = _buf.split(b"\n", 1)
    msg = json.loads(line)["message"]
    emit({"type": "user", "message": msg, "isReplay": True})
    return msg["content"]


def main():
    resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
    sid = resume or str(uuid.uuid4())
    prompt = read_message() if LIVE else sys.stdin.read()
    role, gdir = os.environ.get("LOOP_ROLE"), Path(os.environ.get("LOOP_GOAL_DIR", "."))
    wt = Path(os.environ.get("LOOP_WS", ".")) / "wt" / os.environ.get("LOOP_GOAL", "")  # integration worktree
    emit({"type": "system", "subtype": "init", "session_id": sid})
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.25, "resetsAt": int(time.time()) + 3600}}}})
    # Every session runs in the workspace's shared venv
    venv = Path(os.environ["LOOP_WS"]) / ".venv"
    assert os.environ["VIRTUAL_ENV"] == str(venv) and str(venv / "bin") in os.environ["PATH"].split(os.pathsep)
    if role == "meta-advisor" and "ADVICE_BUILD" in _spec(gdir):
        # Builds a prototype in its own worktree and scores it with a detached job, whose result resumes it
        own = Path(os.environ["LOOP_WS"]) / "wt" / os.environ["LOOP_SESSION"]
        if "[System] The `loop run` job is done" in prompt:
            # The result names the job's log instead of quoting it
            log = re.search(r"^Output: (\S+)", prompt, re.M).group(1)
            assert resume and "proto score 42" in Path(log).read_text()
            text = "Prototype scored 42; merge it."
        else:
            (own / "proto.py").write_text("print('proto score 42')\n")
            out = subprocess.run(["loop", "run", "--", sys.executable, "-c",
                                  "import time; time.sleep(3); exec(open('proto.py').read())"],
                                 cwd=own, capture_output=True, text=True)
            assert "is running in the background" in out.stdout + out.stderr, out
            text = "Prototype is being scored."
            if "ADVICE_BUILD_WAIT" in _spec(gdir):  # works on until the job has ended within this turn
                time.sleep(4)
                text = "Prototype scored 42; merge it."
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        emit({"type": "result", "subtype": "success", "result": text, "session_id": sid})
        return
    if role == "meta-advisor":
        # Only a follow-up, or the platform after a usage limit, resumes its consultation
        assert (resume is not None) == ("Follow-up" in prompt or "cut off by the" in prompt)
        assert all((gdir / name).read_text() is not None for name in ("spec.md", "goal.md", "plan.md"))
        # ADVICE_LIMIT_SOON hits a limit once, which resets at once
        soon = "ADVICE_LIMIT_SOON" in _spec(gdir)
        if "ADVICE_LIMIT" in _spec(gdir) and not (soon and (gdir / ".limit-advisor").exists()):
            (gdir / ".limit-advisor").write_text("")
            text = f"Claude AI usage limit reached|{int(time.time()) + (1 if soon else 600)}"
            emit({"type": "result", "subtype": "success", "is_error": True, "result": text, "session_id": sid})
            return
        if "ADVICE_FAIL" in _spec(gdir):
            emit({"type": "result", "subtype": "error", "is_error": True, "result": "advisor failed on purpose"})
            return
        if "ADVICE_WRITE" in _spec(gdir):  # a shell write the guard does not see; the platform must report it
            (Path(os.environ["LOOP_WS"]) / "repo" / "advisor.txt").write_text("x\n")
        for cmd in ("advise", "note", "spawn", "send", "merge", "done", "stop"):
            # Hit the API directly, so CLI argument parsing cannot mask an authorization failure.
            from agentloop.cli import call
            try:
                call(cmd, {"text": "forbidden", "prompt": "forbidden"})
            except SystemExit as exc:
                assert "does not steer" in str(exc)
            else:
                raise AssertionError(f"advisor executed {cmd}")
        time.sleep(2 if "ADVICE_STOP" in _spec(gdir) else 0.5)
        text = "Keep the new split." if resume else "Consider a different validation split."
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        emit({"type": "result", "subtype": "success", "result": text, "session_id": sid,
              "usage": {"input_tokens": 10, "output_tokens": 5}})
        return
    # The first meta-work turn of a LIMIT goal (or worker turn of a WLIMIT one) hits a usage limit, as the CLI says it
    if "ADVICE" not in _spec(gdir) and "LIMIT" in prompt + _spec(gdir) and not (gdir / f".limit-{role}").exists() and \
            (role == "worker") == ("WLIMIT" in _spec(gdir)):
        (gdir / f".limit-{role}").write_text("")
        # The meta-work's limit resets at once so its goal can finish; a worker's lasts while the test checks it,
        # unless WLIMIT_SOON lets the platform resume the worker
        late = role == "worker" and "WLIMIT_SOON" not in _spec(gdir)
        text = f"Claude AI usage limit reached|{int(time.time()) + (600 if late else 1)}"
        emit({"type": "result", "subtype": "success", "is_error": True, "result": text, "session_id": sid})
        return
    if role == "worker":
        if "[Job " in prompt:  # resumed by the platform with the result of a detached job
            Path(".artifacts/job.txt").write_text(prompt)
            text = "**Result:** read the job result"
        elif "FAIL" in prompt:
            Path("broken.txt").write_text("partial\n")
            text = "**Result:** failed on purpose\n**Issues:** simulated failure"
        elif "MIDTURN" in prompt:
            # The meta-work's message sent while this turn runs comes on stdin after a tool call
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
            msg = read_message(timeout=60)
            Path(".artifacts").mkdir(exist_ok=True)
            Path(".artifacts/midturn.txt").write_text(msg or "")
            Path("hello.py").write_text('print("hello")\n')
            text = f"**Result:** created hello.py, then read: {msg}"
        elif "MIDHOOK" in prompt:
            # A CLI without live input takes it through its mailbox hook, run after a tool call while the flag exists
            flag = Path(os.environ["LOOP_MAILBOX"])
            for _ in range(300):
                if flag.exists():
                    break
                time.sleep(0.1)
            out = subprocess.run([sys.executable, "-m", "agentloop.mailbox"], capture_output=True, text=True,
                                 input=json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash"})).stdout
            Path(".artifacts").mkdir(exist_ok=True)
            Path(".artifacts/midhook.txt").write_text(out)
            Path(".artifacts/flag-after.txt").write_text(str(flag.exists()))
            Path("hello.py").write_text('print("hello")\n')
            text = "**Result:** created hello.py"
        else:
            Path("hello.py").write_text('print("hello")\n')
            # Kept outputs: collected and git-excluded by the platform, never committed
            Path(".artifacts").mkdir(exist_ok=True)
            Path(".artifacts/prompt.txt").write_text(prompt)
            (Path(os.environ["TMPDIR"]) / "scratch.txt").write_text("temporary\n")
            Path(".artifacts/tmpdir.txt").write_text(os.environ["TMPDIR"])
            if "TAMPER" in prompt:  # a write the guard does not see; the platform must revert it
                time.sleep(1)  # once the meta-work's kickoff turn has ended, so this worker's turn reverts it
                (gdir / "goal.md").write_text("- nothing to do\n")
            if "SLOW" in prompt:  # still running when the meta-work sends it a message
                time.sleep(1.5)
            if "RAMJOB" in prompt:
                # A tool process is put ahead of the CLI and `loop run` jobs for the OOM killer within a sample
                tool = subprocess.Popen(["sleep", "5"])
                time.sleep(2.5)
                adj = " ".join(Path(f"/proc/{pid}/oom_score_adj").read_text().strip() for pid in (os.getpid(), tool.pid))
                tool.kill()
                ok = loop("run", "--", sys.executable, "-c",
                          "import os, time; print('job ran, adj', open('/proc/self/oom_score_adj').read().strip(), "
                          "'cuda', repr(os.environ.get('CUDA_VISIBLE_DEVICES'))); time.sleep(2.5)")
                # Thread defaults unless the job's environment sets its own; the job's scope and the pool slice
                # carry their CPU weights ('-' without systemd)
                probe = ("import os, pathlib; cg = pathlib.Path('/sys/fs/cgroup' + open('/proc/self/cgroup')"
                         ".read().strip().split(':', 2)[2]); w = lambda p: (p / 'cpu.weight').read_text().strip() "
                         "if (p / 'cpu.weight').exists() else '-'; print('threads', os.environ.get('OMP_NUM_THREADS'), "
                         "os.environ.get('MKL_NUM_THREADS'), os.environ.get('OMP_WAIT_POLICY'), 'cpuw', w(cg), "
                         "w(cg.parent))")
                threads = subprocess.run(["loop", "run", "--", sys.executable, "-c", probe], capture_output=True,
                                         text=True, env={**os.environ, "OMP_NUM_THREADS": "3"}).stdout.strip()
                threads += "\n" + loop("run", "--", sys.executable, "-c", probe)
                # Only a job granted VRAM sees a GPU (refused on a host without one)
                gpu = loop("run", "--vram", "0.5", "--", sys.executable, "-c",
                           "import os; print('gpu job', os.environ.get('CUDA_VISIBLE_DEVICES'), "
                           "os.environ.get('LOOP_VRAM_GB'))")
                # An agent used to granting RAM is told the flag is gone; steering is refused to a worker
                old = loop("run", "--ram", "4", "--", "true")
                spawn = loop("spawn", "--agent", "fake", "--role", "x", "--prompt", "x")
                Path(".artifacts/ram.txt").write_text("\n---\n".join([adj, ok, old, spawn, gpu, threads]))
            if "DETACH" in prompt:
                # Both jobs outlast the wait of their `loop run`: one ends soon after this turn, through a `loop run`
                # of its own that has started before the turn ends and must not end with it; the other runs until
                # the goal closes
                out = [loop("run", "--", "loop", "run", "--", sys.executable, "-c",
                            "import os, pathlib, time; pathlib.Path('.artifacts/nested.txt').touch(); time.sleep(3); "
                            "print('detached job output', os.environ['TMPDIR'])"),
                       loop("run", "--", "sleep", "300")]
                for _ in range(100):
                    if Path(".artifacts/nested.txt").exists():
                        break
                    time.sleep(0.1)
                Path(".artifacts/detach.txt").write_text("\n---\n".join(out))
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "python hello.py"}}]}})
            emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "hello"}]}})
            text = "**Result:** created hello.py\n**Evidence:** python hello.py -> hello"
    elif role == "meta-work":
        if "ADVICE_BUILD" in _spec(gdir):
            # One consultation; its job-woken follow-up brings the prototype, which the meta-work merges
            if "[Kickoff]" in prompt:
                text = loop("advise", stdin="Prototype and score the idea.")
            elif m := re.search(r"\[Advice\] (ma-\w+)(?: · turn 2)? · completed\nPrototype scored 42", prompt):
                text = loop("merge", m.group(1))
                text += "\n" + loop("done", "--status", "success", stdin="Prototype merged.")
            else:
                text = "Waiting for the prototype's score."
        elif "ADVICE" in _spec(gdir):
            count = gdir / ".advice-count"
            n = int(count.read_text()) if count.exists() else 0
            if n < 2 and "[Budget exhausted" not in prompt:
                count.write_text(str(n + 1))
                # ADVICE_FOLLOW asks its second question within the first consultation
                if n == 1 and "ADVICE_FOLLOW" in _spec(gdir):
                    sid = re.search(r"\[Advice\] (ma-\w+)", prompt).group(1)
                    text = loop("advise", "--follow-up", sid, stdin="Follow-up: does the split hold?")
                else:
                    text = loop("advise", stdin=f"Question {n + 1}: what should we reconsider?")
                if "budget exhausted" in text or "read-only advisors require" in text:
                    text += "\n" + loop("done", "--status", "partial", stdin=text)
            else:
                assert "[Advice]" in prompt or "[Budget exhausted" in prompt
                text = loop("done", "--status", "success", stdin="Advice received and considered.")
        elif "DIRECT" in _spec(gdir):
            # Does the work itself over several turns without committing, then closes; a rejected success
            # (verify edited tracked files) falls back to partial
            counter = gdir / ".direct-turns"
            n = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(n))
            if n <= 3:
                (wt / f"step{n}.py").write_text(f"print({n})\n")
                text = f"wrote step{n}.py"
            else:
                text = loop("done", "--status", "success", stdin="- steps exist")
                if "REJECTED" in text:
                    text += "\n" + loop("done", "--status", "partial", stdin="verify edited files")
        elif "OFFBRANCH" in _spec(gdir):
            # Leaves the integration branch: spawn and done must refuse rather than lose the change
            subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=wt, check=True)
            (wt / "wip.py").write_text("x = 1\n")
            (Path(os.environ["LOOP_WS"]) / "repo" / "stray.txt").write_text("oops\n")
            text = loop("spawn", "--agent", "fake", "--role", "coder", "--prompt", "x")
            text += "\n" + loop("done", "--status", "success", stdin="- done")
            subprocess.run(["git", "checkout", "-q", f"loop/{os.environ['LOOP_GOAL']}/integration"], cwd=wt,
                           check=True)
            text += "\n" + loop("done", "--status", "partial", stdin="- wip only")
        elif "MIDTURN" in _spec(gdir) and "[Kickoff]" in prompt:
            # Steers the worker while it runs and takes its report in this same turn (mid-turn delivery)
            w = re.search(r"Spawned (w-\w+)", loop("spawn", "--agent", "fake", "--role", "coder", "--prompt",
                                                    "Create hello.py MIDTURN")).group(1)
            (gdir / ".midturn-send.txt").write_text(loop("send", w, "--prompt", "MIDMSG: mind the docstring"))
            (gdir / ".midturn-report.txt").write_text(read_message(timeout=60) or "")
            text = loop("merge", w) + "\n" + loop("done", "--status", "success", stdin="- hello.py exists")
        elif "MIDHOOK" in _spec(gdir) and "[Kickoff]" in prompt:
            w = re.search(r"Spawned (w-\w+)", loop("spawn", "--agent", "fakehook", "--role", "coder", "--prompt",
                                                    "Create hello.py MIDHOOK")).group(1)
            (gdir / ".midhook-send.txt").write_text(loop("send", w, "--prompt", "HOOKMSG: mind the docstring"))
            text = "waiting for the worker"
        elif "QUEUESEND" in _spec(gdir) and "[Kickoff]" in prompt:
            # Without mid-turn delivery, a message to a running worker starts its next turn
            w = re.search(r"Spawned (w-\w+)", loop("spawn", "--agent", "fake", "--role", "coder", "--prompt",
                                                    "Create hello.py SLOW")).group(1)
            (gdir / ".queue-send.txt").write_text(loop("send", w, "--prompt", "QMSG: second task"))
            text = "waiting for the worker"
        elif "QUEUESEND" in _spec(gdir) and "[Worker" in prompt and "· turn 2]" not in prompt:
            text = "waiting for its second turn"
        elif "CHECKIN" in _spec(gdir) and "[Kickoff]" in prompt:
            # Leaves a long job running and ends its turn: before the job ends, only a check-in wakes it
            text = loop("run", "--", "sleep", "299")
        elif "CHECKIN" in _spec(gdir) and "[Check-in]" in prompt:
            (gdir / ".checkin.txt").write_text(prompt)
            text = loop("done", "--status", "success", stdin="- the job was checked")
        elif "[Kickoff]" in prompt:
            (gdir / "plan.md").write_text("# Plan\n\n- [ ] create hello.py\n")
            task = "Create hello.py" + "".join(f" {k}" for k in ("TAMPER", "RAMJOB", "DETACH") if k in _spec(gdir))
            text = loop("spawn", "--agent", "fake", "--role", "coder", "--prompt", task)
        elif "DETACH" in _spec(gdir) and "[Worker" in prompt and "result of j-" not in prompt:
            # The worker left a job running: wait for the digest of the turn the platform starts with its result
            (gdir / ".ls.txt").write_text(loop("ls"))
            text = "waiting for the worker's job"
        elif "RETIRE" in _spec(gdir) and (m := re.search(r"\[Worker (w-\w+)", prompt)):
            # Retires the merged worker: its worktree goes, its branch stays, and it can no longer be sent
            w = m.group(1)
            out = [loop("merge", w), loop("retire", w), loop("send", w, "--prompt", "again"), loop("retire", w),
                   loop("ls")]
            (gdir / ".retire.txt").write_text("\n---\n".join(out))
            text = loop("done", "--status", "success", stdin="- hello.py exists: verified")
        elif m := re.search(r"\[Worker (w-\w+)", prompt):
            (wt / "meta_note.txt").write_text("written by the meta-work\n")  # uncommitted: the merge must keep it
            merged = loop("merge", m.group(1))
            (gdir / "plan.md").write_text("# Plan\n\n- [x] create hello.py\n")
            text = merged + "\n" + loop("done", "--status", "success", stdin="- hello.py exists: verified")
        elif "Human note" in prompt:
            text = "ack note"
        else:
            text = loop("done", "--status", "partial", stdin="nothing to do")
    else:
        if "CHATWRITE" in prompt:  # a shell write the guard does not see; the platform must report it
            (wt / "chat.txt").write_text("x\n")
        if "CHATWAIT" in prompt:  # the human's next message comes while this turn answers (mid-turn delivery)
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
            text = f"Also read: {read_message(timeout=20)}"
        else:
            status = loop("status")
            note = loop("note", "Consider adding a test")
            text = f"Status checked ({len(status)} chars). {note}"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}}})
    emit({"type": "result", "subtype": "success", "result": text, "session_id": sid, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 10, "output_tokens": 5}})


if __name__ == "__main__":
    main()
    # With live input, stdin stays open until the platform closes it: a message after the answer gets one more
    while LIVE and (more := read_message()) is not None:
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Noted: {more}"}]}})
        emit({"type": "result", "subtype": "success", "result": f"Noted: {more}"})
