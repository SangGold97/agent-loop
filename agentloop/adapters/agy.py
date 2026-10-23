"""Antigravity CLI adapter: `agy --input-format stream-json` (the prompt as one NDJSON message on stdin), resume by
conversation id. Sessions run agy's default agent as the TUI does; the guard reaches them through a plugin
(workspace.agy_plugin) and the layer's instructions through the prompt."""
from __future__ import annotations

import json
import subprocess

from .base import Adapter, ParseState, RunSpec, epoch, short, tool_result

TOOL_KEYS = ("CommandLine", "TargetFile", "AbsolutePath", "Url", "Query", "SearchPath", "DirectoryPath", "toolSummary")


def billed(usage: dict) -> int:
    """Tokens of one model call except input served from the cache (agy reports that part apart, as cache_read)."""
    return (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)


def tool_detail(params: dict) -> str:
    for key in TOOL_KEYS:
        if params.get(key):
            return short(str(params[key]), 240)
    return short(params, 240)


class AgyAdapter(Adapter):
    name = "agy"
    binary = "agy"

    def argv(self, s: RunSpec) -> list[str]:
        a = ["agy", "--output-format", "stream-json", "--input-format", "stream-json", "--dangerously-skip-permissions"]
        if s.model:
            a += ["--model", s.model]
        if s.effort:
            a += ["--effort", s.effort]
        if s.resume_id:
            a += ["--conversation", s.resume_id]
        return a

    def stdin(self, s: RunSpec) -> str:
        # agy has no system-prompt flag: the instructions lead the turn's message, as the meta harness expects
        text = f"<instructions>\n{s.system_text.strip()}\n</instructions>\n\n{s.prompt}" if s.system_text else s.prompt
        return json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n"

    def parse(self, o: dict, st: ParseState) -> list[dict]:
        kind, out = o.get("event"), []
        if kind == "init":
            st.vendor_id = o.get("conversation_id") or st.vendor_id
            out.append({"type": "init", "vendor_id": st.vendor_id, "model": (o.get("init") or {}).get("model")})
        elif kind == "step_update":
            su = o.get("step_update") or {}
            step, done, stype = su.get("step_index"), su.get("state") != "ACTIVE", su.get("step_type")
            if stype == "agent_response":
                # A reply streams as text deltas of one step; the step's end carries the model call's usage
                delta = su.get("text_delta") or ""
                st.steps[step] = text = st.steps.get(step, "") + delta
                if delta and not done:
                    out.append({"type": "delta", "text": delta})
                if done and text.strip():
                    st.last_text = text.strip()
                    out.append({"type": "text", "text": st.last_text})
                if done and su.get("usage"):
                    # result.usage counts the whole conversation, so this turn's tokens are summed call by call
                    st.call_usage.append(su["usage"])
                    st.tokens = sum(billed(u) for u in st.call_usage)
                    st.context = (su["usage"].get("input_tokens") or 0) + (su["usage"].get("cache_read_tokens") or 0)
                    out.append({"type": "usage", "context": st.context})
            elif stype == "tool":
                info = su.get("tool_info") or {}
                if step not in st.steps:  # a tool step may report progress more than once
                    st.steps[step] = True
                    out.append({"type": "tool", "name": su.get("tool_name") or info.get("name"),
                                "detail": tool_detail(info.get("parameters") or {})})
                if done:
                    err = info.get("error") or {}
                    out.append(tool_result(info.get("output") or err.get("message") or "", bool(err)))
        elif kind == "result":
            r = o.get("result") or {}
            st.vendor_id = r.get("conversation_id") or st.vendor_id
            st.final = (r.get("response") or "").strip() or st.last_text
            if r.get("status") != "SUCCESS":
                st.is_error = True
                st.error = short(r.get("error") or str(r.get("status") or "error"), 400)
                out.append({"type": "error", "text": st.error})
        return out

    def finish(self, st: ParseState, s: RunSpec) -> None:
        # agy streams no usage limits, so they are read once the turn has ended
        st.quota = usage_quota(s.cwd) or st.quota


def usage_quota(cwd) -> list[dict]:
    """agy's usage windows from its /usage command, which makes no model call; [] when it cannot be read. A bucket
    at 0 left omits remaining_fraction (protobuf JSON), so a missing one is fully used."""
    try:
        out = subprocess.run(["agy", "-p", "/usage", "--output-format", "json"], cwd=cwd, capture_output=True,
                             text=True, timeout=30).stdout
        groups = json.loads(out)["command"]["data"]["groups"]
        return [{"name": f"{g['name']} {b['window']}", "used": 1 - float(b.get("remaining_fraction") or 0),
                 "resets_at": epoch(b.get("reset_time")) or None}
                for g in groups for b in g.get("buckets") or []]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return []
