"""Compact messages for the meta-work: inline what is small, point to files for what is large."""
from __future__ import annotations

import json
import re
from pathlib import Path


def trim_words(text: str, limit: int) -> tuple[str, bool]:
    words = text.split()
    if len(words) <= limit:
        return text.strip(), False
    # Keep the tail too: final reports usually end with the most decision-relevant lines
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return " ".join(words[:head]) + "\n[…]\n" + " ".join(words[-tail:]), True


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def worker_digest(sess: dict, n: int, res, stat: str, patch: str, paths: dict, cfg: dict) -> str:
    status = "stopped" if res.stopped else ("failed: " + res.error if res.error else "ok")
    head = (f"[Worker {sess['id']} · role {sess['role']} · label {sess['label']} · turn {n}] "
            f"{status} · {res.duration / 60:.1f} min · {fmt_tokens(res.tokens)} tokens"
            + (f" · ${res.cost:.2f}" if res.cost else "")
            + (f" · context ~{fmt_tokens(res.context)}" if res.context else ""))
    out, cut = trim_words(res.final or "(no final message)", cfg["max_output_words"])
    parts = [head, f"[Final output]{' (trimmed; full: ' + paths['out'] + ')' if cut else ''}\n{out}"]
    if stat:
        lines = patch.splitlines()
        shown = "\n".join(lines[:cfg["max_diff_lines"]])
        more = f" (showing {cfg['max_diff_lines']}/{len(lines)} lines; full: {paths['diff']})" \
            if len(lines) > cfg["max_diff_lines"] else ""
        parts.append(f"[Diff] {stat.splitlines()[-1].strip()}{more}\n{stat}\n```diff\n{shown}\n```")
    else:
        parts.append("[Diff] no code changes")
    return "\n".join(parts)


def budget_line(b: dict) -> str:
    def lim(v):
        return "∞" if v in (None, 0) else v
    return (f"[Budget] running workers {b['running_workers']}/{lim(b['max_concurrent_workers'])} · "
            f"worker turns {b['worker_turns']}/{lim(b['max_worker_turns'])} · "
            f"meta turns {b['meta_turns']}/{lim(b['max_meta_turns'])} · active {b['hours']:.1f} h")


def envelope(items: list[dict], budget: str, infra: str) -> str:
    parts = []
    for it in items:
        p = json.loads(it["payload"])
        kind = it["kind"]
        if kind in ("kickoff", "handoff", "system", "budget", "worker_done"):
            parts.append(p["text"])
        elif kind == "note":
            parts.append(f"[Human note #{p['id']}] {p['text']}")
        elif kind == "file_changed":
            parts.append(f"[{p['name']} changed by the human]\n```diff\n{p['diff'][-4000:]}\n```")
    parts += [budget, f"[Infra] {infra}", "(Act, update plan.md/journal.md, then end your turn.)"]
    return "\n\n".join(parts)


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
