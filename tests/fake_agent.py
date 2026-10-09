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
    emit({"type": "system", "subtype": "init", "session_id": sid})
    # The first meta-work turn of a LIMIT goal (or worker turn of a WLIMIT one) hits a usage limit, as the CLI says it
    if "LIMIT" in prompt + _spec(gdir) and not (gdir / f".limit-{role}").exists() and \
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
        if "[Kickoff]" in prompt:
            (gdir / "plan.md").write_text("# Plan\n\n- [ ] create hello.py\n")
            task = "Create hello.py" + (" TAMPER" if "TAMPER" in (gdir / "spec.md").read_text() else "")
            text = loop("spawn", "--agent", "fake", "--role", "coder", "--prompt", task)
        elif m := re.search(r"\[Worker (w-\w+)", prompt):
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
        status = loop("status")
        note = loop("note", "Consider adding a test")
        text = f"Status checked ({len(status)} chars). {note}"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}}})
    emit({"type": "result", "subtype": "success", "result": text, "session_id": sid, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 10, "output_tokens": 5}})


if __name__ == "__main__":
    main()
