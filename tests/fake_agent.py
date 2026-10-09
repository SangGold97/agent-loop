"""Scripted stand-in for Claude Code: emits stream-json and drives the platform through `loop`."""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


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
    wt = Path(os.environ.get("LOOP_WS", ".")) / "wt" / f"_int-{os.environ.get('LOOP_GOAL')}"  # integration worktree
    emit({"type": "system", "subtype": "init", "session_id": sid})
    if role == "meta-advisor":
        assert resume is None
        assert all((gdir / name).read_text() is not None for name in ("spec.md", "goal.md", "plan.md", "journal.md"))
        if "ADVICE_LIMIT" in _spec(gdir):
            text = f"Claude AI usage limit reached|{int(time.time()) + 600}"
            emit({"type": "result", "subtype": "success", "is_error": True, "result": text, "session_id": sid})
            return
        if "ADVICE_FAIL" in _spec(gdir):
            emit({"type": "result", "subtype": "error", "is_error": True, "result": "advisor failed on purpose"})
            return
        if "ADVICE_WRITE" in _spec(gdir):  # a shell write the guard does not see; the platform must report it
            (wt / "advisor.txt").write_text("x\n")
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
        if "FAIL" in prompt:
            Path("broken.txt").write_text("partial\n")
            text = "**Result:** failed on purpose\n**Issues:** simulated failure"
        else:
            Path("hello.py").write_text('print("hello")\n')
            # Kept outputs and a virtualenv: collected and git-excluded by the platform, never committed
            Path(".artifacts").mkdir(exist_ok=True)
            Path(".artifacts/prompt.txt").write_text(prompt)
            Path(".venv").mkdir(exist_ok=True)
            Path(".venv/pyvenv.cfg").write_text("home = /usr\n")
            if "TAMPER" in prompt:  # a write the guard does not see; the platform must revert it
                (gdir / "goal.md").write_text("- nothing to do\n")
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
            task = "Create hello.py" + (" TAMPER" if "TAMPER" in (gdir / "spec.md").read_text() else "")
            text = loop("spawn", "--agent", "fake", "--role", "coder", "--prompt", task)
        elif m := re.search(r"\[Worker (w-\w+)", prompt):
            (wt / "meta_note.txt").write_text("written by the meta-work\n")  # uncommitted: the merge must keep it
            merged = loop("merge", m.group(1))
            (gdir / "plan.md").write_text("# Plan\n\n- [x] create hello.py\n")
            with (gdir / "journal.md").open("a") as fh:
                fh.write(f"- merged {m.group(1)}\n")
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
