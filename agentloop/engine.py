"""Goal engine: event-driven meta-work turns, workers, meta-chats, notes, budgets, review."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import psutil
import yaml

from . import agents, digest, gitops, workspace
from .adapters import RunSpec
from .adapters.base import to_tokens
from .config import DEFAULTS, PYTHON, home, goal_config, state_dir, workspace_config
from .db import DB
from .infra import (GPU_STALE_S, SAMPLE_S, Infra, cgroup_dir, cgroup_pids, scope_cpu_s, scope_memory, set_oom_adj,
                    tree_cpu_s, tree_rss)
from .runner import (Runner, child_env, job_scope_argv, job_unit, jobs_tmp_root, kill_unit, pool_cgroup,
                     scratch_root, systemd_available, unit_prefix)

log = logging.getLogger("agentloop")
VERIFY_TIMEOUT_S = 900
PREFIX = {"meta-work": "mw", "worker": "w", "meta-chat": "mc", "meta-advisor": "ma"}
LIMIT_SLACK_S = 60  # retry this long after a vendor's announced usage-limit reset
VRAM_SETTLE_S = 10  # a GPU is not policed again this long after a job on it was stopped, while its memory frees
RAM_SETTLE_S = 5  # likewise for the RAM pool
RAM_ADMIT = 0.90  # share of the RAM pool in use from which `loop run`, spawn, send and advise wait
RAM_KILL = 0.95  # share in use from which the newest heavy process is stopped
HEAVY_GB = 1.0  # a tool process or job this large may be stopped to save the pool
GRANT_SAMPLES = ("rss_gb", "peak_gb", "oom", "cpu", "cpu_cores")  # a job's measurements, not kept in its jobs-table record
AGENT_PROCS = {"claude", "codex", "agy"}  # an agent CLI's own processes, never stopped for RAM
QUOTA_WARN = 0.9  # share of a CLI's usage window past which running goals hear it is running out
DISK_WARN_GB = 10  # free disk below which running goals hear it is running out (again once back above twice that)
CPU_WARN_PCT = 90  # 10-minute mean CPU use from which running goals hear the CPU is saturated (again once below 70)
CPU_REARM_PCT = 70
MIN_JOB_THREADS = 2  # fewest BLAS/OpenMP threads a `loop run` job gets by default, however busy the CPU is


def clock(ts: float) -> str:
    """Local wall time for agent-facing messages: HH:MM today, with the date otherwise."""
    fmt = "%H:%M" if time.strftime("%x", time.localtime(ts)) == time.strftime("%x") else "%Y-%m-%d %H:%M"
    return time.strftime(fmt, time.localtime(ts))


def leftover_note(cmds: tuple[str, ...], whose: str) -> str:
    """Tell the meta-work that a turn ended with processes still running, which the platform stopped."""
    more = f" (+{len(cmds) - 5} more)" if len(cmds) > 5 else ""
    return (f"[System] Processes still running when {whose} ended were stopped; their results may be "
            "incomplete: " + "; ".join(f"`{c}`" for c in cmds[:5]) + more)


class AgentError(Exception):
    """A refused agent/human request; the message is returned verbatim."""


class Bus:
    """Fan-out of live events to SSE subscribers."""

    def __init__(self):
        self.subs: set[asyncio.Queue] = set()

    def publish(self, evt: dict) -> None:
        evt.setdefault("ts", time.time())
        for q in list(self.subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)


def core_ranges(cores: set[int]) -> str:
    """CPU cores as ranges ("6-8", "0-3,14-15"); "all" when nothing narrows them."""
    if not cores or len(cores) >= (psutil.cpu_count() or 0):
        return "all" if cores else ""
    out, run = [], []
    for c in sorted(cores):
        if run and c != run[-1] + 1:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = []
        run.append(c)
    out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ",".join(out)


class Engine:
    def __init__(self, api_url: str):
        self.api_url = api_url
        self.db = DB(state_dir() / "agentloop.db")
        self.bus = Bus()
        self.runner = Runner()
        self.infra = Infra(str(home()))
        self.meta_active: set[tuple[str, str]] = set()
        self.vendor_limits: dict[str, float] = {}  # agent CLI -> when its usage limit resets
        self.waking: set[tuple[str, str]] = set()  # goals whose meta-work waits for a usage-limit reset
        # Each CLI's usage-limit windows as its last turn reported them, kept across restarts; the windows over
        # QUOTA_WARN that running goals were already told about
        try:
            self.quotas: dict[str, dict] = json.loads((state_dir() / "quotas.json").read_text())
        except (OSError, ValueError):
            self.quotas = {}
        self.quota_warned: set[tuple[str, str]] = set()
        self.disk_warned = False
        self.cpu_warned = False
        self.int_locks: dict[tuple[str, str], threading.Lock] = {}  # one per goal's integration worktree
        self.tasks: set[asyncio.Task] = set()
        self.task_goal: dict[asyncio.Task, tuple[str, str]] = {}  # tasks working on a goal, awaited by deletes
        self.chat_tasks: dict[str, asyncio.Task] = {}  # meta-chat -> its running turn, awaited by its delete
        self.agent_ops: dict[str, dict] = {}
        # Running `loop run` jobs (with their VRAM grants), the requests still waiting to start, and each running
        # turn's memory record; all of it lives only as long as the turns, so a restart starts empty, but for the
        # detached jobs, which `startup` restores from the jobs table
        self.grants: dict[int, dict] = {}
        # Keys start from the clock, so a `loop run` helper that outlived a restart (inside a detached job) never
        # releases a newer grant by its old key
        self.grant_ids = itertools.count(int(time.time() * 1000))
        self.ram_waiting: dict[int, dict] = {}
        self.turn_mem: dict[str, dict] = {}
        self.ram_wait_goals: set[tuple[str, str]] = set()  # goals refused a dispatch while the pool was full
        self.ram_admitted = 0.0  # sample time of the last job started; the next one waits for a newer sample
        # Per GPU index, the VRAM in use while none of its grants was active: where the driver lists no processes
        # (WSL), what a GPU uses above it is its jobs'. Grants stopped for RAM or VRAM -> (session, what `loop run`
        # reports)
        self.vram_base: dict[int, float] = {}
        self.stopped: dict[int, tuple[str, str]] = {}
        self.vram_settle: dict[int, float] = {}  # GPU index -> until when it is left alone after a stop
        self.ram_settle = 0.0  # until when the RAM pool is left alone after a stop
        self.bin_dir = state_dir() / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        shim = self.bin_dir / "loop"
        shim.write_text(f'#!/bin/sh\nexec "{PYTHON}" -m agentloop.cli "$@"\n')
        shim.chmod(0o755)

    # ---------- plumbing ----------
    def spawn(self, coro, goal: tuple[str, str] | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        if goal:
            self.task_goal[task] = goal
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        self.task_goal.pop(task, None)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())
            self.bus.publish({"type": "error", "text": repr(task.exception())})

    def limit_until(self, agent: str) -> float | None:
        """Reset time of the agent CLI's usage limit while it is still in force."""
        until = self.vendor_limits.get(agent)
        if until and until > time.time():
            return until
        self.vendor_limits.pop(agent, None)
        return None

    def set_quota(self, agent: str, windows: list[dict]) -> None:
        """Keep a CLI's latest usage-limit windows. When one passes QUOTA_WARN, every running goal hears it once
        (deferred, so no meta turn is spent on it) and can move new workers to another CLI before this one refuses
        them; it hears again only after the window has dropped below QUOTA_WARN."""
        self.quotas[agent] = {"ts": time.time(), "windows": windows}
        with contextlib.suppress(OSError):
            (state_dir() / "quotas.json").write_text(json.dumps(self.quotas))
        self.publish("quota", agent=agent, **self.quotas[agent])
        for w in windows:
            key = (agent, w["name"])
            if w["used"] < QUOTA_WARN:
                self.quota_warned.discard(key)
                continue
            if key in self.quota_warned:
                continue
            self.quota_warned.add(key)
            reset = f", which resets at {clock(w['resets_at'])}" if w.get("resets_at") else ""
            text = (f"{agent} has used {w['used']:.0%} of its {w['name']} usage window{reset}; prefer another CLI "
                    "for new workers and sends until then.")
            for g in self.db.q("SELECT ws, id FROM goals WHERE status='running'"):
                self.db.push_inbox(g["ws"], g["id"], "system",
                                   {"text": f"[System] {text}", "until": w.get("resets_at")}, deferred=True)
                self.publish("alert", ws=g["ws"], goal=g["id"], text=text)

    def check_disk(self, free_gb: float) -> None:
        """Wake every running goal's meta-work once when free disk falls below DISK_WARN_GB, since builds, jobs
        and commits start failing soon after; it hears again only after free disk has recovered."""
        if free_gb >= 2 * DISK_WARN_GB:
            self.disk_warned = False
        if free_gb >= DISK_WARN_GB or self.disk_warned:
            return
        self.disk_warned = True
        text = (f"Only {free_gb} GB of disk is free; delete large outputs you no longer need and avoid jobs that "
                "write a lot until there is more.")
        for g in self.db.q("SELECT ws, id FROM goals WHERE status='running'"):
            self.db.push_inbox(g["ws"], g["id"], "system", {"text": f"[System] {text}"})
            self.kick(g["ws"], g["id"])
            self.publish("alert", ws=g["ws"], goal=g["id"], text=text)

    def check_cpu(self) -> None:
        """Tell every running goal once when the CPU has been saturated for 10 minutes. Nothing is refused for CPU:
        busy jobs share it, so more parallel heavy work only slows each of them. The note waits for the next event
        rather than waking the meta-work, and comes again only after the CPU has calmed down."""
        if len(self.infra.history) < self.infra.history.maxlen:
            return
        avg = self.infra.cpu_avg()
        if avg < CPU_REARM_PCT:
            self.cpu_warned = False
        if avg < CPU_WARN_PCT or self.cpu_warned:
            return
        self.cpu_warned = True
        text = (f"The CPU has been {avg:.0f}% busy over the last 10 minutes; running jobs share it, so more heavy "
                "work in parallel only slows each of them.")
        for g in self.db.q("SELECT ws, id FROM goals WHERE status='running'"):
            self.db.push_inbox(g["ws"], g["id"], "system", {"text": f"[System] {text}"}, deferred=True)
            self.publish("alert", ws=g["ws"], goal=g["id"], text=text)

    def job_threads(self) -> int:
        """Default BLAS/OpenMP thread count for a job starting now: the cores the running jobs leave free. A job
        counts with the cores it was last measured using, or with its own default until its first measurement, so
        the agent CLIs' small use does not shrink it and two jobs started together do not both take every core."""
        cpus = self.infra.latest()["cpus"] or 1
        busy = sum(g["cpu_cores"] if "cpu_cores" in g else g.get("threads", 0) for g in self.grants.values())
        return max(MIN_JOB_THREADS, min(cpus, int(cpus - busy)))

    def quota_text(self) -> str:
        """Each CLI's usage-limit windows as last reported; a window whose reset has passed counts as unused."""
        now, parts = time.time(), []
        for agent, q in sorted(self.quotas.items()):
            windows = []
            for w in q["windows"]:
                reset = w.get("resets_at")
                used = 0.0 if reset and reset <= now else w["used"]
                windows.append(f"{w['name']} {used:.0%}" + (f" until {clock(reset)}" if used >= 0.5 and reset else ""))
            parts.append(f"{agent} {', '.join(windows)} (as of {clock(q['ts'])})")
        return "CLI usage limits: " + "; ".join(parts) if parts else ""

    def wake_at(self, ws: str, gid: str, until: float) -> None:
        """Resume a goal's meta-work once its CLI's usage limit resets. The task is not tied to the goal, so a
        delete does not wait for it."""
        if (ws, gid) in self.waking:
            return
        self.waking.add((ws, gid))

        async def wake():
            try:
                await asyncio.sleep(max(0.0, until - time.time()) + LIMIT_SLACK_S)
            finally:
                self.waking.discard((ws, gid))
            if self.db.one("SELECT 1 FROM goals WHERE ws=? AND id=? AND status='running'", ws, gid):
                self.kick(ws, gid)
        self.spawn(wake())

    def publish(self, kind: str, **data) -> None:
        self.bus.publish({"type": kind, **data})

    @staticmethod
    def wsp(ws: str) -> Path:
        return workspace.ws_path(ws)

    def gdir(self, ws: str, gid: str) -> Path:
        return workspace.goal_dir(self.wsp(ws), gid)

    def logs(self, ws: str, sid: str) -> tuple[Path, Path]:
        base = self.wsp(ws) / ".loop" / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{sid}.raw.jsonl", base / f"{sid}.events.jsonl"

    def goal_row(self, ws: str, gid: str) -> dict:
        if not (self.gdir(ws, gid) / "goal.yaml").exists():
            raise AgentError(f"goal {ws}/{gid} not found")
        row = self.db.goal(ws, gid)
        if not row:
            self.db.x("INSERT OR IGNORE INTO goals(ws, id, status) VALUES(?,?,?)", ws, gid, "draft")
            row = self.db.goal(ws, gid)
        return row

    def set_goal(self, ws: str, gid: str, **fields) -> None:
        self.db.update("goals", {"ws": ws, "id": gid}, **fields)
        if "status" in fields:
            self.publish("goal_status", ws=ws, goal=gid, status=fields["status"])

    @contextlib.asynccontextmanager
    async def int_lock(self, ws: str, gid: str):
        """Serialize git work in a goal's single integration worktree (checkpoints, merges, verify runs). The
        lock is taken in a thread so a waiting caller never stalls the event loop."""
        lock = self.int_locks.setdefault((ws, gid), threading.Lock())
        await asyncio.to_thread(lock.acquire)
        try:
            yield
        finally:
            lock.release()

    def checkpoint(self, ws: str, gid: str, sid: str, why: str) -> str:
        """Commit the meta-work's own changes in the integration worktree; the caller holds the integration lock."""
        try:
            return gitops.checkpoint(self.wsp(ws), gid, f"meta-work({sid})", f"meta-work({sid}): {why}")
        except RuntimeError as exc:
            raise AgentError(f"could not commit your changes in the integration worktree: {exc}") from None

    @staticmethod
    def checkout_state(path: Path) -> str:
        """Branch, HEAD and uncommitted files of a checkout, to notice a turn that changed one it must not touch."""
        if not path.exists():
            return ""
        return "\n".join(gitops.git(path, *args, check=False)
                         for args in (("symbolic-ref", "-q", "HEAD"), ("rev-parse", "HEAD"), ("status", "--porcelain")))

    def workers(self, ws: str, gid: str, running_only: bool = False) -> list[dict]:
        rows = self.db.q("SELECT * FROM sessions WHERE ws=? AND goal=? AND kind='worker' ORDER BY created",
                         ws, gid)
        return [r for r in rows if r["status"] == "running"] if running_only else rows

    def advisors(self, ws: str, gid: str) -> list[dict]:
        return self.db.q("SELECT * FROM sessions WHERE ws=? AND goal=? AND kind='meta-advisor' "
                         "AND status IN ('queued', 'running') ORDER BY created", ws, gid)

    def new_session(self, ws: str, gid: str, kind: str, role: str, conf: dict, cwd: Path,
                    parent: str | None = None, branch: str | None = None, sid: str | None = None) -> dict:
        sid = sid or f"{PREFIX[kind]}-{secrets.token_hex(3)}"
        now = time.time()
        self.db.x("INSERT INTO sessions(id, ws, goal, kind, parent, role, agent, model, effort, "
                  "autocompact, cwd, branch, status, token, created, updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  sid, ws, gid, kind, parent, role, conf["agent"], conf.get("model"), conf.get("effort"),
                  None if conf.get("autocompact") is None else str(conf.get("autocompact")), str(cwd), branch,
                  "idle", secrets.token_hex(16), now, now)
        self.publish("session_status", ws=ws, goal=gid, session=sid, status="idle")
        return self.db.session(sid)

    # ---------- lifecycle ----------
    async def startup(self) -> None:
        # Worker scopes left over from an unclean shutdown would keep writing into worktrees
        subprocess.run(["systemctl", "--user", "stop", f"{unit_prefix()}-*.scope"], capture_output=True)
        # Scratch dirs of turns cut off by the shutdown; nothing runs any more to use them
        shutil.rmtree(scratch_root(), ignore_errors=True)
        # Events handed to a meta turn that never finished go back to the queue
        self.db.x("UPDATE inbox SET consumed=0 WHERE consumed=2")
        # A turn cut off by a restart still counts, so the next turn gets its own evidence files
        for s in self.db.q("SELECT id, turns FROM sessions WHERE status='running'"):
            cut = self.db.q("SELECT id FROM turns WHERE session=? AND status='running'", s["id"])
            for t in cut:
                self.db.update("turns", {"id": t["id"]}, status="interrupted", ended=time.time())
            self.db.update("sessions", {"id": s["id"]}, status="interrupted", turns=s["turns"] + len(cut))
        self.recover_advice()
        # Detached jobs outlive a restart, so their records come back and the pool keeps watching them; one whose
        # helper is gone is ended by `police_jobs`. The record holds no measurements; the sampler takes them anew
        for j in self.db.q("SELECT * FROM jobs WHERE status='running' AND grant_info IS NOT NULL"):
            if psutil.pid_exists(j["pid"]):
                g = {k: v for k, v in json.loads(j["grant_info"]).items() if k not in GRANT_SAMPLES}
                self.grants[next(self.grant_ids)] = {**g, "rss_gb": 0.0, "oom": 0}
                # The GPU's level without grants, as it was when the job started, is what it is measured against
                if g.get("vram_base") is not None:
                    self.vram_base.setdefault(g["gpu"], g["vram_base"])
        # A meta-chat is kept only once the human wrote in it; drop chats created empty by older versions
        self.db.x("DELETE FROM sessions WHERE kind='meta-chat' AND id NOT IN (SELECT session FROM messages)")
        # Integration worktrees used to live at wt/_int-<gid>; nothing runs yet, so they move to wt/<gid> now
        for m in self.db.q("SELECT ws, id FROM goals"):
            try:
                if gitops.move_legacy_worktree(self.wsp(m["ws"]), m["id"]):
                    log.info("%s: moved the integration worktree of %s to wt/%s", m["ws"], m["id"], m["id"])
            except (RuntimeError, OSError) as exc:
                log.warning("%s: could not move the integration worktree of %s: %s", m["ws"], m["id"], exc)
        for m in self.db.q("SELECT * FROM goals WHERE status='running'"):
            names = [s["id"] for s in self.db.q("SELECT id FROM sessions WHERE ws=? AND goal=? AND "
                                                "status='interrupted' AND kind='worker'", m["ws"], m["id"])]
            # Count active time up to the last recorded activity before the restart
            last = self.db.one("SELECT MAX(updated) t FROM sessions WHERE ws=? AND goal=?", m["ws"], m["id"])["t"]
            gained = max(0.0, (last or 0) - (m["started"] or time.time()))
            self.db.update("goals", {"ws": m["ws"], "id": m["id"]}, started=time.time(),
                           active_s=(m["active_s"] or 0) + gained)
            self.db.push_inbox(m["ws"], m["id"], "system", {"text": (
                "[System] The platform restarted. " + (f"Interrupted workers: {', '.join(names)} "
                "(continue them with `loop send`)." if names else "No workers were interrupted."))})
            self.kick(m["ws"], m["id"])
        # Harness prompts follow the platform templates unless the human customised them; every workspace has
        # the shared venv its sessions run in
        for name in workspace.list_workspaces():
            p = self.wsp(name)
            try:
                workspace.ensure_venv(p)
            except (OSError, subprocess.CalledProcessError) as exc:
                log.error("%s: cannot create the shared venv: %s", name, exc)
            res = workspace.sync_harness(p)
            if res["updated"]:
                workspace.commit(p, "control-plane", "control-plane: sync harness " + ", ".join(res["updated"]))
            if res["kept"]:
                log.info("%s: customised harness files kept: %s", name, ", ".join(res["kept"]))
        self.spawn(self.infra_loop())
        self.spawn(agents.refresh_models())  # model lists follow the installed CLI versions

    async def infra_loop(self) -> None:
        # Samples keep a fixed cadence: the time a sample takes is not added to the wait for the next one. The
        # monotonic clock paces it, as WSL steps its wall clock every half minute or so
        tick = time.monotonic()
        while True:
            # A failed sample must not freeze the RAM admission gate on stale numbers
            try:
                snap = await asyncio.to_thread(self.infra.sample)
                snap["sessions"] = []
                # A job runs in its own scope in the pool slice; one whose `loop run` died without ending it (e.g.
                # the helper was killed) is stopped with its whole process tree
                for key, g in list(self.grants.items()):
                    if not psutil.pid_exists(g["pid"]):
                        self.end_grant(key)
                        continue
                    base = pool_cgroup()
                    used = scope_memory(base / f"{g['unit']}.scope") if g["unit"] and base else None
                    if used:
                        g["oom"] = used[1]
                    # Without systemd the job is a child of the helper
                    g["rss_gb"] = round((used[0] if used else 0 if g["unit"] else tree_rss(g["pid"])) / 2**30, 2)
                    # Peak of the samples, which `loop run` prints when the job ends; a job too short to be sampled
                    # has none
                    if used or not g["unit"]:
                        g["peak_gb"] = max(g.get("peak_gb") or 0.0, g["rss_gb"])
                    # Cores in use since the last sample (1.0 = one core busy), for the infra page; until the job's
                    # scope exists its process tree is read, and a change of source starts the count again
                    cpu_s = scope_cpu_s(base / f"{g['unit']}.scope") if g["unit"] and base else None
                    cpu = (time.monotonic(), tree_cpu_s(g["pid"]) if cpu_s is None else cpu_s, cpu_s is None)
                    if g.get("cpu") and g["cpu"][2] == cpu[2]:
                        g["cpu_cores"] = round(max(0.0, cpu[1] - g["cpu"][1]) / max(1e-3, cpu[0] - g["cpu"][0]), 2)
                    g["cpu"] = cpu
                for key, (proc, _) in list(self.runner.procs.items()):
                    s = self.db.session(key) or {"kind": "smoke", "ws": "", "goal": "", "role": ""}
                    # The turn's scope counts detached jobs too; without systemd only the CLI's process tree
                    mem = self.turn_mem.get(key, {"peak": 0.0})  # CLI smoke runs keep no record
                    cg = mem.get("cgroup") or cgroup_dir(proc.pid)
                    # Until systemd-run has moved the CLI into its scope, the cgroup is still the server's
                    used = scope_memory(cg) if cg and cg.name.startswith(unit_prefix()) else None
                    if used:
                        mem["cgroup"], mem["oom"] = cg, used[1]
                    rss = round((used[0] if used else tree_rss(proc.pid)) / 2**30, 2)
                    jobs = sum(g["rss_gb"] for g in self.grants.values() if g["sid"] == key and g["unit"])
                    mem["peak"] = max(mem["peak"], rss + jobs)
                    snap["sessions"].append({"id": key, "kind": s["kind"], "ws": s["ws"], "goal": s["goal"],
                                             "role": s["role"], "agent": s.get("agent") or "",
                                             "rss_gb": round(rss + jobs, 2),
                                             # The session's `loop run` jobs (their RAM is in rss_gb already) and
                                             # those still waiting to start
                                             "grants": [{"rss_gb": g["rss_gb"], "vram": g["vram"], "gpu": g.get("gpu")}
                                                        for g in self.grants.values() if g["sid"] == key],
                                             "waiting": [{"vram": w["vram"], "since": w["since"]}
                                                         for w in self.ram_waiting.values() if w["sid"] == key]})
                self.police_ram()
                self.police_jobs()
                self.sample_vram(snap)
                snap["jobs"] = self.jobs_view()
                self.police_vram(snap)
                self.check_disk(snap["disk_free_gb"])
                self.check_cpu()
                self.publish("infra", data=snap)
                # A goal refused a dispatch for RAM hears once the pool has room again, unless it ended meanwhile
                for ws, gid in list(self.ram_wait_goals):
                    row = self.db.one("SELECT status FROM goals WHERE ws=? AND id=?", ws, gid)
                    if not row or row["status"] != "running":
                        self.ram_wait_goals.discard((ws, gid))
                    elif self.ram_share() < RAM_ADMIT:
                        self.ram_wait_goals.discard((ws, gid))
                        self.db.push_inbox(ws, gid, "system", {"text": (
                            f"[System] The RAM pool has room again ({self.ram_free()} GB free); spawn or send what "
                            "was refused if it is still needed.")})
                        self.kick(ws, gid)
            except Exception:
                log.exception("infra sample failed")
            tick = max(tick + SAMPLE_S, time.monotonic())
            await asyncio.sleep(tick - time.monotonic())

    async def shutdown(self) -> None:
        for key in list(self.runner.procs):
            await self.runner.kill(key)
        # Detached jobs keep running in the pool slice across a restart
        if not self.db.one("SELECT 1 FROM jobs WHERE status='running'"):
            self.runner.clear_pool()

    # ---------- budget ----------
    def budget(self, ws: str, gid: str) -> dict:
        cfg = goal_config(self.wsp(ws), gid)["goal"]["budget"]
        m = self.goal_row(ws, gid)
        running = m["status"] == "running" and m["started"]
        active = (m["active_s"] or 0) + (time.time() - m["started"] if running else 0)
        return {**cfg, "running_workers": len(self.workers(ws, gid, running_only=True)),
                "running_advisors": len(self.advisors(ws, gid)),
                "worker_turns": m["worker_turns"], "meta_turns": m["meta_turns"], "hours": active / 3600,
                "from": m["first_started"], "to": None if running else m["ended"]}

    @staticmethod
    def exhausted(b: dict) -> tuple[str, bool] | None:
        """(reason, hard) when a budget limit is reached; hard limits also stop running workers."""
        if b.get("max_meta_turns") and b["meta_turns"] >= b["max_meta_turns"]:
            return "meta turns", True
        if b.get("max_worker_turns") and b["worker_turns"] >= b["max_worker_turns"]:
            return "worker turns", False
        return None

    def check_budget(self, ws: str, gid: str) -> None:
        m = self.goal_row(ws, gid)
        if m["status"] != "running" or m["final_turn"]:
            return
        b = self.budget(ws, gid)
        hit = self.exhausted(b)
        if not hit:
            return
        reason, hard = hit
        if hard:
            for w in self.workers(ws, gid, running_only=True):
                self.spawn(self.runner.kill(w["id"], stopped=True))
        elif b["running_workers"]:
            return  # soft limit: let running workers report first
        if b["running_advisors"]:
            return  # reserved advisor turns finish before the meta-work's final budget turn
        self.set_goal(ws, gid, final_turn=1)
        self.db.push_inbox(ws, gid, "budget", {"text": (
            f"[Budget exhausted: {reason}] No more workers or advisors can be started. This is your final turn: "
            "call `loop done --status partial` (or `success` if the goal is met) with a report.")})
        self.kick(ws, gid)

    def check_dispatch(self, ws: str, gid: str) -> None:
        p = self.wsp(ws)
        cfg, m, b = workspace_config(p), self.goal_row(ws, gid), self.budget(ws, gid)
        if m["final_turn"] or self.exhausted(b):
            raise AgentError("budget exhausted: no more workers can be started; finalize with `loop done`")
        if b["running_workers"] >= (b["max_concurrent_workers"] or 1e9):
            raise AgentError(f"{b['running_workers']} workers already running (max {b['max_concurrent_workers']} "
                             "for this goal); a slot frees when one of them reports")
        total = self.db.one("SELECT COUNT(*) c FROM sessions WHERE ws=? AND kind='worker' AND status='running'",
                            ws)["c"]
        if total >= cfg["limits"]["max_workers_total"]:
            raise AgentError(f"workspace worker limit reached ({total}); wait for a report")
        self.check_ram(ws, gid)

    def check_ram(self, ws: str, gid: str) -> None:
        """Refuse a dispatch while the pool is RAM_ADMIT full. The goal then waits for RAM: it does not count as
        idle, and the sampler sends it an event once the pool has room again."""
        if self.ram_share() < RAM_ADMIT:
            self.ram_wait_goals.discard((ws, gid))
        else:
            self.ram_wait_goals.add((ws, gid))
            raise AgentError(f"the RAM pool is full ({self.ram_text()}); an event follows when it has room again, "
                             "so do other work or end your turn meanwhile")

    # ---------- RAM pool shared by all sessions ----------
    def infra_max(self, key: str) -> float:
        """A host-wide infra setting: the largest value any workspace asks for."""
        return max([workspace_config(self.wsp(n))["infra"][key] for n in workspace.list_workspaces()]
                   or [DEFAULTS["infra"][key]])

    def pool_gb(self) -> float:
        """RAM all sessions share: the host's RAM minus the largest reserve any workspace keeps for the rest."""
        return max(1.0, round(self.infra.latest()["ram_total_gb"] - self.infra_max("pool_reserve_gb"), 1))

    def ram_free(self) -> float:
        """GB the pool has left: what its sessions and jobs leave, but no more than the host has available."""
        snap = self.infra.latest()
        sessions = snap.get("sessions", [])
        # A session's RAM includes its jobs; a detached job whose session has no turn running counts on its own
        used = sum(s["rss_gb"] for s in sessions) + sum(
            g["rss_gb"] for g in self.grants.values() if g.get("job") and g["sid"] not in {s["id"] for s in sessions})
        return round(max(0.0, min(self.pool_gb() - used, snap["ram_free_gb"])), 1)

    def ram_share(self) -> float:
        """Share of the pool in use."""
        return 1 - self.ram_free() / self.pool_gb()

    def ram_text(self) -> str:
        parts = [f"agent RAM pool {self.pool_gb()} GB, {self.ram_share():.0%} in use ({self.ram_free()} GB free); "
                 f"`loop run` waits from {RAM_ADMIT:.0%}, and from {RAM_KILL:.0%} the newest heavy process is stopped"]
        parts += [f"GPU{gpu['index']} VRAM {gpu['mem_total_gb']} GB, {self.vram_free(gpu)} GB left to grant"
                  for gpu in self.infra.latest()["gpus"]]

        def vram(g: dict) -> str:  # a waiting request has no GPU yet
            if not g.get("vram"):
                return ""
            if "gpu" not in g:
                return f" + {g['vram']} GB VRAM"
            return f" + {g['vram']} GB VRAM on GPU{g['gpu']}"

        if self.grants:
            parts.append("jobs: " + "; ".join(f"{g['sid']} {g['rss_gb']:.1f} GB RAM{vram(g)} `{g['cmd']}`"
                                              for g in self.grants.values()))
        if self.ram_waiting:
            parts.append("waiting: " + "; ".join(f"{w['sid']}{vram(w)} `{w['cmd']}`"
                                                 for w in self.ram_waiting.values()))
        return " · ".join(parts)

    def heavy(self) -> list[dict]:
        """What the pool may lose, newest first: `loop run` jobs and the tool processes of running turns that hold
        HEAVY_GB or more. An agent CLI and the `loop run` helpers are never among them."""
        items = [{"started": g["started"], "gb": g["rss_gb"], "sid": g["sid"], "key": key, "what": g["cmd"],
                  "pids": self.job_pids(g) - {g["pid"]}} for key, g in self.grants.items() if g["rss_gb"] >= HEAVY_GB]
        for sid, mem in self.turn_mem.items():
            for pid in mem.get("tools", ()):
                with contextlib.suppress(psutil.Error):
                    proc = psutil.Process(pid)
                    gb = proc.memory_info().rss / 2**30
                    if gb >= HEAVY_GB:
                        items.append({"started": proc.create_time(), "gb": gb, "sid": sid, "pid": pid,
                                      "what": shlex.join(proc.cmdline())[:120], "pids": {pid}})
        return sorted(items, key=lambda h: -h["started"])

    def police_ram(self) -> None:
        """No job reserves RAM; the pool is watched as a whole. From RAM_KILL of it in use, the newest heavy process
        is stopped, one at a time with RAM_SETTLE_S for its memory to free, so the jobs that were there first keep
        running. Should the pool run out between samples, the kernel's OOM killer takes them in the same order."""
        tools: dict[int, str] = {}  # a turn's tool processes, other than the CLI and `loop run` helpers
        for sid, mem in self.turn_mem.items():
            if not mem.get("cgroup"):
                continue
            keep = {g["pid"] for g in self.grants.values() if g["sid"] == sid}
            if proc := self.runner.procs.get(sid, (None,))[0]:
                keep.add(proc.pid)
            # A helper's `systemd-run` sits in the turn's scope until it has moved into the job's scope, and an
            # agent CLI may run its own binary under a launcher (codex's node wrapper)
            for pid in list(keep):
                with contextlib.suppress(psutil.Error):
                    for c in psutil.Process(pid).children(recursive=True):
                        if pid != getattr(proc, "pid", None) or c.name() in AGENT_PROCS:
                            keep.add(c.pid)
            mem["tools"] = cgroup_pids(mem["cgroup"]) - keep
            tools |= dict.fromkeys(mem["tools"], sid)
        heavy = self.heavy()
        # Kernel order: the newest heavy one first, older ones after it, then the other tool processes
        rank = dict.fromkeys(tools, 200)
        for i, h in enumerate(heavy):
            rank |= dict.fromkeys(h["pids"], max(300, 1000 - 100 * i))
        for pid, adj in rank.items():
            set_oom_adj(pid, adj)
        if not heavy or time.monotonic() < self.ram_settle or self.ram_share() < RAM_KILL:
            return
        h = heavy[0]
        self.ram_settle = time.monotonic() + RAM_SETTLE_S
        why = (f"stopped at {h['gb']:.1f} GB because the RAM pool was {self.ram_share():.0%} full and it was the "
               "newest heavy process; run it again once `loop infra` shows room, or make it use less memory")
        if "key" in h:
            self.stop_job(h["key"], why)
            return
        with contextlib.suppress(psutil.Error):
            psutil.Process(h["pid"]).kill()
        self.turn_mem[h["sid"]]["over"].append(f"`{h['what']}` (pid {h['pid']}) was {why}")
        log.warning("%s: killed pid %s (%.1f GB) for RAM: %s", h["sid"], h["pid"], h["gb"], h["what"])

    def stop_job(self, key: int, why: str) -> None:
        """Stop a `loop run` job; its `loop run` and the turn's memory note say why."""
        g = self.grants[key]
        if not g["unit"]:  # without systemd the job is a child of the helper
            with contextlib.suppress(psutil.Error):
                for proc in psutil.Process(g["pid"]).children(recursive=True):
                    proc.kill()
        self.stopped[key] = (g["sid"], f"the job was {why}")
        if g["sid"] in self.turn_mem:
            self.turn_mem[g["sid"]]["over"].append(f"`{g['cmd']}`: {why}")
        self.end_grant(key)

    # ---------- VRAM, granted per job on one GPU ----------
    def vram_reserve(self) -> float:
        """VRAM kept free on every GPU: the largest reserve any workspace asks for."""
        return self.infra_max("vram_reserve_gb")

    def vram_free(self, gpu: dict) -> float:
        """GB of `gpu` a new job may still be granted. Every grant counts in full even while
        its job is still loading, and memory used outside the platform's jobs (the desktop, other programs) is
        taken off as well."""
        granted = sum(g["vram"] for g in self.grants.values() if g.get("gpu") == gpu["index"])
        unused = max(0.0, granted - gpu.get("jobs_gb", 0.0))
        cap = gpu["mem_total_gb"] - self.vram_reserve()
        return round(max(0.0, min(cap - granted, cap - gpu["mem_used_gb"] - unused)), 1)

    def job_pids(self, g: dict) -> set[int]:
        """Processes of a `loop run` job: the helper's process tree and whatever still runs in the job's scope."""
        pids: set[int] = set()
        with contextlib.suppress(psutil.Error):
            proc = psutil.Process(g["pid"])
            pids = {proc.pid, *(c.pid for c in proc.children(recursive=True))}
        if g["unit"] and (base := pool_cgroup()):
            pids |= cgroup_pids(base / f"{g['unit']}.scope")
        return pids

    def sample_vram(self, snap: dict) -> None:
        """Measure what the platform's jobs use on each GPU, all together, which `vram_free` counts against their
        grants: their processes' memory where the driver lists it, else (WSL) what the GPU uses above its last
        level without grants; without such a level (a job restored from before it was recorded), nothing."""
        for gpu in snap["gpus"]:
            mine = [g for g in self.grants.values() if g.get("gpu") == gpu["index"]]
            if not mine:
                self.vram_base[gpu["index"]] = gpu["mem_used_gb"]
                continue
            if gpu["procs"]:
                pids = set().union(*(self.job_pids(g) for g in mine))
                jobs = sum(gb for pid, gb in gpu["procs"].items() if pid in pids)
            else:
                base = self.vram_base.get(gpu["index"])
                jobs = 0.0 if base is None else max(0.0, gpu["mem_used_gb"] - base)
            gpu["jobs_gb"] = round(jobs, 2)

    def police_vram(self, snap: dict) -> None:
        """VRAM has no hard per-job cap, so a job may use more than its grant while its GPU has room. Once less than
        the reserve is free there, the newest job with processes on that GPU is stopped, one at a time with
        VRAM_SETTLE_S for its memory to free, as the RAM pool stops its newest heavy process: the jobs that were
        there first keep running. Where the driver names no processes, the newest job granted the GPU goes. A GPU
        filled by programs outside the platform's jobs stops nothing, and neither does a stale reading (nvidia-smi
        hanging), which may predate a job that already stopped."""
        if snap["ts"] - snap.get("gpus_ts", snap["ts"]) > GPU_STALE_S:
            return
        reserve = self.vram_reserve()
        for gpu in snap["gpus"]:
            mine = [(k, g) for k, g in self.grants.items() if g.get("gpu") == gpu["index"]]
            if (not mine or gpu["mem_total_gb"] - gpu["mem_used_gb"] >= reserve
                    or time.monotonic() < self.vram_settle.get(gpu["index"], 0)):
                continue
            users = set(gpu["procs"]) | set(gpu.get("pids", ()))
            on = [(k, g) for k, g in mine if self.job_pids(g) & users] if users else mine
            if not on:
                continue
            key = max(on, key=lambda kg: kg[1]["started"])[0]
            self.vram_settle[gpu["index"]] = time.monotonic() + VRAM_SETTLE_S
            self.stop_job(key, f"stopped because GPU{gpu['index']} had less than {reserve} GB free and it was the "
                               "newest job on it; run it again once `loop infra` shows room there")

    def pick_gpu(self, vram: float) -> int | None:
        """The GPU with the least free VRAM that still fits `vram`, leaving larger holes for larger jobs."""
        fits = [(free, gpu["index"]) for gpu in self.infra.latest()["gpus"] if (free := self.vram_free(gpu)) >= vram]
        return min(fits)[1] if fits else None

    async def ram_grant(self, caller: dict, wait_s: float, pid: int, cmd: str, vram: float = 0.0,
                        job: str | None = None, within: str | None = None) -> dict:
        """Start the `loop run` job `pid` once the RAM pool is less than RAM_ADMIT full, and `vram` GB is free on one
        GPU when asked, waiting up to `wait_s`; one job starts per sample, so the next sees what the last one took.
        Only a job granted VRAM sees a GPU, and only that one. The job runs in its own scope in the pool, where
        `police_ram` can stop it. A detached `job` outlives the turn, and so does a `loop run` run `within` one."""
        if caller["kind"] not in ("worker", "meta-work"):
            raise AgentError("`loop run` belongs to workers and the meta-work")
        if job or within:
            jrow = self.db.one("SELECT unit FROM jobs WHERE id=? AND session=? AND status='running'", job or within,
                               caller["id"])
            if not jrow:
                raise AgentError(f"no running job {job or within} of this session")
        if job:
            self.db.update("jobs", {"id": job}, pid=pid)
        if vram:
            gpus = self.infra.latest()["gpus"]
            if not gpus:
                raise AgentError("--vram: no GPU on this host")
            most = round(max(g["mem_total_gb"] for g in gpus) - self.vram_reserve(), 1)
            if not 0 < vram <= most:
                raise AgentError(f"--vram must be more than 0 and at most {most} GB (the largest GPU minus the "
                                 f"{self.vram_reserve()} GB kept free)")
        key, deadline = next(self.grant_ids), time.time() + min(wait_s, 1800)
        self.ram_waiting[key] = {"sid": caller["id"], "vram": vram, "cmd": cmd[:120], "since": time.time()}
        try:
            while True:
                ts, gpu = self.infra.latest()["ts"], self.pick_gpu(vram) if vram else None
                # No await between the check and the start, so concurrent requests are served one at a time
                if self.ram_share() < RAM_ADMIT and ts > self.ram_admitted and (not vram or gpu is not None):
                    self.ram_admitted = ts
                    unit = f"{unit_prefix()}-{caller['id']}-job{key}-{int(time.time() * 1000)}" \
                        if systemd_available() else None
                    unit = jrow["unit"] if job else unit
                    # A `loop run` inside a detached job belongs to that job: it is not ended with the session's
                    # turn, it survives a restart (its unit name is outside the `<prefix>-*` pattern startup stops)
                    # and it is stopped with the job
                    if within and jrow["unit"]:
                        unit = f"{jrow['unit']}-{key}"
                    text, env = f"started with the RAM pool {self.ram_share():.0%} in use", {}
                    # Thread defaults the job's own environment overrides (`loop run` applies them first): enough
                    # for the cores left free, and BLAS/OpenMP threads that sleep rather than spin while they wait
                    threads = self.job_threads()
                    defaults = {**{k: str(threads) for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
                                                             "OPENBLAS_NUM_THREADS")},
                                "OMP_WAIT_POLICY": "PASSIVE"}
                    if vram:
                        # The GPU's level without grants is what its jobs are measured against
                        if not any(g.get("gpu") == gpu for g in self.grants.values()):
                            self.vram_base[gpu] = next(g["mem_used_gb"] for g in self.infra.latest()["gpus"]
                                                       if g["index"] == gpu)
                        text += f" and {vram} GB VRAM on GPU{gpu}"
                        # nvidia-smi numbers GPUs by PCI bus, CUDA by speed unless told otherwise
                        env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(gpu),
                               "LOOP_VRAM_GB": str(vram)}
                    self.grants[key] = {"sid": caller["id"], "pid": pid, "cmd": cmd[:120], "cmd_full": cmd[:500],
                                        "unit": unit,
                                        "rss_gb": 0.0, "oom": 0, "started": time.time(), "vram": vram,
                                        "threads": threads,
                                        **({"gpu": gpu, "vram_base": self.vram_base.get(gpu)}
                                           if vram else {}),
                                        **({"job": job or within} if job or within else {}),
                                        **({"within": True} if within else {})}
                    if job:
                        env["LOOP_JOB"] = job
                        self.db.update("jobs", {"id": job}, grant_info=json.dumps(
                            {k: v for k, v in self.grants[key].items() if k not in GRANT_SAMPLES}))
                    return {"text": text, "grant": key, "argv": job_scope_argv(unit) if unit else [], "env": env,
                            "defaults": defaults}
                if time.time() >= deadline:
                    what = "the RAM pool has no room" + (f" or {vram} GB VRAM is not free" if vram else "")
                    raise AgentError(f"{what} after {wait_s / 60:.0f} min ({self.ram_text()}); run it later or "
                                     "make it use less memory")
                await asyncio.sleep(1)
        finally:
            self.ram_waiting.pop(key, None)

    def jobs_view(self) -> list[dict]:
        """Running `loop run` jobs for the infra page, oldest first: whose they are, what they use, and whether they
        outlive their session's turn (detached, or run within a detached job)."""
        rows = []
        for g in sorted(self.grants.values(), key=lambda g: g["started"]):
            s = self.db.session(g["sid"]) or {}
            # The cores the job may run on (e.g. under taskset); its `loop run` helper is not pinned
            cores: set[int] = set()
            for pid in self.job_pids(g) - {g["pid"]}:
                with contextlib.suppress(psutil.Error):
                    cores |= set(psutil.Process(pid).cpu_affinity())
            rows.append({"sid": g["sid"], "kind": s.get("kind", ""), "role": s.get("role", ""),
                         "ws": s.get("ws", ""), "goal": s.get("goal", ""), "cmd": g.get("cmd_full") or g["cmd"],
                         "started": g["started"], "rss_gb": g["rss_gb"], "cpu_cores": g.get("cpu_cores"),
                         "cpus": core_ranges(cores), "vram": g["vram"],
                         "gpu": g.get("gpu"),
                         "job": g.get("job"), "within": bool(g.get("within"))})
        return rows

    def ram_release(self, caller: dict, key: int, killed: bool = False, job: str | None = None) -> dict:
        if job and key not in self.grants:  # started before a platform restart, restored under another key
            key = next((k for k, g in self.grants.items() if g.get("job") == job and not g.get("within")), key)
        g, peak = self.grants.get(key), None
        if g and g["sid"] == caller["id"]:
            peak = g.get("peak_gb")
            self.end_grant(key, killed)
        # A job stopped by the platform was not killed by the kernel; `loop run` says why
        stopped = self.stopped.get(key)
        text = self.stopped.pop(key)[1] if stopped and stopped[0] == caller["id"] else "released"
        return {"text": text, "peak_gb": peak}

    def end_grant(self, key: int, killed: bool = False) -> None:
        """End a job's record and stop whatever still runs in its scope, so no job outlives it. A job the kernel
        killed (seen by the sampler, or as a SIGKILL exit by `loop run`, since a scope emptied by the kill may
        vanish before the next sample) goes into the turn's memory note."""
        g = self.grants.pop(key, None)
        if not g:
            return
        if g["unit"]:
            kill_unit(g["unit"])
        if (g["oom"] or killed) and g["sid"] in self.turn_mem:
            what = f"{g['oom']} of its processes were killed" if g["oom"] else "it was killed (SIGKILL)"
            self.turn_mem[g["sid"]]["over"].append(f"`{g['cmd']}`: {what}, most likely by the kernel when the RAM "
                                                   "pool ran out")

    @staticmethod
    def mem_note(mem: dict) -> str:
        """What the meta-work should know about a finished turn's memory: its peak when it was large, and any
        process the kernel or `loop run` killed for memory."""
        parts = [f"peak {mem['peak']:.1f} GB"] if mem.get("peak", 0) >= 1 else []
        if mem.get("oom"):
            parts.append(f"{mem['oom']} process(es) killed by the kernel for lack of memory")
        parts += mem.get("over", [])
        return "; ".join(parts)

    # ---------- detached jobs (`loop run --detach`) ----------
    def job_register(self, caller: dict, cmd: str) -> dict:
        """Record a detached job before its `loop run` helper starts outside the caller's turn. The helper asks
        to start like any `loop run`; the job may outlive the turn and a platform restart, and its result
        goes back to the caller when it ends (`job_end`)."""
        if caller["kind"] not in ("worker", "meta-work"):
            raise AgentError("`loop run` belongs to workers and the meta-work")
        jid = f"j-{secrets.token_hex(3)}"
        log_path = self.gdir(caller["ws"], caller["goal"]) / "runs" / caller["id"] / "artifacts" / "jobs" / f"{jid}.log"
        unit = job_unit(jid) if systemd_available() else None
        self.db.x("INSERT INTO jobs(id, session, ws, goal, cmd, log, unit, started, status) VALUES(?,?,?,?,?,?,?,?,?)",
                  jid, caller["id"], caller["ws"], caller["goal"], cmd[:500], str(log_path), unit, time.time(),
                  "running")
        hours = workspace_config(self.wsp(caller["ws"]))["job_timeout_h"]
        return {"id": jid, "log": str(log_path), "tmp": str(jobs_tmp_root() / jid), "unit": unit,
                "text": f"Started job {jid} in the background (output: {log_path}; stopped after {hours} h). End "
                        "your turn now instead of waiting: when the job ends, its result arrives as a new message."}

    def job_report(self, caller: dict, jid: str, text: str) -> str:
        if not self.db.one("SELECT 1 FROM jobs WHERE id=? AND session=?", jid, caller["id"]):
            raise AgentError(f"no job {jid} of this session")
        self.job_end(jid, text)
        return "reported"

    def job_end(self, jid: str, how: str) -> None:
        """Close a running detached job once, whichever comes first (its helper's report, a helper gone without
        one, job_timeout_h), and hand its result to its session."""
        j = self.db.one("SELECT * FROM jobs WHERE id=? AND status='running'", jid)
        if not j:
            return
        self.db.update("jobs", {"id": jid}, status="done", ended=time.time())
        shutil.rmtree(jobs_tmp_root() / jid, ignore_errors=True)
        try:
            with open(j["log"], "rb") as fh:
                fh.seek(max(0, fh.seek(0, os.SEEK_END) - 4000))
                tail = fh.read().decode(errors="replace").splitlines()[-20:]
        except OSError:
            tail = []
        mins = (time.time() - j["started"]) / 60
        took = f"{mins:.0f} min" if mins < 90 else f"{mins / 60:.1f} h"
        log_text = f"End of {j['log']}:\n```\n" + "\n".join(tail) + "\n```" if tail else f"{j['log']} is empty."
        self.db.update("jobs", {"id": jid}, result=f"[Job {jid} ended] {how} after {took}: `{j['cmd']}`\n{log_text}")
        self.wake_for_jobs(j["session"])

    def police_jobs(self) -> None:
        """End detached jobs whose helper is gone without a report, and stop those past job_timeout_h."""
        for j in self.db.q("SELECT * FROM jobs WHERE status='running'"):
            age, hours = time.time() - j["started"], workspace_config(self.wsp(j["ws"]))["job_timeout_h"]
            if age > hours * 3600:
                self.job_end(j["id"], f"stopped at the {hours} h limit for jobs")
                self.kill_job(j)
            elif (not psutil.pid_exists(j["pid"])) if j["pid"] else age > 120:
                self.job_end(j["id"], "its `loop run` helper ended without reporting how the job ended")

    def stop_jobs(self, why: str, **where) -> list[str]:
        """Stop the running detached jobs matching `where`. Their result is not delivered: whoever stops them
        knows why."""
        cond = " AND ".join(f"{k}=?" for k in where)
        rows = self.db.q(f"SELECT * FROM jobs WHERE status='running' AND {cond}", *where.values())
        for j in rows:
            self.db.update("jobs", {"id": j["id"]}, status="stopped", ended=time.time(), result=why, reported=1)
            self.kill_job(j)
        return [j["id"] for j in rows]

    def kill_job(self, j: dict) -> None:
        for key in [k for k, g in self.grants.items() if g.get("job") == j["id"]]:
            self.end_grant(key)
        if j["unit"]:  # the helper's service, the job's scope and those of `loop run`s within it
            subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", f"{j['unit']}.service",
                            f"{j['unit']}.scope", f"{j['unit']}-*.scope"], capture_output=True)
        elif j["pid"]:  # without systemd the helper leads a process group of its own, the job in it
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(j["pid"], signal.SIGKILL)
        shutil.rmtree(jobs_tmp_root() / j["id"], ignore_errors=True)

    def wake_for_jobs(self, sid: str) -> None:
        """Hand a session the results of its ended detached jobs: a worker gets them as a turn of its own once
        it is idle, or the meta-work gets them as an event when that turn cannot start; the meta-work gets its
        own as an event."""
        s = self.db.session(sid)
        jobs = self.db.q("SELECT id, result FROM jobs WHERE session=? AND status='done' AND reported=0 "
                         "ORDER BY ended", sid)
        if not s or not jobs or (s["kind"] == "worker" and s["status"] == "running"):
            return  # a worker in a turn gets them when that turn ends (worker_turn)
        ws, gid = s["ws"], s["goal"]
        if self.goal_row(ws, gid)["status"] != "running":
            return
        self.db.x(f"UPDATE jobs SET reported=1 WHERE id IN ({','.join('?' * len(jobs))})", *(j["id"] for j in jobs))
        text = "\n\n".join(j["result"] for j in jobs)
        if s["kind"] == "worker":
            try:
                if not Path(s["cwd"]).exists():
                    raise AgentError("its worktree no longer exists")
                self.check_limit(s["agent"])
                self.check_dispatch(ws, gid)
            except AgentError as exc:
                text = (f"[System] {sid} was not resumed with the result of its job ({exc}); pass it on with "
                        f"`loop send {sid}` if it is still needed.\n{text}")
            else:
                self.db.update("sessions", {"id": sid}, status="running")
                self._count_dispatch(ws, gid)
                self.spawn(self.worker_turn(sid, text, woken=", ".join(j["id"] for j in jobs)), goal=(ws, gid))
                return
        self.db.push_inbox(ws, gid, "system", {"text": text})
        self.kick(ws, gid)

    # ---------- running one turn of any session ----------
    @staticmethod
    def language_rule(lang: str) -> str:
        """Meta-only rule: human-facing text in the goal language, agent-to-agent text in English."""
        if not lang or lang.lower() == "english":
            return ""
        return (f" LANGUAGE: the human reads {lang}. Write plan.md, notes, reports and chat replies "
                f"in {lang}. Write worker prompts in English (workers report in English); keep code, identifiers, "
                "commands and commit messages in the project's usual language.")

    def mode_text(self, sess: dict) -> str:
        ws, gid = sess["ws"], sess["goal"]
        p, gdir = self.wsp(ws), self.gdir(ws, gid)
        lang = self.language_rule(goal_config(p, gid)["goal"].get("language"))
        if sess["kind"] == "meta-work":
            return (f"MODE: META-WORK for goal {gid} in workspace {ws}. Goal dir: {gdir}. "
                    f"Workspace root: {p}. Follow the META-WORK section of your AGENTS.md.{lang}")
        if sess["kind"] == "meta-advisor":
            return (f"MODE: META-ADVISOR for goal {gid} in workspace {ws}. Goal directory: {gdir}. "
                    "Follow the META-ADVISOR section of your AGENTS.md. Read spec.md, goal.md and plan.md to "
                    "understand the objective and the current state. "
                    f"Write your advice in {goal_config(p, gid)['goal'].get('language') or 'English'}.")
        orch = self.goal_row(ws, gid)["meta_work"] or "not started"
        return (f"MODE: META-CHAT for goal {gid} in workspace {ws}. Goal dir: {gdir}. Workspace root: {p}. "
                f"Meta-work session: {orch}. Follow the META-CHAT section of your AGENTS.md; "
                f"worker coordination belongs to the meta-work, steer it with `loop note`.{lang}")

    def session_conf(self, sess: dict) -> dict:
        """Agent settings for the next turn: fixed by the human config for meta-work and workers,
        chosen at creation for a meta-chat."""
        keys = ("model", "effort", "autocompact")
        if sess["kind"] in ("meta-chat", "meta-advisor"):
            return {k: sess[k] for k in keys}
        mc = goal_config(self.wsp(sess["ws"]), sess["goal"])["goal"]
        conf = mc["meta_work"] if sess["kind"] == "meta-work" else mc["workers"].get(sess["agent"], {})
        return {k: None if conf.get(k) is None else str(conf[k]) for k in keys}

    def build_spec(self, sess: dict, prompt: str, n: int) -> RunSpec:
        ws, gid, sid = sess["ws"], sess["goal"], sess["id"]
        p, gdir = self.wsp(ws), self.gdir(ws, gid)
        cfg = goal_config(p, gid)
        worker = sess["kind"] == "worker"
        conf = self.session_conf(sess)
        if any(conf[k] != sess[k] for k in conf):  # the human changed the config since the last turn
            self.db.update("sessions", {"id": sid}, **conf)
        venv = p / ".venv"
        # Workers created before they got a token (for `loop run` and `loop infra`) receive one now
        token = sess["token"] or secrets.token_hex(16)
        if not sess["token"]:
            self.db.update("sessions", {"id": sid}, token=token)
        env = {"LOOP_ROLE": sess["kind"], "TMPDIR": scratch_root() / f"{sid}-t{n}",
               "VIRTUAL_ENV": venv, "PATH_PREPEND": f"{self.bin_dir}{os.pathsep}{venv / 'bin'}",
               "LOOP_API": self.api_url, "LOOP_TOKEN": token,
               "LOOP_WS": p, "LOOP_GOAL": gid, "LOOP_GOAL_DIR": gdir, "LOOP_SESSION": sid,
               "LOOP_GUARD_PATTERNS": json.dumps(cfg["guard"]["deny_patterns"] or [])}
        if sess["kind"] != "meta-chat":  # a GPU is handed to one job at a time, by `loop run --vram`
            env["CUDA_VISIBLE_DEVICES"] = ""
        spec = RunSpec(agent=sess["agent"], kind="worker" if worker else "meta", cwd=Path(sess["cwd"]),
                       prompt=prompt, model=conf["model"], effort=conf["effort"], autocompact=conf["autocompact"],
                       resume_id=sess["vendor_id"], env=env)
        if worker:
            if sess["agent"] == "codex":
                spec.codex_home = workspace.codex_home(p, "worker")
            elif sess["agent"] == "agy":
                spec.system_text = workspace.agy_worker_text(p)
            else:
                spec.plugin_dir, spec.append_file = p / "worker" / "claude-plugin", p / "worker" / "AGENTS.md"
                spec.settings_file, spec.add_dirs = workspace.claude_settings(p, "worker"), [gdir, p / "data"]
        else:
            spec.system_text = self.mode_text(sess)
            if sess["agent"] == "codex":
                spec.codex_home = workspace.codex_home(p, "meta")
            elif sess["agent"] != "agy":  # agy reads meta/AGENTS.md and meta/.agents/skills from its cwd
                spec.settings_file, spec.add_dirs = workspace.claude_settings(p, "meta"), [p]
        if sess["agent"] == "codex":
            spec.last_message_file = self.logs(ws, sid)[0].with_name(f"{sid}.t{n}.last.txt")
        elif sess["agent"] == "agy":
            workspace.agy_plugin()
        if sess["kind"] == "meta-advisor":
            spec.resume_id = None  # every consultation starts with a fresh context
        return spec

    async def run_turn(self, sess: dict, prompt: str, start_commit: str | None = None):
        sid, ws, gid = sess["id"], sess["ws"], sess["goal"]
        n = sess["turns"] + 1
        spec = self.build_spec(sess, prompt, n)
        raw, events = self.logs(ws, sid)
        cfg = goal_config(self.wsp(ws), gid)
        self.db.update("sessions", {"id": sid}, status="running", updated=time.time())
        turn_id = self.db.x("INSERT INTO turns(session, n, started, status, start_commit) VALUES(?,?,?,?,?)",
                            sid, n, time.time(), "running", start_commit)
        self.publish("session_status", ws=ws, goal=gid, session=sid, status="running", turn=n)
        self._log_event(ws, gid, sid, n, events, {"type": "turn", "n": n, "prompt": prompt})

        async def on_event(evt: dict) -> None:
            if evt["type"] == "init" and evt.get("vendor_id") and not sess["vendor_id"]:
                self.db.update("sessions", {"id": sid}, vendor_id=evt["vendor_id"])
            self.bus.publish({"type": "session_event", "ws": ws, "goal": gid, "session": sid, "turn": n,
                              "event": evt})

        # Workers and the meta-work run project jobs, so both may get a per-session cap; a meta-chat only reads
        mem = cfg["infra"]["worker_memory_max_gb"] if sess["kind"] in ("worker", "meta-work", "meta-advisor") else None
        self.runner.set_pool(self.pool_gb())
        self.turn_mem[sid] = {"peak": 0.0, "oom": 0, "over": []}
        # Each turn gets its own scratch dir; it goes when the turn ends (its processes are stopped by then),
        # and so do its `loop run` jobs, all but the detached ones
        scratch = Path(spec.env["TMPDIR"])
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            res = await self.runner.run(sid, spec, raw_log=raw, event_log=events, on_event=on_event,
                                        timeout_s=cfg["turn_timeout_min"] * 60, memory_max_gb=mem)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            mine = [k for k, g in self.grants.items() if g["sid"] == sid and not g.get("job")]
            jobs = [self.grants[k]["cmd"] for k in mine]
            for key in mine:
                self.end_grant(key)
            for key in [k for k, (owner, _) in self.stopped.items() if owner == sid]:
                self.stopped.pop(key)
            res_mem = self.turn_mem.pop(sid, {})
        res.memory = self.mem_note(res_mem)
        if res.quota:
            self.set_quota(sess["agent"], res.quota)
        # `loop run` jobs have scopes of their own, so the turn's scope did not list them among its leftovers
        if not res.stopped:
            res.leftover = (*res.leftover, *(c for c in jobs if c not in res.leftover))
        status = "stopped" if res.stopped else ("failed" if res.error else "idle")
        cur = self.db.session(sid)
        self.db.update("sessions", {"id": sid}, vendor_id=res.vendor_id or cur["vendor_id"], turns=n,
                       tokens=cur["tokens"] + res.tokens,
                       context=res.context or cur["context"], status=status, updated=time.time())
        self.db.update("turns", {"id": turn_id}, ended=time.time(), status=status, exit_code=res.exit_code,
                       tokens=res.tokens, final=(res.final or res.error) if sess["kind"] == "meta-advisor"
                       else (res.final or res.error)[:20000])
        self._log_event(ws, gid, sid, n, events, {"type": "turn_end", "n": n, "status": status, "error": res.error,
                                                   "tokens": res.tokens})
        self.publish("session_status", ws=ws, goal=gid, session=sid, status=status, turn=n)
        return res, n

    def _log_event(self, ws: str, gid: str, sid: str, n: int, path: Path, evt: dict) -> None:
        evt["ts"] = time.time()
        with path.open("a") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
        self.bus.publish({"type": "session_event", "ws": ws, "goal": gid, "session": sid, "turn": n, "event": evt})

    # ---------- goals ----------
    def kickoff_text(self, ws: str, gid: str) -> str:
        p, gdir = self.wsp(ws), self.gdir(ws, gid)
        cfg = goal_config(p, gid)
        mc = cfg["goal"]
        agents = "\n".join(f"  - `--agent {a}`: {c.get('model') or 'default model'} / effort {c.get('effort') or 'default'}"
                           for a, c in mc["workers"].items() if c.get("enabled", True))
        return (f"[Kickoff] Goal \"{mc['title']}\" ({gid}) in workspace {ws}.\n"
                f"- Goal dir: {gdir}. Read spec.md and goal.md first (human-owned, do not edit). "
                "You own plan.md there.\n"
                f"- Project repo: {p / 'repo'} (the human's checkout: read it, do not change it). Integration branch "
                f"{gitops.int_branch(gid)} is checked out at {gitops.int_worktree(p, gid)}.\n"
                f"- Worker agents (the human fixed model and effort; you pick the CLI and name the role):\n{agents}\n"
                f"- Your turns and the workers share a {self.pool_gb()} GB RAM pool; only jobs run with "
                "`loop run --vram <GB>` see a GPU.\n"
                f"- Workspace map: {p / 'PLATFORM.md'}. Cross-goal memory: {p / 'meta' / 'memory'}.")

    async def start_goal(self, ws: str, gid: str) -> None:
        p, m = self.wsp(ws), self.goal_row(ws, gid)
        if m["status"] in ("running", "awaiting_review", "done"):
            raise AgentError(f"goal is {m['status']}")
        limit = workspace_config(p)["limits"]["max_running_goals"]
        running = self.db.one("SELECT COUNT(*) c FROM goals WHERE ws=? AND status='running'", ws)["c"]
        if running >= limit:
            raise AgentError(f"{running} goals already running in this workspace (max_running_goals={limit})")
        mc = goal_config(p, gid)["goal"]
        # The base is recorded once, so the PR targets the branch the integration branch was cut from
        base = mc.get("base_branch") or m["base"] or gitops.current_branch(p / "repo")
        # A new goal starts from the latest base: work merged through earlier goals' PRs is pulled first
        if not gitops.branch_exists(p / "repo", gitops.int_branch(gid)):
            await asyncio.to_thread(gitops.pull_base, p, base)
        gitops.ensure_integration(p, gid, base)
        if not m["base"]:
            self.set_goal(ws, gid, base=base)
        if not m["meta_work"]:
            orch = self.new_session(ws, gid, "meta-work", "meta-work", mc["meta_work"], p / "meta")
            self.set_goal(ws, gid, meta_work=orch["id"])
            self.db.push_inbox(ws, gid, "kickoff", {"text": self.kickoff_text(ws, gid)})
        else:
            self.db.push_inbox(ws, gid, "system", {"text": "[System] The human resumed the goal. "
                                                           "Check `loop ls` and continue."})
        self.set_goal(ws, gid, status="running", started=time.time(), final_turn=0, idle_streak=0,
                         fail_streak=0, first_started=m["first_started"] or time.time())
        self.kick(ws, gid)

    async def stop_goal(self, ws: str, gid: str, status: str = "stopped", meta_work: bool = True) -> None:
        """Leave the running state; kill workers (and the meta-work unless it is the caller)."""
        m = self.goal_row(ws, gid)
        if m["status"] == "running":
            self.set_goal(ws, gid, status=status, active_s=self.budget(ws, gid)["hours"] * 3600,
                             started=None)
        elif m["status"] != status:
            self.set_goal(ws, gid, status=status)
        # A queued consultation has no process yet; cancel its inbox request as well as live advisors.
        for s in self.advisors(ws, gid):
            if s["status"] == "queued":
                self.db.update("sessions", {"id": s["id"]}, status="stopped", updated=time.time())
                self.advice_result(s, "stopped", "The goal stopped before the consultation started.")
        self.stop_jobs("the goal stopped", ws=ws, goal=gid)
        kinds = ("worker", "meta-work", "meta-advisor") if meta_work else ("worker", "meta-advisor")
        for s in self.db.q("SELECT id, kind FROM sessions WHERE ws=? AND goal=?", ws, gid):
            if s["kind"] in kinds:
                await self.runner.kill(s["id"], stopped=True)

    def kick(self, ws: str, gid: str) -> None:
        if (ws, gid) not in self.meta_active:
            self.spawn(self.meta_loop(ws, gid), goal=(ws, gid))

    async def meta_loop(self, ws: str, gid: str) -> None:
        key = (ws, gid)
        if key in self.meta_active:
            return
        self.meta_active.add(key)
        try:
            while True:
                m = self.goal_row(ws, gid)
                if m["status"] != "running":
                    return
                # Advice requests use the same durable inbox, but are delivered to the advisor, not meta-work.
                # `loop advise` dispatches its own at once; this picks up those left queued by a restart
                self.dispatch_advice(ws, gid)
                if not self.db.one("SELECT id FROM inbox WHERE ws=? AND goal=? AND consumed=0 AND deferred=0",
                                   ws, gid):
                    return
                # The events wait for the reset; workers on another CLI keep running meanwhile
                until = self.limit_until(goal_config(self.wsp(ws), gid)["goal"]["meta_work"]["agent"])
                if until:
                    self.wake_at(ws, gid, until)
                    return
                items = self.db.q("SELECT * FROM inbox WHERE ws=? AND goal=? AND consumed=0 ORDER BY id",
                                  ws, gid)
                # Items stay in flight (2) until the turn completes; failures put them back (0)
                ids = ",".join(str(i["id"]) for i in items)
                self.db.x(f"UPDATE inbox SET consumed=2 WHERE id IN ({ids})")
                try:
                    progressed = await self.meta_turn(ws, gid, items)
                except Exception as exc:
                    self.db.x(f"UPDATE inbox SET consumed=0 WHERE id IN ({ids}) AND consumed=2")
                    log.exception("meta turn failed for %s/%s", ws, gid)
                    await self.stop_goal(ws, gid, status="paused")
                    self.publish("alert", ws=ws, goal=gid, text=f"Meta-work turn crashed, goal paused: {exc!r}")
                    return
                self.db.x(f"UPDATE inbox SET consumed=1 WHERE id IN ({ids}) AND consumed=2")
                await self.after_meta_turn(ws, gid, items, progressed)
        finally:
            self.meta_active.discard(key)

    async def meta_turn(self, ws: str, gid: str, items: list[dict]) -> bool:
        """Run one meta-work turn on the given events. Returns True when the integration branch moved (the
        meta-work's own commits or merges), which counts as progress even with no worker running."""
        p, m = self.wsp(ws), self.goal_row(ws, gid)
        mc = goal_config(p, gid)["goal"]
        orch = self.db.session(m["meta_work"])
        rotate = to_tokens(mc["meta_work"].get("rotate_at_tokens"))
        # A vendor session cannot move between CLIs, so a new meta-work agent means a fresh session
        if orch["agent"] != mc["meta_work"]["agent"] or (rotate and orch["context"] > rotate):
            # Fresh meta-work session; plan.md carries the state across
            self.db.update("sessions", {"id": orch["id"]}, status="retired")
            orch = self.new_session(ws, gid, "meta-work", "meta-work", mc["meta_work"], p / "meta")
            self.set_goal(ws, gid, meta_work=orch["id"])
            items = [{"kind": "handoff", "payload": json.dumps({"text": (
                "[Handoff] You are a fresh meta-work session replacing a previous one (its context grew too "
                "large, or the human switched the meta-work agent). Regain state from plan.md, notes.md and `loop ls`.\n\n"
                + self.kickoff_text(ws, gid))})}] + items
        # A running agent's pending edit to a human-owned file is not the human's: its own turn end reverts it
        busy = self.db.one("SELECT 1 FROM sessions WHERE ws=? AND status='running'", ws)
        workspace.commit_external(p, protected=not busy)
        # The turn carries its events; resources and budget come only when they matter (the budget once mostly
        # spent; RAM, disk and usage limits as events when they run low), and `loop infra` / `loop budget` on demand
        b = self.budget(ws, gid)
        text = digest.envelope(items, digest.budget_line(b) if digest.budget_low(b) else None)
        wt = gitops.int_worktree(p, gid)
        start = gitops.head(wt) if wt.exists() else None
        repo_before = self.checkout_state(p / "repo")
        res, n = await self.run_turn(orch, text, start_commit=start)
        reverted = workspace.enforce_protected(p)
        if res.leftover:
            self.db.push_inbox(ws, gid, "system", {"text": leftover_note(res.leftover, "your last turn")},
                               deferred=True)
        if res.memory:
            self.db.push_inbox(ws, gid, "system", {"text": f"[System] Memory in your last turn: {res.memory}"},
                               deferred=True)
        # The meta-work's project changes are saved after every turn, so none depends on it committing them
        end = start
        if wt.exists():
            try:
                async with self.int_lock(ws, gid):
                    end = self.checkpoint(ws, gid, orch["id"], f"{gid} turn {n}")
            except AgentError as exc:
                end = gitops.head(wt)
                self.db.push_inbox(ws, gid, "system", {"text": f"[System] At the end of your turn the platform {exc}"},
                                   deferred=True)
                self.publish("alert", ws=ws, goal=gid, text=f"Meta-work changes left uncommitted: {exc}")
            self.db.x("UPDATE turns SET end_commit=? WHERE session=? AND n=?", end, orch["id"], n)
        # The guard misses some shell writes; a changed repo/ is reported, not reverted (it may be the human's work)
        if self.checkout_state(p / "repo") != repo_before:
            self.db.push_inbox(ws, gid, "system", {"text": (
                "[System] repo/ (branch, HEAD or files) changed during your turn. It is the human's checkout: make "
                "project changes in the integration worktree only.")}, deferred=True)
            self.publish("alert", ws=ws, goal=gid, text=f"repo/ changed during meta-work turn {n}; please check it.")
        workspace.commit(p, f"meta-work({orch['id']})", f"meta-work({orch['id']}): {gid} turn {n}", protected=False)
        ids = ",".join(str(i["id"]) for i in items if "id" in i)
        if res.limited_until:
            # The CLI refused to run: no meta turn spent and no failure counted; the same events are redelivered
            self.vendor_limits[orch["agent"]] = res.limited_until
            if ids:
                self.db.x(f"UPDATE inbox SET consumed=0 WHERE id IN ({ids})")
            for i in items:  # a handoff built for this turn only must reach the session's first real turn
                if "id" not in i:
                    self.db.push_inbox(ws, gid, i["kind"], json.loads(i["payload"]))
            self.publish("alert", ws=ws, goal=gid, text=f"Meta-work CLI {orch['agent']} hit its usage limit; the goal "
                                                        f"resumes after {clock(res.limited_until)}.")
            return end != start
        m = self.goal_row(ws, gid)
        fields = {"meta_turns": m["meta_turns"] + 1, "tokens": m["tokens"] + res.tokens}
        if reverted:
            self.db.push_inbox(ws, gid, "system", {"text": "[System] Reverted your edits to human-owned files: "
                                                           + ", ".join(reverted)}, deferred=True)
            self.publish("alert", ws=ws, goal=gid, text="Reverted edits to human-owned files (backup in "
                                                            ".loop/reverted/): " + ", ".join(reverted))
        if res.stopped and ids:  # a human-stopped turn: keep its events for the next trigger
            self.db.x(f"UPDATE inbox SET consumed=0, deferred=1 WHERE id IN ({ids})")
        if res.error and not res.stopped:
            fields["fail_streak"] = m["fail_streak"] + 1
            if ids:  # redeliver the same events on the next attempt
                self.db.x(f"UPDATE inbox SET consumed=0 WHERE id IN ({ids})")
            if fields["fail_streak"] >= 3:
                self.db.update("goals", {"ws": ws, "id": gid}, **fields)
                await self.stop_goal(ws, gid, status="paused")
                self.publish("alert", ws=ws, goal=gid, text=f"Meta-work failed 3 times: {res.error}")
                return end != start
            await asyncio.sleep(min(60, 10 * fields["fail_streak"]))
        else:
            fields["fail_streak"] = 0
        self.db.update("goals", {"ws": ws, "id": gid}, **fields)
        return end != start

    async def after_meta_turn(self, ws: str, gid: str, items: list[dict], progressed: bool = False) -> None:
        m = self.goal_row(ws, gid)
        if m["status"] != "running":
            return
        if m["final_turn"] == 1 and any(i["kind"] == "budget" for i in items):
            reason = (self.exhausted(self.budget(ws, gid)) or ("limit",))[0]
            await self.finish(ws, gid, "partial", f"Budget exhausted ({reason}); the meta-work did not "
                                                  "close the goal on its final turn.", [], by="platform")
            return
        self.check_budget(ws, gid)
        pending = self.db.one("SELECT id FROM inbox WHERE ws=? AND goal=? AND consumed=0 AND deferred=0", ws, gid)
        jobs = self.db.one("SELECT 1 FROM jobs WHERE ws=? AND goal=? AND status='running'", ws, gid)
        if self.workers(ws, gid, running_only=True) or self.advisors(ws, gid) or pending or jobs \
                or self.goal_row(ws, gid)["final_turn"]:
            if progressed:  # work the meta-work did itself next to running workers counts too
                self.set_goal(ws, gid, idle_streak=0)
            return
        if (ws, gid) in self.ram_wait_goals:  # waiting for RAM is not idleness; the sampler sends the next event
            return
        # Nothing else will trigger a turn: this message continues the goal. A turn that moved the integration
        # branch did work itself and resets the count; otherwise the goal pauses after repeated idle turns
        streak = 0 if progressed else m["idle_streak"] + 1
        self.set_goal(ws, gid, idle_streak=streak)
        if streak <= 2:
            self.db.push_inbox(ws, gid, "system", {"text": (
                "[System] Nothing is running and the goal is still open, so this message starts your next turn. "
                "After three turns in a row that leave the integration branch unchanged, the goal pauses.")})
        else:
            await self.stop_goal(ws, gid, status="paused")
            self.publish("alert", ws=ws, goal=gid, text="Meta-work went idle; goal paused.")

    async def finish(self, ws: str, gid: str, status: str, report: str, verify: list[dict], by: str) -> None:
        gdir = self.gdir(ws, gid)
        await self.stop_goal(ws, gid, status="awaiting_review", meta_work=False)
        lines = [f"# Completion report\n", f"- Status: **{status}**", f"- Closed by: {by}",
                 f"- Time: {time.strftime('%Y-%m-%d %H:%M')}\n", report.strip() + "\n"]
        if verify:
            lines.append("## Verify commands\n")
            lines += [f"- `{v['cmd']}` → exit {v['code']}" for v in verify]
        (gdir / "report.md").write_text("\n".join(lines) + "\n")
        self.set_goal(ws, gid, outcome=status, ended=time.time())
        workspace.commit(self.wsp(ws), "control-plane", f"control-plane: {gid} closed ({status})")
        self.publish("alert", ws=ws, goal=gid, text=f"Goal closed as {status}; awaiting your review.")

    async def accept(self, ws: str, gid: str) -> list[str]:
        p, m = self.wsp(ws), self.goal_row(ws, gid)
        if m["status"] != "awaiting_review":
            raise AgentError("only a goal awaiting review can be accepted")
        tags = gitops.cleanup(p, gid, self.workers(ws, gid))
        self.set_goal(ws, gid, status="done")
        workspace.commit(p, "human", f"human: accepted {gid}")
        return tags

    def goal_base(self, ws: str, gid: str) -> str:
        """Branch the goal's work targets: goal.yaml base_branch, else the branch repo/ had checked out at start."""
        return (goal_config(self.wsp(ws), gid)["goal"].get("base_branch") or self.goal_row(ws, gid)["base"]
                or gitops.current_branch(self.wsp(ws) / "repo"))

    async def push_goal(self, ws: str, gid: str) -> dict:
        """Push an accepted goal's integration branch and open a PR into its base; the base branch is never touched."""
        m = self.goal_row(ws, gid)
        if m["status"] != "done":
            raise AgentError("accept the goal before pushing it")
        report = self.gdir(ws, gid) / "report.md"
        res = await asyncio.to_thread(gitops.push_goal, self.wsp(ws), gid, self.goal_base(ws, gid),
                                      goal_config(self.wsp(ws), gid)["goal"]["title"],
                                      report.read_text() if report.exists() else "")
        self.set_goal(ws, gid, pushed=time.time(), pr=res["pr"] or m["pr"])
        self.publish("goal_status", ws=ws, goal=gid, status="done")
        return res

    async def reopen(self, ws: str, gid: str, text: str, nid: int | None = None) -> None:
        """Back to running after review; the human's note (new, or an approved pending one) is delivered now."""
        m = self.goal_row(ws, gid)
        if m["status"] != "awaiting_review":
            raise AgentError("only a goal awaiting review can be reopened")
        self.set_goal(ws, gid, status="running", started=time.time(), final_turn=0, idle_streak=0, outcome=None)
        self.db.push_inbox(ws, gid, "system", {"text": "[System] The human reviewed your report and reopened "
                                                       "the goal; see the note."}, deferred=True)
        if nid:
            self.review_note(nid, True, "now", text)
        else:
            self.add_note(ws, gid, text or "Reopened after review.", mode="now")
        self.kick(ws, gid)

    # ---------- deletes ----------
    async def _quiesce(self, ws: str, gids: list[str]) -> list[dict]:
        """Stop every session of the goals and wait for their turns to wrap up, so nothing writes after a delete."""
        for gid in gids:
            await self.stop_goal(ws, gid)
        sessions = [s for gid in gids for s in self.db.q("SELECT * FROM sessions WHERE ws=? AND goal=?", ws, gid)]
        for s in sessions:
            await self.runner.kill(s["id"], stopped=True)
        # A killed turn still snapshots its worktree and writes its digest; finishing ones may kick the meta loop
        keys, end = {(ws, gid) for gid in gids}, time.time() + 120
        while pending := [t for t, k in self.task_goal.items() if k in keys]:
            if time.time() > end:
                raise AgentError("sessions are still finishing their turns; try the delete again shortly")
            await asyncio.wait(pending, timeout=end - time.time())
        return sessions

    def _forget(self, ws: str, gid: str | None = None) -> None:
        """Drop the runtime rows of one goal, or of every goal in the workspace when gid is None."""
        cond, args = ("ws=? AND goal=?", (ws, gid)) if gid else ("ws=?", (ws,))
        for table in ("turns", "messages"):
            self.db.x(f"DELETE FROM {table} WHERE session IN (SELECT id FROM sessions WHERE {cond})", *args)
        for table in ("sessions", "inbox", "notes"):
            self.db.x(f"DELETE FROM {table} WHERE {cond}", *args)
        self.db.x(f"DELETE FROM goals WHERE {cond.replace('goal', 'id')}", *args)

    async def delete_goal(self, ws: str, gid: str) -> None:
        """Remove a goal with its sessions, worktrees, branches, tags, files and runtime rows."""
        p = self.wsp(ws)
        self.goal_row(ws, gid)
        sessions = await self._quiesce(ws, [gid])
        await asyncio.to_thread(gitops.remove_goal, p, gid, [s for s in sessions if s["kind"] == "worker"])
        workspace.delete_goal_dir(p, gid)
        for s in sessions:
            for path in self.logs(ws, s["id"]):
                path.unlink(missing_ok=True)
        self._forget(ws, gid)
        self.publish("deleted", ws=ws, goal=gid)

    async def delete_workspace(self, ws: str) -> None:
        """Remove a workspace directory, project repo included, and the runtime rows of all its goals."""
        p = self.wsp(ws)
        if not (p / "loop.yaml").exists():
            raise AgentError(f"workspace {ws} not found")
        await self._quiesce(ws, workspace.list_goals(p))
        await asyncio.to_thread(shutil.rmtree, p)
        self._forget(ws)
        self.publish("deleted", ws=ws)

    # ---------- notes and human edits ----------
    def add_note(self, ws: str, gid: str, text: str, author: str = "human", mode: str = "queue",
                 status: str = "approved", source: str | None = None) -> int:
        text = text.strip()
        if not text:
            raise AgentError("empty note")
        nid = self.db.x("INSERT INTO notes(ws, goal, ts, text, author, status, mode, source_session) "
                        "VALUES(?,?,?,?,?,?,?,?)", ws, gid, time.time(), text, author, status, mode, source)
        if status == "approved":
            self.deliver_note(nid)
        self.publish("note", ws=ws, goal=gid, id=nid, status=status)
        return nid

    def deliver_note(self, nid: int) -> None:
        n = self.db.one("SELECT * FROM notes WHERE id=?", nid)
        path = self.gdir(n["ws"], n["goal"]) / "notes.md"
        with path.open("a") as fh:
            fh.write(f"\n## Note #{nid} · {time.strftime('%Y-%m-%d %H:%M')} · {n['author']}\n\n{n['text']}\n")
        workspace.commit(self.wsp(n["ws"]), "human", f"human: note #{nid} for {n['goal']}")
        self.db.push_inbox(n["ws"], n["goal"], "note", {"id": nid, "text": n["text"]},
                           deferred=n["mode"] != "now")
        if n["mode"] == "now":
            self.kick(n["ws"], n["goal"])

    def review_note(self, nid: int, approve: bool, mode: str = "queue", text: str | None = None) -> None:
        n = self.db.one("SELECT * FROM notes WHERE id=?", nid)
        if not n or n["status"] != "pending":
            raise AgentError("note is not pending")
        self.db.update("notes", {"id": nid}, status="approved" if approve else "rejected", mode=mode,
                       text=(text or n["text"]).strip())
        if approve:
            self.deliver_note(nid)
        self.publish("note", ws=n["ws"], goal=n["goal"], id=nid)

    def human_edit(self, ws: str, rel: str, text: str) -> None:
        if rel.endswith(".yaml"):
            data = yaml.safe_load(text) or {}
            errors = agents.config_errors((data.get("defaults") if rel == "loop.yaml" else data) or {})
            if errors:
                raise AgentError("; ".join(errors))
        change = workspace.human_write(self.wsp(ws), rel, text)
        parts = Path(rel).parts
        if change and len(parts) == 3 and parts[0] == "goals":
            gid, name = parts[1], parts[2]
            if self.goal_row(ws, gid)["status"] in ("running", "paused", "stopped"):
                self.db.push_inbox(ws, gid, "file_changed", {"name": name},
                                   deferred=name == "goal.yaml")
                self.kick(ws, gid)
        self.publish("file_changed", ws=ws, path=rel)

    def watch_project(self, ws: str, gid: str):
        """For a session that only reads the project (meta-chat, advisor): returns a function naming what the
        session changed anyway. The guard cannot see every shell write, so changes are reported, not reverted.
        In the integration worktree only uncommitted files count, and only while no meta-work turn (which
        changes and commits them) runs."""
        p, key = self.wsp(ws), (ws, gid)
        int_wt = gitops.int_worktree(p, gid)
        dirty = lambda: gitops.git(int_wt, "status", "--porcelain", check=False) if int_wt.exists() else ""
        repo_before, int_before = self.checkout_state(p / "repo"), None if key in self.meta_active else dirty()

        def changed() -> list[str]:
            hit = ["repo/"] if self.checkout_state(p / "repo") != repo_before else []
            if int_before is not None and key not in self.meta_active and dirty() != int_before:
                hit.append("the integration worktree")
            return hit
        return changed

    # ---------- advisor consultations ----------
    def advise(self, caller: dict, prompt: str) -> str:
        m = self.require_meta_work(caller)
        ws, gid = caller["ws"], caller["goal"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise AgentError("empty consultation")
        if caller["id"] != m["meta_work"]:
            raise AgentError("only the current meta-work can request advice")
        b = self.budget(ws, gid)
        # Reserve the advisor turn now, including the caller's unfinished turn in the admission check.
        active = self.db.one("SELECT COUNT(*) c FROM turns t JOIN sessions s ON s.id=t.session "
                             "WHERE s.ws=? AND s.goal=? AND s.kind='meta-work' AND t.status='running'", ws, gid)["c"]
        if m["final_turn"] or self.exhausted(b) or (b.get("max_meta_turns") and
                                                   b["meta_turns"] + active >= b["max_meta_turns"]):
            raise AgentError("meta turn budget exhausted; finalize with `loop done`")
        cfg = goal_config(self.wsp(ws), gid)
        conf = cfg["goal"]["meta_advisor"]
        errors = agents.config_errors({"meta_advisor": conf})
        if errors:
            raise AgentError("; ".join(errors))
        self.check_limit(conf["agent"])
        self.check_ram(ws, gid)
        sess = self.new_session(ws, gid, "meta-advisor", "meta-advisor", conf,
                                self.wsp(ws) / "meta", parent=caller["id"])
        self.db.update("sessions", {"id": sess["id"]}, status="queued")
        self.db.push_inbox(ws, gid, "advice_request", {"sid": sess["id"], "question": prompt.strip()})
        self.db.x("UPDATE goals SET meta_turns=meta_turns+1, idle_streak=0 WHERE ws=? AND id=?", ws, gid)
        self.publish("session_status", ws=ws, goal=gid, session=sess["id"], status="queued")
        # The advisor only reads the project, so it starts now, alongside the turn that asked
        self.dispatch_advice(ws, gid)
        return f"Consultation {sess['id']} started; the advice arrives as an [Advice] event."

    def dispatch_advice(self, ws: str, gid: str) -> None:
        for item in self.db.q("SELECT * FROM inbox WHERE ws=? AND goal=? AND kind='advice_request' AND consumed=0 "
                              "ORDER BY id", ws, gid):
            payload = json.loads(item["payload"])
            sess = self.db.session(payload["sid"])
            self.db.update("inbox", {"id": item["id"]}, consumed=1)
            if not sess or sess["status"] != "queued":
                continue
            self.db.update("sessions", {"id": sess["id"]}, status="running", updated=time.time())
            self.spawn(self.advisor_turn(sess["id"], payload["question"]), goal=(ws, gid))

    def advice_result(self, sess: dict, status: str, text: str) -> None:
        """Persist one answer per consultation, including failures and interrupted turns."""
        ws, gid, sid = sess["ws"], sess["goal"], sess["id"]
        self.db.x("UPDATE inbox SET consumed=1 WHERE ws=? AND goal=? AND kind='advice_request' "
                  "AND json_extract(payload, '$.sid')=?", ws, gid, sid)
        payload = json.dumps({"sid": sid, "text": f"[Advice] {sid} · {status}\n{text}"})
        self.db.x("INSERT INTO inbox(ws, goal, ts, kind, payload) SELECT ?, ?, ?, 'advice', ? "
                  "WHERE NOT EXISTS (SELECT 1 FROM inbox WHERE ws=? AND goal=? AND kind='advice' "
                  "AND json_extract(payload, '$.sid')=?)", ws, gid, time.time(), payload, ws, gid, sid)

    def recover_advice(self) -> None:
        # A crash can land between saving the final text and delivering it. Reuse that text, never rerun the
        # consultation; an unfinished turn becomes an interruption notice. Queued requests still dispatch normally.
        rows = self.db.q("SELECT * FROM sessions s WHERE kind='meta-advisor' AND status!='queued' AND NOT EXISTS "
                         "(SELECT 1 FROM inbox i WHERE i.kind='advice' AND json_extract(i.payload, '$.sid')=s.id)")
        for s in rows:
            turn = self.db.one("SELECT * FROM turns WHERE session=? ORDER BY n DESC LIMIT 1", s["id"])
            complete = s["status"] == "idle" and turn and turn["ended"]
            self.advice_result(s, "completed" if complete else s["status"],
                               turn["final"] if complete else "The consultation was interrupted before advice was delivered.")

    async def advisor_turn(self, sid: str, question: str) -> None:
        sess = self.db.session(sid)
        ws, gid = sess["ws"], sess["goal"]
        try:
            if self.goal_row(ws, gid)["status"] != "running":
                self.db.update("sessions", {"id": sid}, status="stopped", updated=time.time())
                self.advice_result(sess, "stopped", "The goal stopped before the consultation started.")
                return
            prompt = f"[Consultation]\n{question}"  # the mode text goes in as system text (build_spec)
            changed = self.watch_project(ws, gid)
            res, _ = await self.run_turn(sess, prompt)
            if hit := changed():
                self.publish("alert", ws=ws, goal=gid, text=f"Advisor {sid} changed {', '.join(hit)}; please check it.")
            workspace.enforce_protected(self.wsp(ws))
            self.db.x("UPDATE goals SET tokens=tokens+? WHERE ws=? AND id=?", res.tokens, ws, gid)
            text = res.final or res.error or "The advisor returned no advice."
            if res.error and res.final:
                text = f"{res.error}\n\n{text}"
            if res.limited_until:
                self.vendor_limits[sess["agent"]] = res.limited_until
                self.db.x("UPDATE goals SET meta_turns=MAX(0, meta_turns-1) WHERE ws=? AND id=?", ws, gid)
                text += f"\nThe CLI is at its usage limit until {clock(res.limited_until)}; this turn was not counted."
            if res.leftover:
                text += "\n" + leftover_note(res.leftover, "the advisor turn")
            self.advice_result(sess, "stopped" if res.stopped else "failed" if res.error else "completed", text)
        except Exception as exc:
            log.exception("advisor turn failed for %s", sid)
            self.db.update("sessions", {"id": sid}, status="failed", updated=time.time())
            self.db.x("UPDATE turns SET status='failed', ended=?, final=? WHERE session=? AND status='running'",
                      time.time(), str(exc), sid)
            self.advice_result(sess, "failed", f"Consultation failed inside the platform: {exc}")
        finally:
            self.publish("session_status", ws=ws, goal=gid, session=sid, status=self.db.session(sid)["status"])
        self.check_budget(ws, gid)
        self.kick(ws, gid)

    # ---------- workers ----------
    def caller(self, token: str | None) -> dict:
        sess = self.db.one("SELECT * FROM sessions WHERE token=?", token) if token else None
        if not sess:
            raise AgentError("unknown session token (only sessions started by agent-loop may use `loop`)")
        return sess

    def own_worker(self, caller: dict, sid: str) -> dict:
        w = self.db.session(sid)
        if not w or w["kind"] != "worker" or w["goal"] != caller["goal"] or w["ws"] != caller["ws"]:
            raise AgentError(f"no worker {sid} in this goal (see `loop ls`)")
        return w

    def require_meta_work(self, caller: dict) -> dict:
        if caller["kind"] != "meta-work":
            raise AgentError("only the goal meta-work can do this; propose a note with `loop note` instead")
        m = self.goal_row(caller["ws"], caller["goal"])
        if m["status"] != "running":
            raise AgentError(f"goal is {m['status']}")
        return m

    async def spawn_worker(self, caller: dict, agent: str, role: str, prompt: str) -> str:
        self.require_meta_work(caller)
        ws, gid = caller["ws"], caller["goal"]
        p = self.wsp(ws)
        workers = goal_config(p, gid)["goal"]["workers"]
        enabled = [a for a, c in workers.items() if c.get("enabled", True)]
        if agent not in enabled:
            raise AgentError(f"worker agent {agent!r} is not enabled; use --agent {' or '.join(enabled) or '(none)'}")
        self.check_limit(agent)
        if not role.strip():
            raise AgentError("--role is required (e.g. coder, reviewer, research)")
        role = gitops.slug(role)
        if not prompt.strip():
            raise AgentError("empty prompt")
        self.check_dispatch(ws, gid)
        sid = f"w-{secrets.token_hex(3)}"
        # The worker branches from the integration branch, so it must start from the meta-work's own changes too
        async with self.int_lock(ws, gid):
            self.checkpoint(ws, gid, caller["id"], f"changes before spawning {sid}")
            wt, branch = gitops.add_worker_worktree(p, gid, sid, role)
        sess = self.new_session(ws, gid, "worker", role, {"agent": agent, **workers[agent]}, wt, parent=caller["id"],
                                branch=branch, sid=sid)
        self.db.update("sessions", {"id": sid}, status="running")
        self._count_dispatch(ws, gid)
        self.spawn(self.worker_turn(sid, prompt), goal=(ws, gid))
        return (f"Spawned {sid} (role {role}: {sess['agent']}/{sess['model'] or 'default'}) on branch {branch}; "
                "it reports as an event when its turn ends.")

    async def send_worker(self, caller: dict, sid: str, prompt: str) -> str:
        self.require_meta_work(caller)
        w = self.own_worker(caller, sid)
        if w["status"] == "running":
            raise AgentError(f"{sid} is still running; wait for its report")
        if not Path(w["cwd"]).exists():
            raise AgentError(f"{sid} is retired (its worktree was removed); spawn a new worker, which can build on "
                             f"its branch {w['branch']}")
        if not prompt.strip():
            raise AgentError("empty prompt")
        self.check_limit(w["agent"])
        self.check_dispatch(caller["ws"], caller["goal"])
        self.db.update("sessions", {"id": sid}, status="running")
        self._count_dispatch(caller["ws"], caller["goal"])
        self.spawn(self.worker_turn(sid, prompt), goal=(caller["ws"], caller["goal"]))
        return f"Sent turn {w['turns'] + 1} to {sid}; it reports as an event when its turn ends."

    def check_limit(self, agent: str) -> None:
        if until := self.limit_until(agent):
            raise AgentError(f"{agent} is at its usage limit until {clock(until)}; use another --agent or wait")

    def _count_dispatch(self, ws: str, gid: str) -> None:
        m = self.goal_row(ws, gid)
        self.set_goal(ws, gid, worker_turns=m["worker_turns"] + 1, idle_streak=0)

    async def worker_turn(self, sid: str, prompt: str, woken: str = "") -> None:
        sess = self.db.session(sid)
        ws, gid = sess["ws"], sess["goal"]
        try:
            text, n = await self._worker_turn(sess, prompt)
            if woken:
                text += f"\n[System] The platform started this turn with the result of {woken}."
        except Exception as exc:
            # Any platform failure must still reach the meta-work, or the goal stalls on a phantom worker
            log.exception("worker turn failed for %s", sid)
            cur = self.db.session(sid)
            if cur["status"] == "running":
                self.db.update("sessions", {"id": sid}, status="failed", updated=time.time())
                self.publish("session_status", ws=ws, goal=gid, session=sid, status="failed")
            n = cur["turns"]
            text = (f"[Worker {sid} · role {cur['role']}] turn failed inside the platform: {exc!r}. Its worktree "
                    f"{cur['cwd']} is intact; retry with `loop send {sid}` or spawn a new worker.")
        self.db.push_inbox(ws, gid, "worker_done", {"text": text, "sid": sid, "turn": n})
        self.wake_for_jobs(sid)  # detached jobs that ended during the turn
        self.check_budget(ws, gid)
        self.kick(ws, gid)

    async def _worker_turn(self, sess: dict, prompt: str) -> tuple[str, int]:
        sid, ws, gid = sess["id"], sess["ws"], sess["goal"]
        p, rd = self.wsp(ws), self.gdir(ws, gid) / "runs" / sid
        wt, n = Path(sess["cwd"]), sess["turns"] + 1
        digest.write_text(rd / f"t{n}.prompt.md", prompt)
        start = gitops.head(wt)
        # The worker gets the meta-work's prompt as is: no platform header, like a prompt typed by the human
        res, n = await self.run_turn(sess, prompt, start_commit=start)
        try:
            gitops.snapshot(wt, "WIP: uncommitted changes at end of turn")
        except RuntimeError as exc:
            log.warning("snapshot failed for %s: %s", sid, exc)
        # Outputs the worker was asked to keep outlive the worktree, which is removed when the goal is accepted
        if (wt / ".artifacts").is_dir():
            shutil.copytree(wt / ".artifacts", rd / "artifacts", dirs_exist_ok=True)
        digest.write_text(rd / f"t{n}.out.md", res.final or res.error or "")
        self.db.x("UPDATE turns SET end_commit=? WHERE session=? AND n=?", gitops.head(wt), sid, n)
        text = digest.worker_digest(self.db.session(sid), n, res)
        # The guard can be bypassed (e.g. a script writing goal.md): revert before the evidence commit seals it
        reverted = workspace.enforce_protected(p)
        if res.leftover:
            text += "\n" + leftover_note(res.leftover, "this worker's turn")
        if res.memory:
            text += f"\n[System] Memory in this worker's turn: {res.memory}"
        if res.limited_until:
            # The CLI refused the turn: it does not count against the worker budget
            self.vendor_limits[sess["agent"]] = res.limited_until
            self.set_goal(ws, gid, worker_turns=max(0, self.goal_row(ws, gid)["worker_turns"] - 1))
            text += (f"\n[System] {sess['agent']} is at its usage limit until {clock(res.limited_until)}; this turn "
                     f"was not counted, and `--agent {sess['agent']}` spawns and sends are refused until then.")
        if reverted:
            text += "\n[System] Reverted this worker's edits to human-owned files: " + ", ".join(reverted)
            self.publish("alert", ws=ws, goal=gid, text=f"Reverted {sid}'s edits to human-owned files (backup in "
                                                            ".loop/reverted/): " + ", ".join(reverted))
        m = self.goal_row(ws, gid)
        self.set_goal(ws, gid, tokens=m["tokens"] + res.tokens)
        workspace.commit(p, "control-plane", f"control-plane: {sid} turn {n} evidence", protected=False)
        return text, n

    async def stop_worker(self, caller: dict | None, sid: str) -> str:
        if caller and sid.startswith("j-"):
            stopped = self.stop_jobs("stopped by the meta-work", id=sid, ws=caller["ws"], goal=caller["goal"])
            return f"Stopped job {sid}." if stopped else f"{sid} is not a running job of this goal (see `loop ls`)"
        w = self.own_worker(caller, sid) if caller else self.db.session(sid)
        jobs = self.stop_jobs(f"stopped with {sid}", session=sid) if w else []
        also = f" Stopped its jobs {', '.join(jobs)}." if jobs else ""
        if not w or not self.runner.running(sid):
            return f"{sid} is not running.{also}"
        await self.runner.kill(sid, stopped=True)
        return f"Stopped {sid}; its partial work was snapshotted and a report will follow.{also}"

    def merge_worker(self, caller: dict, sid: str) -> str:
        self.require_meta_work(caller)
        w = self.own_worker(caller, sid)
        if w["status"] == "running":
            raise AgentError(f"{sid} is still running")
        # Merges, checkpoints and verify runs would interleave in the single integration worktree
        with self.int_locks.setdefault((w["ws"], w["goal"]), threading.Lock()):
            self.checkpoint(w["ws"], w["goal"], caller["id"], f"changes before merging {sid}")
            res = gitops.merge(self.wsp(w["ws"]), w["goal"], w["branch"])
        if not res["ok"]:
            return (f"Merge of {w['branch']} FAILED with conflicts in: {', '.join(res['conflicts']) or '?'}. "
                    f"The merge was aborted; {gitops.int_branch(w['goal'])} is unchanged.")
        self.db.update("sessions", {"id": sid}, merged=1)
        self.publish("session_status", ws=w["ws"], goal=w["goal"], session=sid, status=w["status"])
        return f"Merged {w['branch']} into {gitops.int_branch(w['goal'])}." + (f"\n{res['stat']}" if res["stat"] else
                                                                  " (nothing new)")

    def retire_worker(self, caller: dict, sid: str) -> str:
        """Free a finished worker's disk: remove its worktree, keep its branch, row, log and artifacts. A worker
        counts as retired while its worktree is missing (worker_status), however it was removed."""
        self.require_meta_work(caller)
        w = self.own_worker(caller, sid)
        if w["status"] == "running" or self.runner.running(sid):
            raise AgentError(f"{sid} is still running")
        if not Path(w["cwd"]).exists():
            return f"{sid} is already retired."
        jobs = self.db.q("SELECT id FROM jobs WHERE session=? AND status='running'", sid)
        if jobs:
            raise AgentError(f"{sid} still has running jobs ({', '.join(j['id'] for j in jobs)}); wait or stop them")
        try:
            gitops.retire_worktree(self.wsp(w["ws"]), Path(w["cwd"]))
        except RuntimeError as exc:
            raise AgentError(f"could not remove {sid}'s worktree: {exc}") from None
        self.publish("session_status", ws=w["ws"], goal=w["goal"], session=sid, status=w["status"])
        return (f"Retired {sid}: removed its worktree; its branch {w['branch']}, log and artifacts stay. "
                "It can no longer be sent.")

    @staticmethod
    def worker_status(w: dict) -> str:
        return "retired" if w["status"] != "running" and not Path(w["cwd"]).exists() else w["status"]

    async def done(self, caller: dict, status: str, report: str) -> str:
        self.require_meta_work(caller)
        if status not in ("success", "partial", "blocked"):
            raise AgentError("status must be success, partial or blocked")
        if not report.strip():
            raise AgentError("a report is required (map each goal criterion to evidence and a verdict)")
        ws, gid = caller["ws"], caller["goal"]
        p, results = self.wsp(ws), []
        wt = gitops.int_worktree(p, gid)
        async with self.int_lock(ws, gid):
            # The report and the PR cover committed work, so that is what verify must judge
            self.checkpoint(ws, gid, caller["id"], "changes before loop done")
            if status == "success":
                before = gitops.untracked(wt)
                for cmd in digest.verify_commands((self.gdir(ws, gid) / "goal.md").read_text()):
                    proc = await asyncio.create_subprocess_shell(
                        cmd, cwd=wt, env=child_env({}), stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                    try:
                        out, _ = await asyncio.wait_for(proc.communicate(), VERIFY_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        os.killpg(proc.pid, signal.SIGKILL)  # the whole tree, not just /bin/sh
                        await proc.wait()
                        out = f"timeout after {VERIFY_TIMEOUT_S // 60} min".encode()
                    results.append({"cmd": cmd, "code": proc.returncode, "out": out.decode(errors="replace")[-1500:]})
                # Verify output must not reach a later checkpoint, and edits to tracked files would mean the
                # accepted state differs from the committed one: both are undone, and the edits reject the close
                changed, _ = gitops.restore_after_run(wt, before)
                failed = [r for r in results if r["code"] != 0]
                if failed or changed:
                    lines = [f"$ {r['cmd']} (exit {r['code']})\n{r['out']}" for r in failed]
                    if changed:
                        lines.append("The verify commands changed tracked files, which were reset to the committed "
                                     "state:\n" + changed[:3000])
                    return ("REJECTED on the integration branch:\n" + "\n".join(lines) +
                            "\nThe goal stays open; verify runs only for a `success` close.")
        await self.finish(ws, gid, status, report, results, by=f"meta-work {caller['id']}")
        return f"Goal closed as {status}; the human will review report.md."

    # ---------- read views (agents + UI) ----------
    def ls_text(self, caller: dict) -> str:
        rows = self.workers(caller["ws"], caller["goal"])
        out = ["id | role | agent/model | status | turns | context | tokens | merged | branch"] if rows else \
            ["No workers yet."]
        for w in rows:
            out.append(f"{w['id']} | {w['role']} | {w['agent']}/{w['model'] or '-'} | {self.worker_status(w)} | "
                       f"{w['turns']} | ~{digest.fmt_tokens(w['context'])} | {digest.fmt_tokens(w['tokens'])} | "
                       f"{'yes' if w['merged'] else 'no'} | {w['branch']}")
        jobs = self.db.q("SELECT * FROM jobs WHERE ws=? AND goal=? AND status='running' ORDER BY started",
                         caller["ws"], caller["goal"])
        if jobs:
            out += ["", "Running jobs (`loop run --detach`): id | session | running for | command"]
            out += [f"{j['id']} | {j['session']} | {(time.time() - j['started']) / 3600:.1f} h | `{j['cmd'][:120]}`"
                    for j in jobs]
        return "\n".join(out)

    def show_text(self, caller: dict, sid: str, turn: int | None, full: bool, with_log: bool = False) -> str:
        w = self.own_worker(caller, sid)
        rd = self.gdir(w["ws"], w["goal"]) / "runs" / sid
        turns = self.db.q("SELECT * FROM turns WHERE session=? ORDER BY n", sid)
        if with_log and not turn and turns:
            turn = turns[-1]["n"]
        if not turn:
            lines = [f"{sid}: role {w['role']}, {w['agent']}/{w['model']}, status {self.worker_status(w)}, "
                     f"worktree {w['cwd']}, branch {w['branch']}, run dir {rd}"]
            for t in turns:
                first = (t["final"] or "").strip().splitlines()[:1]
                lines.append(f"- turn {t['n']}: {t['status']} · {digest.fmt_tokens(t['tokens'])} tokens · "
                             f"{first[0][:160] if first else ''}")
            return "\n".join(lines) + "\nUse --turn N (and --full) for details."
        t = next((x for x in turns if x["n"] == turn), None)
        if not t:
            raise AgentError(f"{sid} has no turn {turn}")
        files = {k: rd / f"t{turn}.{k}" for k in ("prompt.md", "out.md")}
        out = [f"{sid} turn {turn}: {t['status']} · commits {(t['start_commit'] or '')[:8]}..{(t['end_commit'] or '')[:8]}",
               *(f"{k}: {v}" for k, v in files.items() if v.exists())]
        body = files["out.md"].read_text() if files["out.md"].exists() else ""
        if full:
            out += ["", "## Output", body]
        else:
            out += ["", digest.trim_words(body, 400)[0]]
        if with_log:
            out += ["", "## Log", self.turn_log(w["ws"], sid, turn)]
        return "\n".join(out)

    def turn_log(self, ws: str, sid: str, n: int, limit: int = 20_000) -> str:
        """What a worker actually ran in one turn: each tool call with the end of its output.

        Pulled by the meta-work only when it needs to check a claim, so digests stay short."""
        path, lines, inside = self.logs(ws, sid)[1], [], False
        with path.open() as fh:
            for raw in fh:
                e = json.loads(raw)
                if e["type"] == "turn":
                    inside = e["n"] == n
                elif inside and e["type"] == "turn_end":
                    break
                elif inside and e["type"] == "tool":
                    lines.append(f"$ {e['name']} {e.get('detail') or ''}".rstrip())
                elif inside and e["type"] == "tool_result":
                    body = (e.get("tail") or e.get("text") or "").strip().replace("\n", "\n  ")
                    lines.append(("  [error] " if e.get("error") else "  ") + body)
                elif inside and e["type"] == "error":
                    lines.append(f"! {e['text']}")
        text = "\n".join(lines) or "(no tool calls)"
        return text if len(text) <= limit else "[… earlier calls cut]\n" + text[-limit:]

    def status_text(self, caller: dict) -> str:
        ws, gid = caller["ws"], caller["goal"]
        m, gdir = self.goal_row(ws, gid), self.gdir(ws, gid)
        done, total = digest.checklist((gdir / "plan.md").read_text())
        recent = self.db.q("SELECT kind, payload, ts FROM inbox WHERE ws=? AND goal=? ORDER BY id DESC LIMIT 8",
                           ws, gid)
        lines = [f"Goal {gid}: {m['status']}" + (f" ({m['outcome']})" if m["outcome"] else ""),
                 f"Meta-work: {m['meta_work'] or '-'} · plan checklist {done}/{total}",
                 digest.budget_line(self.budget(ws, gid)), "Workers:", self.ls_text(caller), "Recent events:"]
        for r in recent:
            first = json.loads(r["payload"]).get("text", "").splitlines()[:1]
            lines.append(f"- {time.strftime('%H:%M', time.localtime(r['ts']))} {r['kind']}: "
                         f"{first[0][:160] if first else ''}")
        return "\n".join(lines)

    def infra_text(self) -> str:
        lines = [self.infra.summary(), self.ram_text()]
        for s in self.infra.latest().get("sessions", []):
            lines.append(f"- {s['id']} ({s['kind']} {s['role']}): {s['rss_gb']:.2f} GB in use")
        if quota := self.quota_text():
            lines.append(quota)
        return "\n".join(lines)

    def session_events(self, sid: str, limit: int = 400, types: set[str] | None = None) -> list[dict]:
        s = self.db.session(sid)
        if not s:
            raise AgentError("unknown session")
        path = self.logs(s["ws"], sid)[1]
        if not path.exists():
            return []
        with path.open() as fh:
            if not types:
                return [json.loads(x) for x in deque(fh, maxlen=limit)]
            # Cheap substring pre-filter before parsing: logs can hold large tool results
            keys = tuple(f'"type": "{t}"' for t in types)
            return [json.loads(x) for x in deque((x for x in fh if x.startswith(keys, 1)), maxlen=limit)]

    # ---------- meta-chats ----------
    def create_meta_chat(self, ws: str, gid: str, overrides: dict | None = None, text: str = "") -> dict:
        """Start a meta-chat with its first message, so no chat is stored before the human says something."""
        self.goal_row(ws, gid)
        if not text.strip():
            raise AgentError("empty message")
        # Chosen per chat by the human; unset fields fall back to the goal's meta-work settings
        mc = goal_config(self.wsp(ws), gid)["goal"]
        conf = {k: v for k, v in (overrides or {}).items() if k in ("agent", "model", "effort") and v}
        if conf.get("agent", mc["meta_work"]["agent"]) != mc["meta_work"]["agent"]:
            conf.setdefault("model", None)
            conf.setdefault("effort", None)
        conf = {"autocompact": None, **{k: mc["meta_work"].get(k) for k in ("agent", "model", "effort")}, **conf}
        reason = agents.check_agent_conf(conf["agent"], conf, agents.load_models())
        if reason:
            raise AgentError(reason)
        s = self.new_session(ws, gid, "meta-chat", "meta-chat", conf, self.wsp(ws) / "meta")
        self.meta_chat_send(s["id"], text)
        return s

    def meta_chat_send(self, sid: str, text: str) -> None:
        s = self.db.session(sid)
        if not s or s["kind"] != "meta-chat":
            raise AgentError("unknown meta-chat session")
        if s["status"] == "running" or self.runner.running(sid):
            raise AgentError("the meta-chat is still answering")
        if not text.strip():
            raise AgentError("empty message")
        self.db.update("sessions", {"id": sid}, status="running")  # claimed before the CLI starts
        self.db.x("INSERT INTO messages(session, ts, role, text) VALUES(?,?,?,?)", sid, time.time(), "user", text)
        self.publish("meta_chat_message", session=sid, ws=s["ws"], goal=s["goal"])
        task = self.chat_tasks[sid] = self.spawn(self._meta_chat_turn(sid, text), goal=(s["ws"], s["goal"]))
        task.add_done_callback(lambda t: self.chat_tasks.pop(sid, None) if self.chat_tasks.get(sid) is t else None)

    async def delete_meta_chat(self, sid: str) -> None:
        """Remove a meta-chat with its messages, turns and logs; a turn still answering is killed first."""
        s = self.db.session(sid)
        if not s or s["kind"] != "meta-chat":
            raise AgentError("unknown meta-chat session")
        await self.runner.kill(sid, stopped=True)
        # The killed turn still records its answer; wait for it so no row outlives the delete
        if task := self.chat_tasks.get(sid):
            await asyncio.wait([task], timeout=60)
            if not task.done():
                raise AgentError("the meta-chat is still finishing its turn; try the delete again shortly")
        for table, col in (("messages", "session"), ("turns", "session"), ("sessions", "id")):
            self.db.x(f"DELETE FROM {table} WHERE {col}=?", sid)
        for path in self.logs(s["ws"], sid):
            path.unlink(missing_ok=True)
        self.publish("session_deleted", ws=s["ws"], goal=s["goal"], session=sid)

    async def _meta_chat_turn(self, sid: str, text: str) -> None:
        s = self.db.session(sid)
        p = self.wsp(s["ws"])
        prompt = text  # the mode text goes in as system text every turn (build_spec)
        changed = self.watch_project(s["ws"], s["goal"])
        try:
            res, n = await self.run_turn(s, prompt)
            if hit := changed():
                self.publish("alert", ws=s["ws"], goal=s["goal"],
                             text=f"Meta-chat {sid} turn {n} changed {', '.join(hit)}; please check it.")
            workspace.enforce_protected(p)
            workspace.commit(p, f"meta-chat({sid})", f"meta-chat({sid}): turn {n}", protected=False)
            answer = res.final or f"(error: {res.error})"
        except Exception as exc:  # release the session and show the failure in the chat
            log.exception("meta-chat turn failed for %s", sid)
            if self.db.session(sid)["status"] == "running":
                self.db.update("sessions", {"id": sid}, status="failed")
            answer = f"(platform error: {exc!r})"
        self.db.x("INSERT INTO messages(session, ts, role, text) VALUES(?,?,?,?)", sid, time.time(),
                  "assistant", answer)
        self.publish("meta_chat_message", session=sid, ws=s["ws"], goal=s["goal"])
