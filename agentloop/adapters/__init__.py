"""Adapters translate the platform's session contract into Claude Code / Codex / Antigravity headless CLI calls."""
from .agy import AgyAdapter
from .base import Adapter, ParseState, RunSpec
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, Adapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "agy": AgyAdapter()}

__all__ = ["ADAPTERS", "Adapter", "ParseState", "RunSpec"]
