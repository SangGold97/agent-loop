"""Adapters translate the platform's session contract into Claude Code / Codex headless CLI calls."""
from .base import Adapter, ParseState, RunSpec
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, Adapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}

__all__ = ["ADAPTERS", "Adapter", "ParseState", "RunSpec"]
