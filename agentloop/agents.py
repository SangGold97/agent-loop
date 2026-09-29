"""Agent CLI management: versions, updates, and adapter smoke tests."""
from __future__ import annotations

import asyncio
import shutil
import time

from . import gitops
from .adapters import RunSpec
from .config import state_dir
from .runner import Runner, child_env

UPDATE_CMD = {"claude": ["claude", "update"], "codex": ["codex", "update"]}
SMOKE_MODEL = {"claude": ("haiku", "low"), "codex": (None, "low")}


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


async def update(name: str) -> dict:
    code, text = await run_cmd(UPDATE_CMD[name], timeout=1800)
    return {"ok": code == 0, "output": text[-4000:], "smoke": await smoke(name)}
