"""Run one headless CLI turn: spawn (optionally in a memory-capped systemd scope), stream, parse."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .adapters import ADAPTERS, ParseState, RunSpec
from .config import PYTHON, home

# Variables describing the parent agent session; child sessions must not inherit them
STRIP_ENV = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PLUGIN_DATA", "CODEX_HOME",
             "CODEX_THREAD_ID", "VIRTUAL_ENV"}
STRIP_PREFIX = ("CLAUDE_CODE_", "CODEX_COMPANION_", "LOOP_")
_systemd: bool | None = None
log = logging.getLogger("agentloop.runner")


def unit_prefix() -> str:
    """Scope-name prefix unique to this platform home, so two installs never touch each other's workers."""
    return "agentloop-" + hashlib.sha1(str(home().resolve()).encode()).hexdigest()[:8]


@dataclass
class TurnResult:
    vendor_id: str | None
    final: str
    tokens: int
    cost: float
    context: int
    exit_code: int
    error: str
    duration: float
    stopped: bool = False


def systemd_available() -> bool:
    global _systemd
    if _systemd is None:
        ok = shutil.which("systemd-run") and subprocess.run(
            ["systemctl", "--user", "is-system-running"], capture_output=True, text=True
        ).stdout.strip() in ("running", "degraded")
        _systemd = bool(ok)
    return _systemd


def child_env(extra: dict) -> dict:
    venv_bin = str(Path(PYTHON).parent)
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV and not k.startswith(STRIP_PREFIX)}
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if p != venv_bin)
    prepend = extra.pop("PATH_PREPEND", None)
    if prepend:
        env["PATH"] = f"{prepend}{os.pathsep}{env['PATH']}"
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


class Runner:
    def __init__(self):
        self.procs: dict[str, tuple[asyncio.subprocess.Process, str | None]] = {}
        self.stopping: set[str] = set()

    async def run(self, key: str, spec: RunSpec, *, raw_log: Path, event_log: Path,
                  on_event: Callable[[dict], Awaitable[None]], timeout_s: float,
                  memory_max_gb: float | None = None) -> TurnResult:
        adapter = ADAPTERS[spec.agent]
        argv, unit = adapter.argv(spec), None
        # Every session gets a named scope so an unclean restart can find and stop orphans;
        # workers additionally get a hard memory cap
        if systemd_available():
            unit = f"{unit_prefix()}-{key}-{int(time.time() * 1000)}"
            caps = ["-p", f"MemoryMax={memory_max_gb}G", "-p", "MemorySwapMax=0"] if memory_max_gb else []
            argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", *caps, "--", *argv]
        env = child_env({**spec.env, **adapter.env(spec)})
        st, started = ParseState(), time.time()
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(spec.cwd), env=env, start_new_session=True, limit=64 * 1024 * 1024,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as exc:
            return TurnResult(None, "", 0, 0.0, 0, 127, f"cannot start {argv[0]}: {exc}", 0.0)
        self.procs[key] = (proc, unit)
        stderr_tail: list[str] = []

        async def feed_stdin():
            proc.stdin.write(spec.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        async def read_stderr():
            async for line in proc.stderr:
                stderr_tail.append(line.decode(errors="replace"))
                del stderr_tail[:-40]

        # Stream stdout JSONL into raw + normalized logs, forwarding events live
        async def read_stdout():
            with raw_log.open("a") as raw, event_log.open("a") as evl:
                async for line in proc.stdout:
                    raw.write(line.decode(errors="replace"))
                    try:
                        obj = json.loads(line)
                        events = adapter.parse(obj, st) if isinstance(obj, dict) else []
                    except Exception:  # one odd line must not kill the turn
                        log.warning("unparsed %s line: %.200s", spec.agent, line)
                        continue
                    for evt in events:
                        evt["ts"] = time.time()
                        evl.write(json.dumps(evt, ensure_ascii=False) + "\n")
                        evl.flush()
                        await on_event(evt)

        # Readers run to EOF even if the CLI exits before reading its prompt, so stderr is complete
        readers = [asyncio.ensure_future(read_stderr()), asyncio.ensure_future(read_stdout())]
        timed_out = False
        try:
            async with asyncio.timeout(timeout_s):
                try:
                    await feed_stdin()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                await asyncio.gather(*readers)
                await proc.wait()
        except TimeoutError:
            timed_out = True
            await self.kill(key)
        except BaseException:  # never leave an untracked CLI running
            await self.kill(key)
            raise
        finally:
            for task in readers:
                task.cancel()
            self.procs.pop(key, None)
        stopped = key in self.stopping
        self.stopping.discard(key)
        rc = proc.returncode if proc.returncode is not None else -1
        error = st.error
        if timed_out:
            error = f"timed out after {timeout_s / 60:.0f} min"
        elif stopped:
            error = "stopped"
        elif rc and not error:
            error = f"exit {rc}: " + "".join(stderr_tail)[-800:].strip()
        return TurnResult(st.vendor_id, adapter.final_text(st, spec) or "", st.tokens, st.cost,
                          st.context, rc, error, time.time() - started, stopped)

    def running(self, key: str) -> bool:
        return key in self.procs

    def pid(self, key: str) -> int | None:
        entry = self.procs.get(key)
        return entry[0].pid if entry else None

    async def kill(self, key: str, stopped: bool = False) -> None:
        entry = self.procs.get(key)
        if not entry:
            return
        if stopped:
            self.stopping.add(key)
        proc, unit = entry
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                await asyncio.wait_for(proc.wait(), 5)
                break
            except asyncio.TimeoutError:
                continue
        if unit:
            subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True)
