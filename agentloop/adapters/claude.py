"""Claude Code adapter: `claude -p --output-format stream-json`, prompt on stdin, resume by session id."""
from __future__ import annotations

from .base import Adapter, ParseState, RunSpec, short, to_tokens

TOOL_KEYS = ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt")


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
             "--dangerously-skip-permissions", "--setting-sources", "project,local"]
        if s.model:
            a += ["--model", s.model]
        if s.effort:
            a += ["--effort", s.effort]
        tokens = to_tokens(s.autocompact)
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
                    out.append({"type": "tool_result", "text": short(body or ""), "error": bool(c.get("is_error"))})
        elif kind == "result":
            st.vendor_id = o.get("session_id") or st.vendor_id
            st.final = o.get("result") if o.get("result") is not None else st.last_text
            st.cost = float(o.get("total_cost_usd") or 0)
            u = o.get("usage") or {}
            st.tokens = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens", "output_tokens"))
            st.is_error = bool(o.get("is_error")) or o.get("subtype") not in (None, "success")
            if st.is_error:
                st.error = str(o.get("subtype") or "error")
                out.append({"type": "error", "text": f"{st.error}: {short(st.final or '', 300)}"})
        return out
