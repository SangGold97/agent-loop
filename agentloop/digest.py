"""Text the platform hands the meta-work: worker digests, the turn envelope, the budget line, plan checklists."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def trim_words(text: str, limit: int) -> tuple[str, bool]:
    text = text.strip()
    # Words at even indexes, the original whitespace between them at odd ones (keeps line breaks)
    parts = re.split(r"(\s+)", text) if text else []
    words = (len(parts) + 1) // 2
    if words <= limit:
        return text, False
    # Keep the tail too: final reports usually end with the most decision-relevant lines
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return "".join(parts[:2 * head - 1]) + "\n[…]\n" + "".join(parts[2 * (words - tail):]), True


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def worker_digest(sess: dict, n: int, res) -> str:
    """A finished worker turn as the meta-work receives it: which worker and turn, then the reply as is."""
    status = "stopped" if res.stopped else ("failed: " + res.error if res.error else "ok")
    return f"[Worker {sess['id']} · role {sess['role']} · turn {n}] {status}\n{res.final or '(no final message)'}"


def budget_line(b: dict) -> str:
    def lim(v):
        return "∞" if v in (None, 0) else v
    return (f"[Budget] running workers {b['running_workers']}/{lim(b['max_concurrent_workers'])} · "
            f"worker turns {b['worker_turns']}/{lim(b['max_worker_turns'])} · "
            f"meta turns {b['meta_turns']}/{lim(b['max_meta_turns'])} · active {b['hours']:.1f} h")


BUDGET_LOW = 0.8  # share of a turn budget past which every meta-work turn carries the [Budget] line


def budget_low(b: dict) -> bool:
    """Whether worker or meta turns are mostly spent, so the meta-work should see the budget with every turn."""
    return any(b.get(f"max_{k}") and b[k] >= BUDGET_LOW * b[f"max_{k}"] for k in ("worker_turns", "meta_turns"))


def envelope(items: list[dict], budget: str | None = None) -> str:
    parts = []
    for it in items:
        p = json.loads(it["payload"])
        kind = it["kind"]
        if p.get("until") and p["until"] < time.time():
            continue
        if kind in ("kickoff", "handoff", "system", "budget", "worker_done", "advice"):
            parts.append(p["text"])
        elif kind == "note":
            parts.append(f"[Human note #{p['id']}] {p['text']}")
        elif kind == "file_changed":
            parts.append(f"[{p['name']} changed by the human]")
    if budget:
        parts.append(budget)
    return "\n\n".join(parts) or "[System] No new events."


def verify_commands(goal: str) -> list[str]:
    """`verify:` lines in goal.md: `- verify: \\`make test\\`` -> make test."""
    cmds = []
    for line in goal.splitlines():
        m = re.match(r"^\s*(?:[-*]\s*)?verify:\s*(.+?)\s*$", line, re.I)
        if m:
            cmd = m.group(1)
            code = re.match(r"^`([^`]+)`", cmd)
            cmds.append(code.group(1) if code else cmd)
    return cmds


def checklist(plan: str) -> tuple[int, int]:
    done = len(re.findall(r"^\s*[-*] \[[xX]\]", plan, re.M))
    todo = len(re.findall(r"^\s*[-*] \[ \]", plan, re.M))
    return done, done + todo


def write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)
