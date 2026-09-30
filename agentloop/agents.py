"""Agent CLI management: versions, updates, and adapter smoke tests."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path

from . import gitops
from .adapters import ADAPTERS, RunSpec
from .config import state_dir
from .runner import Runner, child_env

UPDATE_CMD = {"claude": ["claude", "update"], "codex": ["codex", "update"]}
SMOKE_MODEL = {"claude": ("haiku", None), "codex": (None, "low")}


async def run_cmd(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    if not shutil.which(argv[0]):
        return 127, f"{argv[0]} not found"
    proc = await asyncio.create_subprocess_exec(*argv, env=child_env({}), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, out.decode(errors="replace").strip()


async def versions() -> dict:
    out = {}
    for name in UPDATE_CMD:
        code, text = await run_cmd([name, "--version"])
        out[name] = text.splitlines()[0] if code == 0 and text else f"unavailable ({text[:120]})"
    return out


async def smoke(name: str) -> dict:
    """One tiny real turn through the adapter; proves flags, auth and stream parsing still work."""
    cwd = state_dir() / "smoke" / name
    if not (cwd / ".git").exists():
        cwd.mkdir(parents=True, exist_ok=True)
        gitops.git(cwd, "init", "-q")
    model, effort = SMOKE_MODEL[name]
    spec = RunSpec(agent=name, kind="worker", cwd=cwd, prompt="Reply with exactly: OK", model=model, effort=effort,
                   last_message_file=cwd / "last.txt" if name == "codex" else None)
    started = time.time()

    async def ignore(_evt):
        return None

    res = await Runner().run(f"smoke-{name}", spec, raw_log=cwd / "raw.jsonl", event_log=cwd / "events.jsonl",
                             on_event=ignore, timeout_s=300)
    ok = "OK" in (res.final or "") and not res.error
    return {"ok": ok, "final": (res.final or "")[:200], "error": res.error[:400], "seconds": round(time.time() - started, 1),
            "session": res.vendor_id}


def _ver(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3])


def _claude_catalog(version: str) -> list[dict]:
    """Models from Claude Code's own catalog cache (refreshed by the CLI whenever it runs)."""
    files = sorted(Path.home().glob(".claude/cache/model-catalog/*-cc.json"), key=lambda f: f.stat().st_mtime,
                   reverse=True)
    for f in files:
        try:
            models = json.loads(f.read_text())["catalog"]["config"]["models"]
        except (ValueError, KeyError, TypeError):
            continue
        out = []
        for m in models:
            if m.get("min_claude_code_version") and _ver(m["min_claude_code_version"]) > _ver(version):
                continue
            efforts = [e["id"] for e in (m.get("thinking") or {}).get("effort_options", [])]
            out.append({"id": m["id"], "name": m.get("name") or m["id"], "efforts": efforts,
                        "main": m.get("section") == "main", "short": (m.get("short_name") or "").lower()})
        # Aliases (opus, sonnet, ...) always resolve to the newest model of that family
        aliases = [{"id": m["short"], "name": f"{m['short']} → {m['id']}", "efforts": m["efforts"], "alias": True}
                   for m in out if m["main"] and m["short"]]
        return aliases + [{k: m[k] for k in ("id", "name", "efforts")} for m in out]
    return []


async def _codex_catalog() -> list[dict]:
    code, text = await run_cmd(["codex", "debug", "models"], timeout=60)
    try:
        data = json.loads(text) if code == 0 else json.loads((Path.home() / ".codex/models_cache.json").read_text())
    except ValueError:
        return []
    return [{"id": m["slug"], "name": m.get("display_name") or m["slug"],
             "efforts": [lv["effort"] for lv in m.get("supported_reasoning_levels", [])],
             "default_effort": m.get("default_reasoning_level")}
            for m in data.get("models", []) if m.get("visibility") == "list"]


def models_path() -> Path:
    return state_dir() / "models.json"


def load_models() -> dict:
    try:
        return json.loads(models_path().read_text())
    except (OSError, ValueError):
        return {}


async def refresh_models() -> dict:
    """Available models and efforts per CLI, as the installed CLI versions report them."""
    vers = await versions()
    catalog = {"claude": {"version": vers["claude"], "models": _claude_catalog(vers["claude"])},
               "codex": {"version": vers["codex"], "models": await _codex_catalog()}, "fetched": time.time()}
    models_path().write_text(json.dumps(catalog, indent=1))
    return catalog


def check_agent_conf(agent: str, conf: dict, catalog: dict) -> str | None:
    """Why a model/effort choice is invalid for the installed CLI, or None."""
    models = {m["id"]: m for m in (catalog.get(agent) or {}).get("models", [])}
    model, effort = conf.get("model"), conf.get("effort")
    if not models or not model:
        return None
    if model not in models:
        return f"{agent} model {model!r} is not available in {catalog[agent]['version']}"
    if effort and effort not in models[model]["efforts"]:
        allowed = ", ".join(models[model]["efforts"]) or "none (leave effort empty)"
        return f"{agent} model {model} does not support effort {effort!r} (allowed: {allowed})"
    return None


def config_errors(doc: dict) -> list[str]:
    """Agent choices in a mission config (or loop.yaml `defaults`) that the installed CLIs reject."""
    catalog, errors = load_models(), []
    mw = doc.get("meta_work") or {}
    if mw:
        if mw.get("agent") not in ADAPTERS:
            errors.append(f"meta_work.agent must be claude or codex, not {mw.get('agent')!r}")
        else:
            errors.append(check_agent_conf(mw["agent"], mw, catalog))
    for agent, conf in (doc.get("workers") or {}).items():
        if agent not in ADAPTERS:
            errors.append(f"unknown worker agent {agent!r} (use claude or codex)")
        elif isinstance(conf, dict) and conf.get("enabled", True):
            errors.append(check_agent_conf(agent, conf, catalog))
    return [e for e in errors if e]


async def update(name: str) -> dict:
    code, text = await run_cmd(UPDATE_CMD[name], timeout=1800)
    result = {"ok": code == 0, "output": text[-4000:], "smoke": await smoke(name)}
    # The smoke turn makes the CLI refresh its catalog; read the model list after it
    catalog = await refresh_models()
    result["models"] = [m["id"] for m in catalog[name]["models"]]
    return result
