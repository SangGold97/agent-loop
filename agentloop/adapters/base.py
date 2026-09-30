"""Session contract shared by all adapters.

Normalized events emitted while a turn runs:
  init {vendor_id} · text {text} · thinking {text} · tool {name, detail}
  tool_result {text, error} · usage {context} · error {text}
"""
from __future__ import annotations

import json
import re
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
    tokens: int = 0
    cost: float = 0.0
    context: int = 0
    is_error: bool = False
    error: str = ""


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


def short(text, limit: int = 600) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …"


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
