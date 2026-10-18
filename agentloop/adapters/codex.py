"""Codex adapter: `codex exec --json` (or `exec resume <id>`), prompt on stdin, CODEX_HOME per layer."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Adapter, ParseState, RunSpec, billed_tokens, epoch, short, to_tokens, tool_result


def quota_windows(limits: dict) -> list[dict]:
    """A rollout's `rate_limits` (primary: the 5-hour window, secondary: the weekly one) as quota windows."""
    out = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if isinstance(w, dict) and w.get("used_percent") is not None:
            mins = w.get("window_minutes") or 0
            name = f"{mins // 1440}d" if mins and mins % 1440 == 0 else f"{round(mins / 60)}h"
            out.append({"name": name, "used": w["used_percent"] / 100, "resets_at": w.get("resets_at")})
    return out


class CodexAdapter(Adapter):
    name = "codex"
    binary = "codex"

    def argv(self, s: RunSpec) -> list[str]:
        a = ["codex", "exec"] + (["resume", s.resume_id] if s.resume_id else [])
        a += ["--json", "--skip-git-repo-check", "--dangerously-bypass-hook-trust"]
        if s.model:
            a += ["-m", s.model]
        if s.effort:
            a += ["-c", f'model_reasoning_effort="{s.effort}"']
        # Codex streams no reasoning summaries unless asked; with them its thinking shows in the feeds as Claude's does
        a += ["-c", 'model_reasoning_summary="auto"']
        tokens = to_tokens(s.autocompact)
        if tokens:
            a += ["-c", f"model_auto_compact_token_limit={tokens}"]
        a += ["--dangerously-bypass-approvals-and-sandbox"]
        if s.system_text:
            a += ["-c", f"developer_instructions={json.dumps(s.system_text)}"]
        if s.kind == "meta":
            # Codex reads AGENTS.md from the git root down to cwd; the meta's git root is the workspace root, so an
            # AGENTS.md there would reach Codex but not Claude (claudeMdExcludes). No markers: cwd is the root.
            a += ["-c", "project_root_markers=[]"]
        if not s.resume_id:
            a += ["-C", str(s.cwd)]
        if s.last_message_file:
            a += ["-o", str(s.last_message_file)]
        return a + ["-"]

    def env(self, s: RunSpec) -> dict:
        return {"CODEX_HOME": str(s.codex_home)} if s.codex_home else {}

    def parse(self, o: dict, st: ParseState) -> list[dict]:
        kind, out = o.get("type"), []
        item = o.get("item") or {}
        itype = item.get("type")
        if kind == "thread.started":
            st.vendor_id = o.get("thread_id")
            out.append({"type": "init", "vendor_id": st.vendor_id})
        elif kind == "item.started" and itype == "command_execution":
            out.append({"type": "tool", "name": "Bash", "detail": short(item.get("command", ""), 240)})
        elif kind == "item.completed":
            if itype == "agent_message" and item.get("text", "").strip():
                st.last_text = item["text"]
                out.append({"type": "text", "text": item["text"]})
            elif itype == "reasoning" and item.get("text"):
                # A summary opens with a bold title ("**Checking the tests**"): plain text, like Claude's thinking
                text = re.sub(r"^\*\*(.+?)\*\*", r"\1", item["text"].strip())
                out.append({"type": "thinking", "text": short(text, 400)})
            elif itype == "command_execution":
                code = item.get("exit_code")
                out.append(tool_result(item.get("aggregated_output") or f"exit {code}", code not in (0, None)))
            elif itype == "file_change":
                paths = ", ".join(c.get("path", "?") for c in item.get("changes") or [])
                out.append({"type": "tool", "name": "edit", "detail": short(paths, 240)})
            elif itype == "mcp_tool_call":
                out.append({"type": "tool", "name": f"{item.get('server')}.{item.get('tool')}",
                            "detail": short(item.get("arguments") or "", 240)})
            elif itype == "web_search":
                out.append({"type": "tool", "name": "web_search", "detail": short(item.get("query", ""), 240)})
            elif itype == "error" and "--dangerously-bypass-hook-trust" not in item.get("message", ""):
                # Codex reports the hook-trust flag we pass on purpose as an error item; it is only a notice
                out.append({"type": "error", "text": short(item.get("message", ""), 400)})
        elif kind == "turn.completed":
            u = o.get("usage") or {}
            # The thread's running total, not this turn's share: finish() narrows it down from the rollout
            st.tokens = billed_tokens(u)
            st.context = u.get("input_tokens") or st.context
            out.append({"type": "usage", "context": st.context})
        elif kind == "turn.failed":
            st.is_error = True
            st.error = short((o.get("error") or {}).get("message") or "turn failed", 400)
            out.append({"type": "error", "text": st.error})
        elif kind == "error":
            out.append({"type": "error", "text": short(o.get("message", ""), 400)})
        return out

    def finish(self, st: ParseState, s: RunSpec) -> None:
        vendor_id = st.vendor_id or s.resume_id
        if not (s.codex_home and vendor_id):
            return
        files = sorted(Path(s.codex_home, "sessions").rglob(f"rollout-*{vendor_id}.jsonl"))
        # The rollout logs the thread's running total after every model call, also for a turn stopped before
        # turn.completed: this turn's tokens are its growth since this run's first task started. The TUI's context
        # figure is the last call's size. Usage limits come only from the rollout too, with each token count.
        total = before = last = None
        in_turn = False
        for line in files[-1].read_text().splitlines() if files else []:
            if '"task_started"' not in line and '"token_count"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            payload = o.get("payload") or {}
            if payload.get("type") == "task_started" and not in_turn and epoch(o.get("timestamp")) >= st.started:
                in_turn, before = True, total
            elif payload.get("type") == "token_count":
                if payload.get("info"):
                    total = payload["info"].get("total_token_usage") or total
                    last = payload["info"].get("last_token_usage") or last
                st.quota = quota_windows(payload.get("rate_limits") or {}) or st.quota
        if last:
            st.context = last.get("total_tokens") or st.context
        if in_turn and total:
            st.tokens = billed_tokens(total) - (billed_tokens(before) if before else 0)

    def final_text(self, st: ParseState, s: RunSpec) -> str:
        if s.last_message_file and s.last_message_file.exists():
            text = s.last_message_file.read_text().strip()
            if text:
                return text
        return st.last_text
