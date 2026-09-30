"""Codex adapter: `codex exec --json` (or `exec resume <id>`), prompt on stdin, CODEX_HOME per layer."""
from __future__ import annotations

import json

from .base import Adapter, ParseState, RunSpec, short, to_tokens



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
        tokens = to_tokens(s.autocompact)
        if tokens:
            a += ["-c", f"model_auto_compact_token_limit={tokens}"]
        if s.kind == "worker":
            a += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            # Meta-work/meta-chat: sandboxed writes (cwd + mission dirs), network on so `loop` reaches the API
            roots = json.dumps([str(d) for d in s.add_dirs])
            a += ["-c", 'sandbox_mode="workspace-write"', "-c", f"sandbox_workspace_write.writable_roots={roots}",
                  "-c", "sandbox_workspace_write.network_access=true"]
        if s.system_text:
            a += ["-c", f"developer_instructions={json.dumps(s.system_text)}"]
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
                out.append({"type": "thinking", "text": short(item["text"], 400)})
            elif itype == "command_execution":
                code = item.get("exit_code")
                out.append({"type": "tool_result", "text": short(item.get("aggregated_output") or f"exit {code}"),
                            "error": code not in (0, None)})
            elif itype == "file_change":
                paths = ", ".join(c.get("path", "?") for c in item.get("changes") or [])
                out.append({"type": "tool", "name": "edit", "detail": short(paths, 240)})
            elif itype == "mcp_tool_call":
                out.append({"type": "tool", "name": f"{item.get('server')}.{item.get('tool')}",
                            "detail": short(item.get("arguments") or "", 240)})
            elif itype == "web_search":
                out.append({"type": "tool", "name": "web_search", "detail": short(item.get("query", ""), 240)})
            elif itype == "error":
                out.append({"type": "error", "text": short(item.get("message", ""), 400)})
        elif kind == "turn.completed":
            u = o.get("usage") or {}
            st.tokens += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
            st.context = u.get("input_tokens") or st.context
            out.append({"type": "usage", "context": st.context})
        elif kind == "turn.failed":
            st.is_error = True
            st.error = short((o.get("error") or {}).get("message") or "turn failed", 400)
            out.append({"type": "error", "text": st.error})
        elif kind == "error":
            out.append({"type": "error", "text": short(o.get("message", ""), 400)})
        return out

    def final_text(self, st: ParseState, s: RunSpec) -> str:
        if s.last_message_file and s.last_message_file.exists():
            text = s.last_message_file.read_text().strip()
            if text:
                return text
        return st.last_text
