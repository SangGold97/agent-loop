"""Paths and layered configuration: DEFAULTS < workspace loop.yaml < mission.yaml."""
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
    "limits": {"max_running_missions": 3, "max_workers_total": 6},
    "defaults": {
        # Fixed by the human: every meta-work turn and every worker turn uses these settings
        "meta_work": {"agent": "claude", "model": "opus", "effort": "high",
                      "autocompact": None, "rotate_at_tokens": None},
        "workers": {
            "claude": {"enabled": True, "model": "opus", "effort": "high", "autocompact": None},
            "codex": {"enabled": True, "model": "gpt-6-astra", "effort": "high", "autocompact": None},
        },
        "budget": {"max_concurrent_workers": 3, "max_worker_turns": 40, "max_meta_turns": 80},
        # Language of everything agents write for the human; code and commit messages follow the project
        "language": "English",
        "base_branch": None,
        "push": "manual",
    },
    "digest": {"max_output_words": 500, "max_diff_lines": 80},
    "infra": {"min_free_ram_gb": 2, "worker_memory_max_gb": 8, "gpu_slots": 1},
    "turn_timeout_min": 180,
    "guard": {"deny_patterns": []},
}

# Mission-level keys that override workspace defaults
MISSION_KEYS = ("meta_work", "workers", "budget", "language", "base_branch", "push")


def home() -> Path:
    return Path(os.environ.get("AGENTLOOP_HOME", PLATFORM_ROOT))


def workspaces_dir() -> Path:
    return home() / "workspaces"


def state_dir() -> Path:
    path = home() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def mission_config(ws: Path, mid: str) -> dict:
    """Effective config for a mission: workspace config with mission overrides in `defaults`."""
    cfg = workspace_config(ws)
    mission = load_yaml(ws / "missions" / mid / "mission.yaml")
    cfg["mission"] = deep_merge(cfg["defaults"], {k: mission[k] for k in MISSION_KEYS if k in mission})
    cfg["mission"]["title"] = mission.get("title", mid)
    return cfg
