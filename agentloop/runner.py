"""Run one headless CLI turn: spawn (in a systemd scope inside the shared RAM pool), stream, parse."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .adapters import ADAPTERS, ParseState, RunSpec
from .adapters.base import usage_limit_until
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


def pool_slice() -> str:
    """The systemd slice every session's scope runs in; its MemoryMax is the RAM pool they share."""
    return unit_prefix() + ".slice"


def scratch_root() -> Path:
    """Where this platform home's per-turn scratch dirs (each turn's TMPDIR) live, outside every workspace."""
    return Path(tempfile.gettempdir()) / unit_prefix()


@dataclass
class TurnResult:
    vendor_id: str | None
    final: str
    tokens: int
    context: int
    exit_code: int
    error: str
    stopped: bool = False
    leftover: tuple[str, ...] = ()  # commands still running in the scope when the CLI exited (now stopped)
    limited_until: float | None = None  # the vendor refused the turn for a usage limit that resets then
    memory: str = ""  # the engine's note on the turn's memory peak and kills; empty when unremarkable


def systemd_available() -> bool:
    global _systemd
    if _systemd is None:
        ok = shutil.which("systemd-run") and subprocess.run(
            ["systemctl", "--user", "is-system-running"], capture_output=True, text=True
        ).stdout.strip() in ("running", "degraded")
        _systemd = bool(ok)
    return _systemd


def reap_scope(unit: str | None, pgid: int) -> tuple[str, ...]:
    """Stop what a finished turn left running (e.g. a job the agent backgrounded, then ended its turn).

    Such processes would otherwise outlive the turn: untracked, unstoppable from the UI and still using the
    worker's CPU. Returns their command lines so the caller can tell the meta-work what was stopped."""
    cmds: list[str] = []
    cgroup = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", f"{unit}.scope"],
                            capture_output=True, text=True).stdout.strip() if unit else ""
    procs = Path("/sys/fs/cgroup" + cgroup, "cgroup.procs") if cgroup else None
    # Claude Code's own housekeeping (e.g. a plugin-catalog git fetch at exit) runs from its config dir:
    # it is stopped too, but it is not the agent's work, so it is not reported
    cli_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").resolve()
    alive = False
    for pid in (procs.read_text().split() if procs and procs.exists() else []):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            cwd = Path(f"/proc/{pid}/cwd").resolve()
        except OSError:
            continue
        alive = True
        if cmd and not cwd.is_relative_to(cli_home) and cmd[:200] not in cmds:
            cmds.append(cmd[:200])
    if not unit:  # without systemd only the CLI's own process group can be reached, and is not reported
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if alive and unit:
        kill_unit(unit)
    return tuple(cmds)


def job_scope_argv(unit: str, gb: float) -> list[str]:
    """Prefix that runs a `loop run` job in its own scope in the pool, capped at its grant: the kernel stops a
    job that outgrows its grant inside that scope, so its overuse never costs another process."""
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", f"--slice={pool_slice()}",
            "-p", f"MemoryMax={gb}G", "-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue", "--"]


def kill_unit(unit: str) -> None:
    # SIGKILL is immediate; `systemctl stop` could block the event loop for the unit's stop timeout
    subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", f"{unit}.scope"], capture_output=True)


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
        self.pool_gb: float | None = None

    def set_pool(self, gb: float) -> None:
        """Cap the slice all sessions share, so an out-of-memory kill stays among agent processes."""
        if gb != self.pool_gb and systemd_available():
            subprocess.run(["systemctl", "--user", "set-property", "--runtime", pool_slice(), f"MemoryMax={gb}G",
                            "MemorySwapMax=0"], capture_output=True)
            self.pool_gb = gb

    def clear_pool(self) -> None:
        """Drop the slice and its runtime cap on shutdown (its sessions are stopped by then), so a platform home
        that never runs again leaves nothing behind in systemd."""
        if self.pool_gb is not None:
            for verb in ("stop", "revert"):
                subprocess.run(["systemctl", "--user", verb, pool_slice()], capture_output=True)
            self.pool_gb = None

    async def run(self, key: str, spec: RunSpec, *, raw_log: Path, event_log: Path,
                  on_event: Callable[[dict], Awaitable[None]], timeout_s: float,
                  memory_max_gb: float | None = None) -> TurnResult:
        adapter = ADAPTERS[spec.agent]
        argv, unit = adapter.argv(spec), None
        # Every session gets a named scope in the shared pool slice so an unclean restart can find and stop
        # orphans; an optional per-session cap comes on top of the pool. OOMPolicy=continue keeps systemd from
        # stopping the whole turn (CLI and granted jobs too) when the kernel kills one process in it
        if systemd_available():
            unit = f"{unit_prefix()}-{key}-{int(time.time() * 1000)}"
            caps = ["-p", f"MemoryMax={memory_max_gb}G", "-p", "MemorySwapMax=0"] if memory_max_gb else []
            argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
                    f"--slice={pool_slice()}", "-p", "OOMPolicy=continue", *caps, "--", *argv]
        env = child_env({**spec.env, **adapter.env(spec)})
        st = ParseState()
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(spec.cwd), env=env, start_new_session=True, limit=64 * 1024 * 1024,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as exc:
            return TurnResult(None, "", 0, 0, 127, f"cannot start {argv[0]}: {exc}")
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
                    try:
                        obj = json.loads(line)
                        events = adapter.parse(obj, st) if isinstance(obj, dict) else []
                    except Exception:  # one odd line must not kill the turn
                        raw.write(line.decode(errors="replace"))
                        log.warning("unparsed %s line: %.200s", spec.agent, line)
                        continue
                    # Partial-message deltas are only streamed to the UI; the complete message is logged
                    if not (isinstance(obj, dict) and obj.get("type") == "stream_event"):
                        raw.write(line.decode(errors="replace"))
                    for evt in events:
                        evt["ts"] = time.time()
                        if evt["type"] != "delta":
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
        leftover = () if timed_out or stopped else reap_scope(unit, proc.pid)
        rc = proc.returncode if proc.returncode is not None else -1
        error = st.error
        if timed_out:
            error = f"timed out after {timeout_s / 60:.0f} min"
        elif stopped:
            error = "stopped"
        elif rc and not error:
            error = f"exit {rc}: " + "".join(stderr_tail)[-800:].strip()
        adapter.finish(st, spec)
        final = adapter.final_text(st, spec) or ""
        limited = usage_limit_until(f"{error}\n{final}") if error and not (stopped or timed_out) else None
        return TurnResult(st.vendor_id, final, st.tokens, st.context, rc, error, stopped, leftover, limited)

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
