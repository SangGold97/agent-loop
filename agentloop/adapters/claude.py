"""Claude Code adapter: `claude -p --output-format stream-json`, prompt on stdin, resume by session id."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .base import Adapter, ParseState, RunSpec, billed_tokens, short, to_tokens, tool_result

USER_SETTINGS = Path.home() / ".claude" / "settings.json"

TOOL_KEYS = ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt")
WINDOW_NAMES = (("five_hour", "5h"), ("seven_day", "7d"))


def user_autocompact() -> int | None:
    """The human's own Claude Code auto-compact window (sessions skip user settings, so pass it on)."""
    try:
        cfg = json.loads(USER_SETTINGS.read_text())
    except (OSError, ValueError):
        return None
    if cfg.get("autoCompactEnabled") is False:
        return None
    return to_tokens(cfg.get("autoCompactWindow"))


def tool_detail(inp: dict) -> str:
    for key in TOOL_KEYS:
        if inp.get(key):
            return short(str(inp[key]), 240)
    return short(inp, 240)


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"

    def argv(self, s: RunSpec) -> list[str]:
        a = ["claude", "-p", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--dangerously-skip-permissions", "--setting-sources", "project,local"]
        if s.model:
            a += ["--model", s.model]
        if s.effort:
            a += ["--effort", s.effort]
        tokens = to_tokens(s.autocompact) or user_autocompact()
        if tokens:
            a += ["--autocompact", str(tokens)]
        if s.resume_id:
            a += ["--resume", s.resume_id]
        if s.settings_file:
            a += ["--settings", str(s.settings_file)]
        if s.plugin_dir:
            a += ["--plugin-dir", str(s.plugin_dir)]
        if s.append_file:
            a += ["--append-system-prompt-file", str(s.append_file)]
        elif s.system_text:
            a += ["--append-system-prompt", s.system_text]
        for d in s.add_dirs:
            a += ["--add-dir", str(d)]
        return a

    def parse(self, o: dict, st: ParseState) -> list[dict]:
        kind, out = o.get("type"), []
        if kind == "system" and o.get("subtype") == "init":
            st.vendor_id = o.get("session_id") or st.vendor_id
            out.append({"type": "init", "vendor_id": st.vendor_id, "model": o.get("model")})
        elif kind == "rate_limit_event":
            # The plan's usage windows, utilization as a fraction: five_hour, seven_day and per-model ones
            info = o.get("rate_limit_info") or {}
            if info.get("status") == "rejected" and info.get("resetsAt"):
                st.limit_reset = float(info["resetsAt"])
            windows = info.get("unifiedWindows") or {}
            quota = []
            for key, w in windows.items():
                if isinstance(w, dict) and w.get("utilization") is not None:
                    name = key
                    for long, brief in WINDOW_NAMES:
                        name = name.replace(long, brief)
                    quota.append({"name": name.replace("_", " "), "used": float(w["utilization"]),
                                  "resets_at": w.get("resetsAt")})
            if quota:
                st.quota, st.quota_ts = quota, time.time()
        elif kind == "stream_event":
            # Token deltas of the main agent's reply: live preview only (the full message follows)
            ev = o.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta" \
                    and not o.get("parent_tool_use_id"):
                out.append({"type": "delta", "text": delta.get("text", "")})
            # The main agent's tokens call by call, so a turn stopped before `result` still counts what it used:
            # message_start opens a model call, message_delta carries that call's final usage
            if not o.get("parent_tool_use_id"):
                if ev.get("type") == "message_start":
                    st.call_usage.append((ev.get("message") or {}).get("usage") or {})
                elif ev.get("type") == "message_delta" and ev.get("usage") and st.call_usage:
                    st.call_usage[-1] = {**st.call_usage[-1], **ev["usage"]}
                st.tokens = sum(billed_tokens(u) for u in st.call_usage)
        elif kind == "assistant":
            msg = o.get("message") or {}
            usage = msg.get("usage") or {}
            if usage and not o.get("parent_tool_use_id"):
                st.context = sum(usage.get(k) or 0 for k in
                                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                out.append({"type": "usage", "context": st.context})
            for c in msg.get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    if not o.get("parent_tool_use_id"):
                        st.last_text = c["text"]
                    out.append({"type": "text", "text": c["text"], "sub": bool(o.get("parent_tool_use_id"))})
                elif c.get("type") == "tool_use":
                    out.append({"type": "tool", "name": c.get("name"), "detail": tool_detail(c.get("input") or {})})
                elif c.get("type") == "thinking" and c.get("thinking"):
                    out.append({"type": "thinking", "text": short(c["thinking"], 400)})
        elif kind == "user":
            for c in (o.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    if isinstance(body, list):
                        body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
                    out.append(tool_result(body or "", bool(c.get("is_error"))))
        elif kind == "result":
            st.vendor_id = o.get("session_id") or st.vendor_id
            st.final = o.get("result") if o.get("result") is not None else st.last_text
            # The CLI's own total for the turn replaces the per-call count
            st.tokens = billed_tokens(o.get("usage") or {})
            st.is_error = bool(o.get("is_error")) or o.get("subtype") not in (None, "success")
            if st.is_error:
                st.error = str(o.get("subtype") or "error")
                # The refusal's wording changes between CLI versions; its error code and reset time do not
                if o.get("api_error") == "usage_limit_reached":
                    st.error = "usage limit reached" + (f"|{int(st.limit_reset)}" if st.limit_reset else "")
                out.append({"type": "error", "text": f"{st.error}: {short(st.final or '', 300)}"})
        return out
