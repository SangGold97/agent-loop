"""Session contract shared by all adapters.

Normalized events emitted while a turn runs:
  init {vendor_id} · text {text} · thinking {text} · tool {name, detail}
  tool_result {text, error, tail?} · usage {context} · error {text}
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunSpec:
    agent: str
    kind: str                      # meta (meta-work, meta-chat) | worker
    cwd: Path
    prompt: str
    model: str | None = None
    effort: str | None = None
    autocompact: str | int | None = None
    resume_id: str | None = None
    env: dict = field(default_factory=dict)
    add_dirs: list[Path] = field(default_factory=list)       # extra readable/writable dirs
    settings_file: Path | None = None                         # Claude: hooks/settings
    plugin_dir: Path | None = None                            # Claude: worker harness plugin
    append_file: Path | None = None                           # Claude: extra system prompt file
    system_text: str | None = None                            # mode instructions
    codex_home: Path | None = None
    last_message_file: Path | None = None


@dataclass
class ParseState:
    vendor_id: str | None = None
    last_text: str = ""
    final: str | None = None
    tokens: int = 0                # this turn's tokens, cached input excluded
    context: int = 0
    is_error: bool = False
    error: str = ""
    started: float = field(default_factory=time.time)        # when the turn started
    call_usage: list[dict] = field(default_factory=list)     # usage of each model call so far in this turn


def billed_tokens(usage: dict) -> int:
    """Tokens of one usage record except input served from the prompt cache, like the CLIs' 'tokens used'."""
    if "cached_input_tokens" in usage:  # Codex: input_tokens includes the cached part
        return (usage.get("input_tokens") or 0) - (usage.get("cached_input_tokens") or 0) \
            + (usage.get("output_tokens") or 0)
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))


class Adapter:
    name = "base"
    binary = ""

    def argv(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError

    def env(self, spec: RunSpec) -> dict:
        return {}

    def parse(self, obj: dict, st: ParseState) -> list[dict]:
        raise NotImplementedError

    def final_text(self, st: ParseState, spec: RunSpec) -> str:
        return st.final if st.final is not None else st.last_text

    def finish(self, st: ParseState, spec: RunSpec) -> None:
        """Last look at CLI-side state once the process has exited (no-op by default)."""


# Plan/quota exhaustion as the CLIs word it ("You've hit your usage limit ... try again at 9:45 PM",
# "Claude AI usage limit reached|<epoch>", "You've hit your limit · resets 9pm"); a plain 429 rate limit is not one
USAGE_LIMIT_RE = re.compile(r"usage limit|hit your (?:usage )?limit|limit reached", re.I)


def usage_limit_until(text: str, now: float | None = None) -> float | None:
    """When `text` reports a usage limit, the epoch at which it resets (an hour ahead if unstated), else None.

    A reset read too early only costs one more refused attempt, so the parse stays simple."""
    if not text or not USAGE_LIMIT_RE.search(text):
        return None
    now = time.time() if now is None else now
    if m := re.search(r"\|(\d{10})\b", text):
        return float(m.group(1))
    if m := re.search(r"\bin (\d+) (minute|hour|day)s?\b", text, re.I):
        return now + int(m.group(1)) * {"minute": 60, "hour": 3600, "day": 86400}[m.group(2).lower()]
    if m := re.search(r"(?:at|resets?)\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", text, re.I):
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        t = time.localtime(now)
        at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, int(m.group(2) or 0), 0, 0, 0, -1))
        return at if at > now else at + 86400
    return now + 3600


def short(text, limit: int = 600) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …"


def tool_result(body, error: bool, limit: int = 600) -> dict:
    """A tool_result event; long outputs also keep their end, where test runners and builds print the outcome."""
    body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    evt = {"type": "tool_result", "text": short(body, limit), "error": error}
    if len(body) > limit:
        evt["tail"] = "… " + body[-limit:]
    return evt


def to_tokens(value) -> int | None:
    """'400k' / '1M' / 400000 -> int tokens."""
    if value in (None, "", "auto"):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmM]?)\s*", str(value))
    if not m:
        raise ValueError(f"bad token count: {value!r}")
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)
