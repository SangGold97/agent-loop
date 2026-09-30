"""Scripted stand-in for Claude Code: emits stream-json and drives the platform through `loop`."""
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path


def emit(obj):
    print(json.dumps(obj), flush=True)


def loop(*args, stdin=None):
    out = subprocess.run(["loop", *args], input=stdin, capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


def main():
    resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
    sid = resume or str(uuid.uuid4())
    prompt = sys.stdin.read()
    role, mdir = os.environ.get("LOOP_ROLE"), Path(os.environ.get("LOOP_MISSION_DIR", "."))
    emit({"type": "system", "subtype": "init", "session_id": sid})
    if role == "worker":
        if "FAIL" in prompt:
            Path("broken.txt").write_text("partial\n")
            text = "**Result:** failed on purpose\n**Issues:** simulated failure"
        else:
            Path("hello.py").write_text('print("hello")\n')
            (Path(os.environ["LOOP_RUN_DIR"]) / "artifacts" / "log.txt").write_text("ran hello\n")
            text = "**Result:** created hello.py\n**Evidence:** python hello.py -> hello"
    elif role == "meta-work":
        if "[Kickoff]" in prompt:
            (mdir / "plan.md").write_text("# Plan\n\n- [ ] create hello.py\n")
            text = loop("spawn", "--agent", "fake", "--role", "coder", "--label", "hello", "--prompt", "Create hello.py")
        elif m := re.search(r"\[Worker (w-\w+)", prompt):
            merged = loop("merge", m.group(1))
            (mdir / "plan.md").write_text("# Plan\n\n- [x] create hello.py\n")
            with (mdir / "journal.md").open("a") as fh:
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
