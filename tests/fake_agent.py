"""Scripted stand-in for Claude Code: emits stream-json and drives the platform through `loop`."""
import json
import os
import re
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


def main():
    resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
    sid = resume or str(uuid.uuid4())
    prompt = sys.stdin.read()
    role, gdir = os.environ.get("LOOP_ROLE"), Path(os.environ.get("LOOP_GOAL_DIR", "."))
    wt = Path(os.environ.get("LOOP_WS", ".")) / "wt" / os.environ.get("LOOP_GOAL", "")  # integration worktree
    emit({"type": "system", "subtype": "init", "session_id": sid})
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.25, "resetsAt": int(time.time()) + 3600}}}})
    # Every session runs in the workspace's shared venv
    venv = Path(os.environ["LOOP_WS"]) / ".venv"
    assert os.environ["VIRTUAL_ENV"] == str(venv) and str(venv / "bin") in os.environ["PATH"].split(os.pathsep)
    if role == "meta-advisor":
        assert resume is None
        assert all((gdir / name).read_text() is not None for name in ("spec.md", "goal.md", "plan.md"))
        if "ADVICE_LIMIT" in _spec(gdir):
            text = f"Claude AI usage limit reached|{int(time.time()) + 600}"
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
                assert "read-only" in str(exc)
            else:
                raise AssertionError(f"advisor executed {cmd}")
        time.sleep(2 if "ADVICE_STOP" in _spec(gdir) else 0.5)
        text = "Consider a different validation split."
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        emit({"type": "result", "subtype": "success", "result": text, "session_id": sid,
              "usage": {"input_tokens": 10, "output_tokens": 5}})
        return
    # The first meta-work turn of a LIMIT goal (or worker turn of a WLIMIT one) hits a usage limit, as the CLI says it
    if "ADVICE" not in _spec(gdir) and "LIMIT" in prompt + _spec(gdir) and not (gdir / f".limit-{role}").exists() and \
            (role == "worker") == ("WLIMIT" in _spec(gdir)):
        (gdir / f".limit-{role}").write_text("")
        # The meta-work's limit resets at once so its goal can finish; a worker's lasts while the test checks it
        text = f"Claude AI usage limit reached|{int(time.time()) + (600 if role == 'worker' else 1)}"
        emit({"type": "result", "subtype": "success", "is_error": True, "result": text, "session_id": sid})
        return
    if role == "worker":
        if "[Job " in prompt:  # resumed by the platform with the result of a detached job
            Path(".artifacts/job.txt").write_text(prompt)
            text = "**Result:** read the job result"
        elif "FAIL" in prompt:
            Path("broken.txt").write_text("partial\n")
            text = "**Result:** failed on purpose\n**Issues:** simulated failure"
        else:
            Path("hello.py").write_text('print("hello")\n')
            # Kept outputs: collected and git-excluded by the platform, never committed
            Path(".artifacts").mkdir(exist_ok=True)
            Path(".artifacts/prompt.txt").write_text(prompt)
            (Path(os.environ["TMPDIR"]) / "scratch.txt").write_text("temporary\n")
            Path(".artifacts/tmpdir.txt").write_text(os.environ["TMPDIR"])
            if "TAMPER" in prompt:  # a write the guard does not see; the platform must revert it
                (gdir / "goal.md").write_text("- nothing to do\n")
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
                # The job of a killed `loop run` helper is stopped with it, not left running unwatched
                helper = subprocess.Popen(["loop", "run", "--", "sleep", "30"],
                                          stderr=subprocess.PIPE, text=True)
                helper.stderr.readline()
                time.sleep(0.5)
                job = psutil.Process(helper.pid).children()[0]
                helper.kill()
                helper.wait()
                try:
                    job.wait(timeout=10)
                    orphan = "job stopped"
                except psutil.TimeoutExpired:
                    orphan = "job still running"
                Path(".artifacts/ram.txt").write_text("\n---\n".join([adj, ok, old, spawn, orphan, gpu, threads]))
            if "DETACH" in prompt:
                # One job ends soon after this turn, through a `loop run` of its own that has started before the turn
                # ends and must not end with it; the other runs until the goal closes
                out = [loop("run", "--detach", "--", "loop", "run", "--", sys.executable, "-c",
                            "import os, pathlib, time; pathlib.Path('.artifacts/nested.txt').touch(); time.sleep(3); "
                            "print('detached job output', os.environ['TMPDIR'])"),
                       loop("run", "--detach", "--", "sleep", "300")]
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
        if "ADVICE" in _spec(gdir):
            count = gdir / ".advice-count"
            n = int(count.read_text()) if count.exists() else 0
            if n < 2 and "[Budget exhausted" not in prompt:
                count.write_text(str(n + 1))
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
        status = loop("status")
        note = loop("note", "Consider adding a test")
        text = f"Status checked ({len(status)} chars). {note}"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}}})
    emit({"type": "result", "subtype": "success", "result": text, "session_id": sid, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 10, "output_tokens": 5}})


if __name__ == "__main__":
    main()
