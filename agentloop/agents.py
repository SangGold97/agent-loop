"""Agent CLI management: versions, updates, and adapter smoke tests."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import gitops
from .adapters import ADAPTERS, RunSpec
from .adapters.agy import usage_quota
from .adapters.codex import quota_windows
from .config import state_dir
from .runner import Runner, child_env

UPDATE_CMD = {"claude": ["claude", "update"], "codex": ["codex", "update"], "agy": ["agy", "update"]}
SMOKE_MODEL = {"claude": ("haiku", None), "codex": (None, "low"), "agy": ("gemini-3.8-flash", "low")}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


async def run_cmd(argv: list[str], timeout: float = 30, cwd: Path | None = None) -> tuple[int, str]:
    if not shutil.which(argv[0]):
        return 127, f"{argv[0]} not found"
    proc = await asyncio.create_subprocess_exec(*argv, env=child_env({}), cwd=cwd, stdout=asyncio.subprocess.PIPE,
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


async def _agy_catalog() -> list[dict]:
    """agy lists one slug per model and effort (`gemini-4-argon-high`, `--model gemini-4-argon --effort high` picks
    the same), so its slugs are grouped into models with efforts like the other CLIs' catalogs."""
    code, text = await run_cmd(["agy", "models"], timeout=60)
    models: dict[str, dict] = {}
    for line in text.splitlines() if code == 0 else []:
        slug, _, label = line.strip().partition("\t")
        if not label:  # progress lines
            continue
        base, _, effort = slug.rpartition("-") if slug.rsplit("-", 1)[-1] in EFFORTS else (slug, "", "")
        entry = models.setdefault(base, {"id": base, "name": re.sub(r"\s*\([^)]*\)$", "", label), "efforts": []})
        if effort:
            entry["efforts"].append(effort)
    for m in models.values():
        m["efforts"].sort(key=EFFORTS.index)
    return list(models.values())


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
               "codex": {"version": vers["codex"], "models": await _codex_catalog()},
               "agy": {"version": vers["agy"], "models": await _agy_catalog()}, "fetched": time.time()}
    models_path().write_text(json.dumps(catalog, indent=1))
    return catalog


CLAUDE_USAGE_RE = re.compile(r"^Current (session|week)(?: \((.+?)\))?: (\d+)% used(?: · resets (.+?) \((.+?)\))?$",
                             re.M)


async def read_quota(name: str) -> list[dict]:
    """A CLI's usage windows, read without a model call; [] when the CLI cannot report them."""
    try:
        if name == "claude":
            code, text = await run_cmd(["claude", "-p", "/usage", "--output-format", "json",
                                        "--no-session-persistence"], timeout=60, cwd=state_dir())
            return claude_usage(json.loads(text)["result"]) if code == 0 else []
        if name == "codex":
            return await _codex_quota()
        return await asyncio.to_thread(usage_quota, state_dir())
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        return []


def claude_usage(text: str, now: float | None = None) -> list[dict]:
    """Windows from what `claude -p /usage` prints ("Current week (Fable): 7% used · resets Oct 25, 7am
    (Asia/Bangkok)"), named as the stream's rate_limit_event names them (5h, 7d, 7d fable)."""
    now = time.time() if now is None else now
    out = []
    for kind, scope, pct, stamp, tz in CLAUDE_USAGE_RE.findall(text):
        name = "5h" if kind == "session" else "7d" if scope in ("", "all models") else f"7d {scope.lower()}"
        out.append({"name": name, "used": int(pct) / 100, "resets_at": _local_epoch(stamp, tz, now) if stamp else None})
    return out


def _local_epoch(stamp: str, tz: str, now: float) -> float | None:
    """'Oct 25, 7am' or '5:20pm' in zone `tz` as epoch seconds; None when it does not parse."""
    try:
        zone = ZoneInfo(tz)
        today = datetime.fromtimestamp(now, zone)
        stamp = stamp if "," in stamp else f"{today:%b} {today.day}, {stamp}"
        if ":" not in stamp:
            stamp = f"{stamp[:-2]}:00{stamp[-2:]}"
        at = datetime.strptime(f"{today.year} {stamp}", "%Y %b %d, %I:%M%p").replace(tzinfo=zone)
    except (KeyError, ValueError):
        return None
    # A reset lies ahead, so one dated well before now belongs to next year (read late in December)
    return (at.replace(year=at.year + 1) if at.timestamp() < now - 86400 else at).timestamp()


async def _codex_quota() -> list[dict]:
    """The account's rate limits from `codex app-server` (JSON-RPC over stdio: initialize, then
    account/rateLimits/read), in the rollout's field names for quota_windows."""
    msgs = [{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentloop", "version": "0"}}},
            {"method": "initialized"}, {"id": 2, "method": "account/rateLimits/read"}]
    proc = await asyncio.create_subprocess_exec("codex", "app-server", env=child_env({}), cwd=state_dir(),
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    proc.stdin.write("".join(json.dumps({"jsonrpc": "2.0", **m}) + "\n" for m in msgs).encode())
    try:
        async with asyncio.timeout(30):
            while line := await proc.stdout.readline():
                msg = json.loads(line)
                if msg.get("id") == 2:
                    limits = (msg.get("result") or {}).get("rateLimits") or {}
                    return quota_windows({key: {"used_percent": w.get("usedPercent"), "resets_at": w.get("resetsAt"),
                                                "window_minutes": w.get("windowDurationMins")}
                                          for key, w in limits.items() if isinstance(w, dict)})
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    return []


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
    """Agent choices in a goal config (or loop.yaml `defaults`) that the installed CLIs reject."""
    catalog, errors = load_models(), []
    for kind in ("meta_work", "meta_advisor"):
        mw = doc.get(kind) or {}
        if not isinstance(mw, dict):
            errors.append(f"{kind} must be a mapping of agent, model and effort")
            continue
        if not mw:
            continue
        if mw.get("agent") not in ADAPTERS:
            errors.append(f"{kind}.agent must be one of {', '.join(ADAPTERS)}, not {mw.get('agent')!r}")
        else:
            errors.append(check_agent_conf(mw["agent"], mw, catalog))
    for agent, conf in (doc.get("workers") or {}).items():
        if agent not in ADAPTERS:
            errors.append(f"unknown worker agent {agent!r} (use {', '.join(ADAPTERS)})")
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
