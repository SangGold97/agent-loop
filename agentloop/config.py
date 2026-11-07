"""Paths and layered configuration: DEFAULTS < workspace loop.yaml < goal.yaml."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import yaml

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PLATFORM_ROOT / "templates"
PYTHON = sys.executable

DEFAULTS: dict = {
    "limits": {"max_running_goals": 3, "max_workers_total": 6},
    "defaults": {
        # Fixed by the human: every meta-work turn and every worker turn uses these settings
        "meta_work": {"agent": "claude", "model": "opus", "effort": "high",
                      "autocompact": None, "rotate_at_tokens": None, "chrome": False},
        "meta_advisor": {"agent": "claude", "model": "opus", "effort": "high", "autocompact": None},
        "workers": {
            "claude": {"enabled": True, "model": "opus", "effort": "high", "autocompact": None},
            "codex": {"enabled": True, "model": "gpt-6-astra", "effort": "high", "autocompact": None},
            "agy": {"enabled": False, "model": "gemini-4-argon", "effort": "high"},  # no autocompact setting
        },
        "budget": {"max_concurrent_workers": 3, "max_worker_turns": 40, "max_meta_turns": 80},
        # Language of everything agents write for the human; code and commit messages follow the project
        "language": "English",
        "base_branch": None,
        # Messages to a session in a turn reach it after its current tool call instead of waiting for the turn to end
        "mid_turn": False,
    },
    "infra": {"pool_reserve_gb": 3, "worker_memory_max_gb": None, "vram_reserve_gb": 1},
    "turn_timeout_min": 180,
    "job_timeout_h": 9,  # a `loop run` job is stopped after this many hours
    # Minutes every agent of a running goal may stay idle, with nothing queued for the meta-work, before the
    # meta-work gets a check-in to look at the work in flight (0: never)
    "idle_checkin_min": 30,
    "guard": {"deny_patterns": []},
    "mcp_servers": [],  # MCP server URLs Claude sessions may use; all others blocked
}

# Goal-level keys that override workspace defaults
GOAL_KEYS = ("meta_work", "meta_advisor", "workers", "budget", "language", "base_branch", "mid_turn")


def home() -> Path:
    return Path(os.environ.get("AGENTLOOP_HOME", PLATFORM_ROOT))


def workspaces_dir() -> Path:
    return home() / "workspaces"


def state_dir() -> Path:
    path = home() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mailbox(sid: str) -> Path:
    """The flag that tells a session's mailbox hook (CLIs without live input) that something waits for it."""
    return state_dir() / "mailbox" / sid


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a YAML mapping")
    return data


def workspace_config(ws: Path) -> dict:
    return deep_merge(DEFAULTS, load_yaml(ws / "loop.yaml"))


def goal_config(ws: Path, gid: str) -> dict:
    """Effective config for a goal: workspace config with goal overrides in `defaults`."""
    cfg = workspace_config(ws)
    goal = load_yaml(ws / "goals" / gid / "goal.yaml")
    cfg["goal"] = deep_merge(cfg["defaults"], {k: goal[k] for k in GOAL_KEYS if k in goal})
    cfg["goal"]["title"] = goal.get("title", gid)
    return cfg
