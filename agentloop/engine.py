"""Mission engine: event-driven meta-work turns, workers, meta-chats, notes, budgets, review."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import yaml

from . import agents, digest, gitops, workspace
from .adapters import RunSpec
from .adapters.base import to_tokens
from .config import PYTHON, home, mission_config, state_dir, workspace_config
from .db import DB
from .infra import Infra, tree_rss
from .runner import Runner, child_env, unit_prefix

log = logging.getLogger("agentloop")
VERIFY_TIMEOUT_S = 900
PREFIX = {"meta-work": "mw", "worker": "w", "meta-chat": "mc"}


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


class Engine:
    def __init__(self, api_url: str):
        self.api_url = api_url
        self.db = DB(state_dir() / "agentloop.db")
        self.bus = Bus()
        self.runner = Runner()
        self.infra = Infra(str(home()))
        self.meta_active: set[tuple[str, str]] = set()
        self.merge_locks: dict[tuple[str, str], threading.Lock] = {}
        self.tasks: set[asyncio.Task] = set()
        self.agent_ops: dict[str, dict] = {}
        self.bin_dir = state_dir() / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        shim = self.bin_dir / "loop"
        shim.write_text(f'#!/bin/sh\nexec "{PYTHON}" -m agentloop.cli "$@"\n')
        shim.chmod(0o755)

    # ---------- plumbing ----------
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())
            self.bus.publish({"type": "error", "text": repr(task.exception())})

    def publish(self, kind: str, **data) -> None:
        self.bus.publish({"type": kind, **data})

    @staticmethod
    def wsp(ws: str) -> Path:
        return workspace.ws_path(ws)

    def mdir(self, ws: str, mid: str) -> Path:
        return workspace.mission_dir(self.wsp(ws), mid)

    def logs(self, ws: str, sid: str) -> tuple[Path, Path]:
        base = self.wsp(ws) / ".loop" / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{sid}.raw.jsonl", base / f"{sid}.events.jsonl"

    def mission_row(self, ws: str, mid: str) -> dict:
        if not (self.mdir(ws, mid) / "mission.yaml").exists():
            raise AgentError(f"mission {ws}/{mid} not found")
        row = self.db.mission(ws, mid)
        if not row:
            self.db.x("INSERT OR IGNORE INTO missions(ws, id, status) VALUES(?,?,?)", ws, mid, "draft")
            row = self.db.mission(ws, mid)
        return row

    def set_mission(self, ws: str, mid: str, **fields) -> None:
        self.db.update("missions", {"ws": ws, "id": mid}, **fields)
        if "status" in fields:
            self.publish("mission_status", ws=ws, mission=mid, status=fields["status"])

    def workers(self, ws: str, mid: str, running_only: bool = False) -> list[dict]:
        rows = self.db.q("SELECT * FROM sessions WHERE ws=? AND mission=? AND kind='worker' ORDER BY created",
                         ws, mid)
        return [r for r in rows if r["status"] == "running"] if running_only else rows

    def new_session(self, ws: str, mid: str, kind: str, role: str, label: str, conf: dict, cwd: Path,
                    parent: str | None = None, branch: str | None = None, gpu: bool = False,
                    sid: str | None = None) -> dict:
        sid = sid or f"{PREFIX[kind]}-{secrets.token_hex(3)}"
        now = time.time()
        self.db.x("INSERT INTO sessions(id, ws, mission, kind, parent, role, label, agent, model, effort, "
                  "autocompact, cwd, branch, status, token, gpu, created, updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  sid, ws, mid, kind, parent, role, label, conf["agent"], conf.get("model"), conf.get("effort"),
                  None if conf.get("autocompact") is None else str(conf.get("autocompact")), str(cwd), branch,
                  "idle", secrets.token_hex(16) if kind != "worker" else None, int(gpu), now, now)
        self.publish("session_status", ws=ws, mission=mid, session=sid, status="idle")
        return self.db.session(sid)

    # ---------- lifecycle ----------
    async def startup(self) -> None:
        # Worker scopes left over from an unclean shutdown would keep writing into worktrees
        subprocess.run(["systemctl", "--user", "stop", f"{unit_prefix()}-*.scope"], capture_output=True)
        # Events handed to a meta turn that never finished go back to the queue
        self.db.x("UPDATE inbox SET consumed=0 WHERE consumed=2")
        # A turn cut off by a restart still counts, so the next turn gets its own evidence files
        for s in self.db.q("SELECT id, turns FROM sessions WHERE status='running'"):
            cut = self.db.q("SELECT id FROM turns WHERE session=? AND status='running'", s["id"])
            for t in cut:
                self.db.update("turns", {"id": t["id"]}, status="interrupted", ended=time.time())
            self.db.update("sessions", {"id": s["id"]}, status="interrupted", turns=s["turns"] + len(cut))
        for m in self.db.q("SELECT * FROM missions WHERE status='running'"):
            names = [s["id"] for s in self.db.q("SELECT id FROM sessions WHERE ws=? AND mission=? AND "
                                                "status='interrupted' AND kind='worker'", m["ws"], m["id"])]
            # Count active time up to the last recorded activity before the restart
            last = self.db.one("SELECT MAX(updated) t FROM sessions WHERE ws=? AND mission=?", m["ws"], m["id"])["t"]
            gained = max(0.0, (last or 0) - (m["started"] or time.time()))
            self.db.update("missions", {"ws": m["ws"], "id": m["id"]}, started=time.time(),
                           active_s=(m["active_s"] or 0) + gained)
            self.db.push_inbox(m["ws"], m["id"], "system", {"text": (
                "[System] The platform restarted. " + (f"Interrupted workers: {', '.join(names)} "
                "(continue them with `loop send`)." if names else "No workers were interrupted."))})
            self.kick(m["ws"], m["id"])
        self.spawn(self.infra_loop())
        self.spawn(agents.refresh_models())  # model lists follow the installed CLI versions

    async def infra_loop(self) -> None:
        while True:
            # A failed sample must not freeze the RAM admission gate on stale numbers
            try:
                snap = await asyncio.to_thread(self.infra.sample)
                snap["sessions"] = []
                for key, (proc, _) in list(self.runner.procs.items()):
                    s = self.db.session(key) or {"kind": "smoke", "ws": "", "mission": "", "role": ""}
                    snap["sessions"].append({"id": key, "kind": s["kind"], "ws": s["ws"], "mission": s["mission"],
                                             "role": s["role"], "rss_gb": round(tree_rss(proc.pid) / 2**30, 2)})
                self.publish("infra", data=snap)
            except Exception:
                log.exception("infra sample failed")
            await asyncio.sleep(3)

    async def shutdown(self) -> None:
        for key in list(self.runner.procs):
            await self.runner.kill(key)

    # ---------- budget ----------
    def budget(self, ws: str, mid: str) -> dict:
        cfg = mission_config(self.wsp(ws), mid)["mission"]["budget"]
        m = self.mission_row(ws, mid)
        running = m["status"] == "running" and m["started"]
        active = (m["active_s"] or 0) + (time.time() - m["started"] if running else 0)
        return {**cfg, "running_workers": len(self.workers(ws, mid, running_only=True)),
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

    def check_budget(self, ws: str, mid: str) -> None:
        m = self.mission_row(ws, mid)
        if m["status"] != "running" or m["final_turn"]:
            return
        b = self.budget(ws, mid)
        hit = self.exhausted(b)
        if not hit:
            return
        reason, hard = hit
        if hard:
            for w in self.workers(ws, mid, running_only=True):
                self.spawn(self.runner.kill(w["id"], stopped=True))
        elif b["running_workers"]:
            return  # soft limit: let running workers report first
        self.set_mission(ws, mid, final_turn=1)
        self.db.push_inbox(ws, mid, "budget", {"text": (
            f"[Budget exhausted: {reason}] No more workers can be started. This is your final turn: "
            "call `loop done --status partial` (or `success` if the goal is met) with a report.")})
        self.kick(ws, mid)

    def check_dispatch(self, ws: str, mid: str, gpu: bool) -> None:
        p = self.wsp(ws)
        cfg, m, b = workspace_config(p), self.mission_row(ws, mid), self.budget(ws, mid)
        if m["final_turn"] or self.exhausted(b):
            raise AgentError("budget exhausted: no more workers can be started; finalize with `loop done`")
        if b["running_workers"] >= (b["max_concurrent_workers"] or 1e9):
            raise AgentError(f"{b['running_workers']} workers already running (max {b['max_concurrent_workers']} "
                             "for this mission); end your turn and wait for a report")
        total = self.db.one("SELECT COUNT(*) c FROM sessions WHERE ws=? AND kind='worker' AND status='running'",
                            ws)["c"]
        if total >= cfg["limits"]["max_workers_total"]:
            raise AgentError(f"workspace worker limit reached ({total}); wait for a report")
        snap = self.infra.latest()
        if snap["ram_free_gb"] < cfg["infra"]["min_free_ram_gb"]:
            raise AgentError(f"only {snap['ram_free_gb']} GB RAM free (min {cfg['infra']['min_free_ram_gb']}); "
                             "wait for running workers or reduce load")
        if gpu:
            if not snap["gpus"]:
                raise AgentError("no GPU on this host")
            used = self.db.one("SELECT COUNT(*) c FROM sessions WHERE gpu=1 AND status='running'")["c"]
            if used >= cfg["infra"]["gpu_slots"]:
                raise AgentError("all GPU slots are busy; wait for the GPU worker to finish")

    # ---------- running one turn of any session ----------
    @staticmethod
    def language_rule(lang: str, meta: bool) -> str:
        if not lang or lang.lower() == "english":
            return ""
        scope = ("plan.md, journal.md, notes, reports, chat replies and the prompts you write for workers"
                 if meta else "your messages and your final report")
        return (f" LANGUAGE: the human reads {lang}. Write {scope} in {lang}; keep code, identifiers, commands "
                "and commit messages in the project's usual language.")

    def mode_text(self, sess: dict) -> str:
        ws, mid = sess["ws"], sess["mission"]
        p, mdir = self.wsp(ws), self.mdir(ws, mid)
        lang = self.language_rule(mission_config(p, mid)["mission"].get("language"), meta=True)
        if sess["kind"] == "meta-work":
            return (f"MODE: META-WORK for mission {mid} in workspace {ws}. Mission dir: {mdir}. "
                    f"Workspace root: {p}. Follow the META-WORK section of your AGENTS.md.{lang}")
        orch = self.mission_row(ws, mid)["meta_work"] or "not started"
        return (f"MODE: META-CHAT for mission {mid} in workspace {ws}. Mission dir: {mdir}. Workspace root: {p}. "
                f"Meta-work session: {orch}. Follow the META-CHAT section of your AGENTS.md; "
                f"you cannot dispatch workers.{lang}")

    def session_conf(self, sess: dict) -> dict:
        """Agent settings for the next turn: fixed by the human config for meta-work and workers,
        chosen at creation for a meta-chat."""
        keys = ("model", "effort", "autocompact")
        if sess["kind"] == "meta-chat":
            return {k: sess[k] for k in keys}
        mc = mission_config(self.wsp(sess["ws"]), sess["mission"])["mission"]
        conf = mc["meta_work"] if sess["kind"] == "meta-work" else mc["workers"].get(sess["agent"], {})
        return {k: None if conf.get(k) is None else str(conf[k]) for k in keys}

    def build_spec(self, sess: dict, prompt: str, n: int) -> RunSpec:
        ws, mid, sid = sess["ws"], sess["mission"], sess["id"]
        p, mdir = self.wsp(ws), self.mdir(ws, mid)
        cfg = mission_config(p, mid)
        worker = sess["kind"] == "worker"
        conf = self.session_conf(sess)
        if any(conf[k] != sess[k] for k in conf):  # the human changed the config since the last turn
            self.db.update("sessions", {"id": sid}, **conf)
        env = {"LOOP_ROLE": sess["kind"],
               "LOOP_WS": p, "LOOP_MISSION": mid, "LOOP_MISSION_DIR": mdir, "LOOP_SESSION": sid,
               "LOOP_GUARD_PATTERNS": json.dumps(cfg["guard"]["deny_patterns"] or [])}
        spec = RunSpec(agent=sess["agent"], kind="worker" if worker else "meta", cwd=Path(sess["cwd"]),
                       prompt=prompt, model=conf["model"], effort=conf["effort"], autocompact=conf["autocompact"],
                       resume_id=sess["vendor_id"], env=env)
        if worker:
            env.update({"LOOP_RUN_DIR": mdir / "runs" / sid})
            if not sess["gpu"]:
                env["CUDA_VISIBLE_DEVICES"] = ""
            if sess["agent"] == "codex":
                spec.codex_home = workspace.codex_home(p, "worker")
            else:
                spec.plugin_dir, spec.append_file = p / "worker" / "claude-plugin", p / "worker" / "AGENTS.md"
                spec.settings_file, spec.add_dirs = p / "worker" / "hooks.json", [mdir, p / "data"]
        else:
            env.update({"LOOP_API": self.api_url, "LOOP_TOKEN": sess["token"], "PATH_PREPEND": self.bin_dir})
            spec.system_text = self.mode_text(sess)
            if sess["agent"] == "codex":
                spec.codex_home = workspace.codex_home(p, "meta")
                spec.add_dirs = [mdir, p / "worker"]
            else:
                spec.settings_file, spec.add_dirs = p / "meta" / "hooks.json", [p]
        if sess["agent"] == "codex":
            spec.last_message_file = self.logs(ws, sid)[0].with_name(f"{sid}.t{n}.last.txt")
        return spec

    async def run_turn(self, sess: dict, prompt: str, start_commit: str | None = None):
        sid, ws, mid = sess["id"], sess["ws"], sess["mission"]
        n = sess["turns"] + 1
        spec = self.build_spec(sess, prompt, n)
        raw, events = self.logs(ws, sid)
        cfg = mission_config(self.wsp(ws), mid)
        self.db.update("sessions", {"id": sid}, status="running", updated=time.time())
        turn_id = self.db.x("INSERT INTO turns(session, n, started, status, start_commit) VALUES(?,?,?,?,?)",
                            sid, n, time.time(), "running", start_commit)
        self.publish("session_status", ws=ws, mission=mid, session=sid, status="running", turn=n)
        self._log_event(ws, mid, sid, n, events, {"type": "turn", "n": n, "prompt": prompt})

        async def on_event(evt: dict) -> None:
            if evt["type"] == "init" and evt.get("vendor_id") and not sess["vendor_id"]:
                self.db.update("sessions", {"id": sid}, vendor_id=evt["vendor_id"])
            self.bus.publish({"type": "session_event", "ws": ws, "mission": mid, "session": sid, "turn": n,
                              "event": evt})

        mem = cfg["infra"]["worker_memory_max_gb"] if sess["kind"] == "worker" else None
        res = await self.runner.run(sid, spec, raw_log=raw, event_log=events, on_event=on_event,
                                    timeout_s=cfg["turn_timeout_min"] * 60, memory_max_gb=mem)
        status = "stopped" if res.stopped else ("failed" if res.error else "idle")
        cur = self.db.session(sid)
        self.db.update("sessions", {"id": sid}, vendor_id=res.vendor_id or cur["vendor_id"], turns=n,
                       tokens=cur["tokens"] + res.tokens, cost=cur["cost"] + res.cost,
                       context=res.context or cur["context"], status=status, updated=time.time())
        self.db.update("turns", {"id": turn_id}, ended=time.time(), status=status, exit_code=res.exit_code,
                       tokens=res.tokens, cost=res.cost, final=(res.final or res.error)[:20000])
        self._log_event(ws, mid, sid, n, events, {"type": "turn_end", "n": n, "status": status, "error": res.error,
                                                   "tokens": res.tokens, "cost": res.cost})
        self.publish("session_status", ws=ws, mission=mid, session=sid, status=status, turn=n)
        return res, n

    def _log_event(self, ws: str, mid: str, sid: str, n: int, path: Path, evt: dict) -> None:
        evt["ts"] = time.time()
        with path.open("a") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
        self.bus.publish({"type": "session_event", "ws": ws, "mission": mid, "session": sid, "turn": n, "event": evt})

    # ---------- missions ----------
    def kickoff_text(self, ws: str, mid: str) -> str:
        p, mdir = self.wsp(ws), self.mdir(ws, mid)
        mc = mission_config(p, mid)["mission"]
        agents = "\n".join(f"  - `--agent {a}`: {c.get('model') or 'default model'} / effort {c.get('effort') or 'default'}"
                           for a, c in mc["workers"].items() if c.get("enabled", True))
        return (f"[Kickoff] Mission \"{mc['title']}\" ({mid}) in workspace {ws}.\n"
                f"- Mission dir: {mdir}. Read spec.md and goal.md first (read-only for you). "
                "You own plan.md and journal.md there.\n"
                f"- Project repo (read-only for you): {p / 'repo'}. Integration branch {gitops.int_branch(mid)} is checked out "
                f"at {p / 'wt' / ('_int-' + mid)}.\n"
                f"- Worker agents (the human fixed model and effort; you pick the CLI and name the role):\n{agents}\n"
                f"- Workspace map: {p / 'PLATFORM.md'}. Cross-mission memory: {p / 'meta' / 'memory'}.\n"
                "Write plan.md, add a journal entry, dispatch workers with `loop spawn`, then end your turn.")

    async def start_mission(self, ws: str, mid: str) -> None:
        p, m = self.wsp(ws), self.mission_row(ws, mid)
        if m["status"] in ("running", "awaiting_review", "done"):
            raise AgentError(f"mission is {m['status']}")
        limit = workspace_config(p)["limits"]["max_running_missions"]
        running = self.db.one("SELECT COUNT(*) c FROM missions WHERE ws=? AND status='running'", ws)["c"]
        if running >= limit:
            raise AgentError(f"{running} missions already running in this workspace (max_running_missions={limit})")
        mc = mission_config(p, mid)["mission"]
        gitops.ensure_integration(p, mid, mc.get("base_branch"))
        if not m["meta_work"]:
            orch = self.new_session(ws, mid, "meta-work", "meta-work", "meta-work", mc["meta_work"], p / "meta")
            self.set_mission(ws, mid, meta_work=orch["id"])
            self.db.push_inbox(ws, mid, "kickoff", {"text": self.kickoff_text(ws, mid)})
        else:
            self.db.push_inbox(ws, mid, "system", {"text": "[System] The human resumed the mission. "
                                                           "Check `loop ls` and continue."})
        self.set_mission(ws, mid, status="running", started=time.time(), final_turn=0, idle_streak=0,
                         fail_streak=0, first_started=m["first_started"] or time.time())
        self.kick(ws, mid)

    async def stop_mission(self, ws: str, mid: str, status: str = "stopped", meta_work: bool = True) -> None:
        """Leave the running state; kill workers (and the meta-work unless it is the caller)."""
        m = self.mission_row(ws, mid)
        if m["status"] == "running":
            self.set_mission(ws, mid, status=status, active_s=self.budget(ws, mid)["hours"] * 3600,
                             started=None)
        elif m["status"] != status:
            self.set_mission(ws, mid, status=status)
        kinds = ("worker", "meta-work") if meta_work else ("worker",)
        for s in self.db.q("SELECT id, kind FROM sessions WHERE ws=? AND mission=?", ws, mid):
            if s["kind"] in kinds:
                await self.runner.kill(s["id"], stopped=True)

    def kick(self, ws: str, mid: str) -> None:
        if (ws, mid) not in self.meta_active:
            self.spawn(self.meta_loop(ws, mid))

    async def meta_loop(self, ws: str, mid: str) -> None:
        key = (ws, mid)
        if key in self.meta_active:
            return
        self.meta_active.add(key)
        try:
            while True:
                m = self.mission_row(ws, mid)
                if m["status"] != "running":
                    return
                if not self.db.one("SELECT id FROM inbox WHERE ws=? AND mission=? AND consumed=0 AND deferred=0",
                                   ws, mid):
                    return
                items = self.db.q("SELECT * FROM inbox WHERE ws=? AND mission=? AND consumed=0 ORDER BY id",
                                  ws, mid)
                # Items stay in flight (2) until the turn completes; failures put them back (0)
                ids = ",".join(str(i["id"]) for i in items)
                self.db.x(f"UPDATE inbox SET consumed=2 WHERE id IN ({ids})")
                try:
                    await self.meta_turn(ws, mid, items)
                except Exception as exc:
                    self.db.x(f"UPDATE inbox SET consumed=0 WHERE id IN ({ids}) AND consumed=2")
                    log.exception("meta turn failed for %s/%s", ws, mid)
                    await self.stop_mission(ws, mid, status="paused")
                    self.publish("alert", ws=ws, mission=mid, text=f"Meta-work turn crashed, mission paused: {exc!r}")
                    return
                self.db.x(f"UPDATE inbox SET consumed=1 WHERE id IN ({ids}) AND consumed=2")
                await self.after_meta_turn(ws, mid, items)
        finally:
            self.meta_active.discard(key)

    async def meta_turn(self, ws: str, mid: str, items: list[dict]) -> None:
        p, m = self.wsp(ws), self.mission_row(ws, mid)
        mc = mission_config(p, mid)["mission"]
        orch = self.db.session(m["meta_work"])
        rotate = to_tokens(mc["meta_work"].get("rotate_at_tokens"))
        # A vendor session cannot move between CLIs, so a new meta-work agent means a fresh session
        if orch["agent"] != mc["meta_work"]["agent"] or (rotate and orch["context"] > rotate):
            # Fresh meta-work session; plan.md/journal.md carry the state across
            self.db.update("sessions", {"id": orch["id"]}, status="retired")
            orch = self.new_session(ws, mid, "meta-work", "meta-work", "meta-work", mc["meta_work"], p / "meta")
            self.set_mission(ws, mid, meta_work=orch["id"])
            items = [{"kind": "handoff", "payload": json.dumps({"text": (
                "[Handoff] You are a fresh meta-work session replacing a previous one (its context grew too "
                "large, or the human switched the meta-work agent). Regain state from plan.md, journal.md, notes.md and `loop ls`.\n\n"
                + self.kickoff_text(ws, mid))})}] + items
        workspace.commit_external(p)
        text = digest.envelope(items, digest.budget_line(self.budget(ws, mid)), self.infra.summary())
        res, n = await self.run_turn(orch, text)
        reverted = workspace.enforce_protected(p)
        workspace.commit(p, f"meta-work({orch['id']})", f"meta-work({orch['id']}): {mid} turn {n}")
        m = self.mission_row(ws, mid)
        fields = {"meta_turns": m["meta_turns"] + 1, "tokens": m["tokens"] + res.tokens,
                  "cost": m["cost"] + res.cost}
        if reverted:
            self.db.push_inbox(ws, mid, "system", {"text": "[System] Reverted your edits to human-owned files: "
                                                           + ", ".join(reverted)}, deferred=True)
            self.publish("alert", ws=ws, mission=mid, text="Reverted edits to human-owned files (backup in "
                                                            ".loop/reverted/): " + ", ".join(reverted))
        ids = ",".join(str(i["id"]) for i in items if "id" in i)
        if res.stopped and ids:  # a human-stopped turn: keep its events for the next trigger
            self.db.x(f"UPDATE inbox SET consumed=0, deferred=1 WHERE id IN ({ids})")
        if res.error and not res.stopped:
            fields["fail_streak"] = m["fail_streak"] + 1
            if ids:  # redeliver the same events on the next attempt
                self.db.x(f"UPDATE inbox SET consumed=0 WHERE id IN ({ids})")
            if fields["fail_streak"] >= 3:
                self.db.update("missions", {"ws": ws, "id": mid}, **fields)
                await self.stop_mission(ws, mid, status="paused")
                self.publish("alert", ws=ws, mission=mid, text=f"Meta-work failed 3 times: {res.error}")
                return
            await asyncio.sleep(min(60, 10 * fields["fail_streak"]))
        else:
            fields["fail_streak"] = 0
        self.db.update("missions", {"ws": ws, "id": mid}, **fields)

    async def after_meta_turn(self, ws: str, mid: str, items: list[dict]) -> None:
        m = self.mission_row(ws, mid)
        if m["status"] != "running":
            return
        if m["final_turn"] == 1 and any(i["kind"] == "budget" for i in items):
            reason = (self.exhausted(self.budget(ws, mid)) or ("limit",))[0]
            await self.finish(ws, mid, "partial", f"Budget exhausted ({reason}); the meta-work did not "
                                                  "close the mission on its final turn.", [], by="platform")
            return
        self.check_budget(ws, mid)
        pending = self.db.one("SELECT id FROM inbox WHERE ws=? AND mission=? AND consumed=0 AND deferred=0", ws, mid)
        if self.workers(ws, mid, running_only=True) or pending or self.mission_row(ws, mid)["final_turn"]:
            return
        streak = m["idle_streak"] + 1
        self.set_mission(ws, mid, idle_streak=streak)
        if streak <= 2:
            self.db.push_inbox(ws, mid, "system", {"text": (
                "[System] No workers are running and the mission is still open. Dispatch work, or call "
                "`loop done` if the goal is met, progress has stalled, or you need the human.")})
        else:
            await self.stop_mission(ws, mid, status="paused")
            self.publish("alert", ws=ws, mission=mid, text="Meta-work went idle; mission paused.")

    async def finish(self, ws: str, mid: str, status: str, report: str, verify: list[dict], by: str) -> None:
        mdir = self.mdir(ws, mid)
        await self.stop_mission(ws, mid, status="awaiting_review", meta_work=False)
        lines = [f"# Completion report\n", f"- Status: **{status}**", f"- Closed by: {by}",
                 f"- Time: {time.strftime('%Y-%m-%d %H:%M')}\n", report.strip() + "\n"]
        if verify:
            lines.append("## Verify commands\n")
            lines += [f"- `{v['cmd']}` → exit {v['code']}" for v in verify]
        (mdir / "report.md").write_text("\n".join(lines) + "\n")
        self.set_mission(ws, mid, outcome=status, ended=time.time())
        workspace.commit(self.wsp(ws), "control-plane", f"control-plane: {mid} closed ({status})")
        self.publish("alert", ws=ws, mission=mid, text=f"Mission closed as {status}; awaiting your review.")

    async def accept(self, ws: str, mid: str) -> list[str]:
        p, m = self.wsp(ws), self.mission_row(ws, mid)
        if m["status"] != "awaiting_review":
            raise AgentError("only a mission awaiting review can be accepted")
        tags = gitops.cleanup(p, mid, self.workers(ws, mid))
        if mission_config(p, mid)["mission"]["push"] == "branch" and gitops.git(p / "repo", "remote", check=False):
            gitops.git(p / "repo", "push", "origin", gitops.int_branch(mid), check=False)
        self.set_mission(ws, mid, status="done")
        workspace.commit(p, "human", f"human: accepted {mid}")
        return tags

    async def reopen(self, ws: str, mid: str, text: str, nid: int | None = None) -> None:
        """Back to running after review; the human's note (new, or an approved pending one) is delivered now."""
        m = self.mission_row(ws, mid)
        if m["status"] != "awaiting_review":
            raise AgentError("only a mission awaiting review can be reopened")
        self.set_mission(ws, mid, status="running", started=time.time(), final_turn=0, idle_streak=0, outcome=None)
        self.db.push_inbox(ws, mid, "system", {"text": "[System] The human reviewed your report and reopened "
                                                       "the mission; see the note."}, deferred=True)
        if nid:
            self.review_note(nid, True, "now", text)
        else:
            self.add_note(ws, mid, text or "Reopened after review.", mode="now")
        self.kick(ws, mid)

    # ---------- notes and human edits ----------
    def add_note(self, ws: str, mid: str, text: str, author: str = "human", mode: str = "queue",
                 status: str = "approved", source: str | None = None) -> int:
        text = text.strip()
        if not text:
            raise AgentError("empty note")
        nid = self.db.x("INSERT INTO notes(ws, mission, ts, text, author, status, mode, source_session) "
                        "VALUES(?,?,?,?,?,?,?,?)", ws, mid, time.time(), text, author, status, mode, source)
        if status == "approved":
            self.deliver_note(nid)
        self.publish("note", ws=ws, mission=mid, id=nid, status=status)
        return nid

    def deliver_note(self, nid: int) -> None:
        n = self.db.one("SELECT * FROM notes WHERE id=?", nid)
        path = self.mdir(n["ws"], n["mission"]) / "notes.md"
        with path.open("a") as fh:
            fh.write(f"\n## Note #{nid} · {time.strftime('%Y-%m-%d %H:%M')} · {n['author']}\n\n{n['text']}\n")
        workspace.commit(self.wsp(n["ws"]), "human", f"human: note #{nid} for {n['mission']}")
        self.db.push_inbox(n["ws"], n["mission"], "note", {"id": nid, "text": n["text"]},
                           deferred=n["mode"] != "now")
        if n["mode"] == "now":
            self.kick(n["ws"], n["mission"])

    def review_note(self, nid: int, approve: bool, mode: str = "queue", text: str | None = None) -> None:
        n = self.db.one("SELECT * FROM notes WHERE id=?", nid)
        if not n or n["status"] != "pending":
            raise AgentError("note is not pending")
        self.db.update("notes", {"id": nid}, status="approved" if approve else "rejected", mode=mode,
                       text=(text or n["text"]).strip())
        if approve:
            self.deliver_note(nid)
        self.publish("note", ws=n["ws"], mission=n["mission"], id=nid)

    def human_edit(self, ws: str, rel: str, text: str) -> None:
        if rel.endswith(".yaml"):
            data = yaml.safe_load(text) or {}
            errors = agents.config_errors((data.get("defaults") if rel == "loop.yaml" else data) or {})
            if errors:
                raise AgentError("; ".join(errors))
        change = workspace.human_write(self.wsp(ws), rel, text)
        parts = Path(rel).parts
        if change and len(parts) == 3 and parts[0] == "missions":
            mid, name = parts[1], parts[2]
            if self.mission_row(ws, mid)["status"] in ("running", "paused", "stopped"):
                self.db.push_inbox(ws, mid, "file_changed", {"name": name, "diff": change},
                                   deferred=name == "mission.yaml")
                self.kick(ws, mid)
        self.publish("file_changed", ws=ws, path=rel)

    # ---------- workers ----------
    def caller(self, token: str | None) -> dict:
        sess = self.db.one("SELECT * FROM sessions WHERE token=?", token) if token else None
        if not sess:
            raise AgentError("unknown session token (only meta-agent sessions may use `loop`)")
        return sess

    def own_worker(self, caller: dict, sid: str) -> dict:
        w = self.db.session(sid)
        if not w or w["kind"] != "worker" or w["mission"] != caller["mission"] or w["ws"] != caller["ws"]:
            raise AgentError(f"no worker {sid} in this mission (see `loop ls`)")
        return w

    def require_meta_work(self, caller: dict) -> dict:
        if caller["kind"] != "meta-work":
            raise AgentError("only the mission meta-work can do this; propose a note with `loop note` instead")
        m = self.mission_row(caller["ws"], caller["mission"])
        if m["status"] != "running":
            raise AgentError(f"mission is {m['status']}")
        return m

    async def spawn_worker(self, caller: dict, agent: str, role: str, label: str, prompt: str,
                           gpu: bool = False) -> str:
        self.require_meta_work(caller)
        ws, mid = caller["ws"], caller["mission"]
        p = self.wsp(ws)
        workers = mission_config(p, mid)["mission"]["workers"]
        enabled = [a for a, c in workers.items() if c.get("enabled", True)]
        if agent not in enabled:
            raise AgentError(f"worker agent {agent!r} is not enabled; use --agent {' or '.join(enabled) or '(none)'}")
        if not role.strip():
            raise AgentError("--role is required (e.g. coder, reviewer, research)")
        role = gitops.slug(role)
        if not prompt.strip():
            raise AgentError("empty prompt")
        self.check_dispatch(ws, mid, gpu)
        sid = f"w-{secrets.token_hex(3)}"
        wt, branch = gitops.add_worker_worktree(p, mid, sid, label or role)
        sess = self.new_session(ws, mid, "worker", role, label or role, {"agent": agent, **workers[agent]}, wt,
                                parent=caller["id"],
                                branch=branch, gpu=gpu, sid=sid)
        self.db.update("sessions", {"id": sid}, status="running")
        self._count_dispatch(ws, mid)
        self.spawn(self.worker_turn(sid, prompt))
        return (f"Spawned {sid} (role {role}: {sess['agent']}/{sess['model'] or 'default'}) on branch {branch}. "
                "End your turn; you will be resumed when it reports.")

    async def send_worker(self, caller: dict, sid: str, prompt: str) -> str:
        self.require_meta_work(caller)
        w = self.own_worker(caller, sid)
        if w["status"] == "running":
            raise AgentError(f"{sid} is still running; wait for its report")
        if not Path(w["cwd"]).exists():
            raise AgentError(f"{sid}'s worktree no longer exists; spawn a new worker")
        if not prompt.strip():
            raise AgentError("empty prompt")
        self.check_dispatch(caller["ws"], caller["mission"], bool(w["gpu"]))
        self.db.update("sessions", {"id": sid}, status="running")
        self._count_dispatch(caller["ws"], caller["mission"])
        self.spawn(self.worker_turn(sid, prompt))
        return f"Sent turn {w['turns'] + 1} to {sid}. End your turn; you will be resumed when it reports."

    def _count_dispatch(self, ws: str, mid: str) -> None:
        m = self.mission_row(ws, mid)
        self.set_mission(ws, mid, worker_turns=m["worker_turns"] + 1, idle_streak=0)

    async def worker_turn(self, sid: str, prompt: str) -> None:
        sess = self.db.session(sid)
        ws, mid = sess["ws"], sess["mission"]
        try:
            text, n = await self._worker_turn(sess, prompt)
        except Exception as exc:
            # Any platform failure must still reach the meta-work, or the mission stalls on a phantom worker
            log.exception("worker turn failed for %s", sid)
            cur = self.db.session(sid)
            if cur["status"] == "running":
                self.db.update("sessions", {"id": sid}, status="failed", updated=time.time())
                self.publish("session_status", ws=ws, mission=mid, session=sid, status="failed")
            n = cur["turns"]
            text = (f"[Worker {sid} · role {cur['role']}] turn failed inside the platform: {exc!r}. Its worktree "
                    f"{cur['cwd']} is intact; retry with `loop send {sid}` or spawn a new worker.")
        self.db.push_inbox(ws, mid, "worker_done", {"text": text, "sid": sid, "turn": n})
        self.check_budget(ws, mid)
        self.kick(ws, mid)

    async def _worker_turn(self, sess: dict, prompt: str) -> tuple[str, int]:
        sid, ws, mid = sess["id"], sess["ws"], sess["mission"]
        p, rd = self.wsp(ws), self.mdir(ws, mid) / "runs" / sid
        cfg = mission_config(p, mid)
        wt, n = Path(sess["cwd"]), sess["turns"] + 1
        (rd / "artifacts").mkdir(parents=True, exist_ok=True)
        digest.write_text(rd / f"t{n}.prompt.md", prompt)
        header = (f"[agent-loop] mission {mid} · role {sess['role']} · worktree {wt} (branch {sess['branch']}) · "
                  f"run dir {rd} · memory limit {cfg['infra']['worker_memory_max_gb']} GB · GPU "
                  f"{'granted' if sess['gpu'] else 'not granted'}"
                  + self.language_rule(cfg["mission"].get("language"), meta=False))
        start = gitops.head(wt)
        res, n = await self.run_turn(sess, f"{header}\n\n{prompt}", start_commit=start)
        try:
            gitops.snapshot(wt, f"agentloop: {sid} turn {n} snapshot")
        except RuntimeError as exc:
            log.warning("snapshot failed for %s: %s", sid, exc)
        end = gitops.head(wt)
        stat, patch = gitops.diff(wt, start, end)
        paths = {"out": digest.write_text(rd / f"t{n}.out.md", res.final or res.error or "")}
        paths["diff"] = digest.write_text(rd / f"t{n}.diff", patch) if patch else ""
        self.db.x("UPDATE turns SET end_commit=? WHERE session=? AND n=?", end, sid, n)
        text = digest.worker_digest(self.db.session(sid), n, res, stat, patch, paths, cfg["digest"])
        m = self.mission_row(ws, mid)
        self.set_mission(ws, mid, tokens=m["tokens"] + res.tokens, cost=m["cost"] + res.cost)
        workspace.commit(p, "control-plane", f"control-plane: {sid} turn {n} evidence")
        return text, n

    async def stop_worker(self, caller: dict | None, sid: str) -> str:
        w = self.own_worker(caller, sid) if caller else self.db.session(sid)
        if not w or not self.runner.running(sid):
            return f"{sid} is not running"
        await self.runner.kill(sid, stopped=True)
        return f"Stopped {sid}; its partial work was snapshotted and a report will follow."

    def merge_worker(self, caller: dict, sid: str) -> str:
        self.require_meta_work(caller)
        w = self.own_worker(caller, sid)
        if w["status"] == "running":
            raise AgentError(f"{sid} is still running")
        # Parallel `loop merge` calls would interleave in the single integration worktree
        with self.merge_locks.setdefault((w["ws"], w["mission"]), threading.Lock()):
            res = gitops.merge(self.wsp(w["ws"]), w["mission"], w["branch"])
        if not res["ok"]:
            return (f"Merge of {w['branch']} FAILED with conflicts in: {', '.join(res['conflicts']) or '?'}. "
                    f"The merge was aborted. Ask a worker to merge {gitops.int_branch(w['mission'])} into its branch and resolve.")
        self.db.update("sessions", {"id": sid}, merged=1)
        self.publish("session_status", ws=w["ws"], mission=w["mission"], session=sid, status=w["status"])
        return f"Merged {w['branch']} into {gitops.int_branch(w['mission'])}." + (f"\n{res['stat']}" if res["stat"] else
                                                                  " (nothing new)")

    async def done(self, caller: dict, status: str, report: str) -> str:
        self.require_meta_work(caller)
        if status not in ("success", "partial", "blocked"):
            raise AgentError("status must be success, partial or blocked")
        if not report.strip():
            raise AgentError("a report is required (map each goal criterion to evidence and a verdict)")
        ws, mid = caller["ws"], caller["mission"]
        p, results = self.wsp(ws), []
        if status == "success":
            wt = p / "wt" / f"_int-{mid}"
            for cmd in digest.verify_commands((self.mdir(ws, mid) / "goal.md").read_text()):
                proc = await asyncio.create_subprocess_shell(
                    cmd, cwd=wt, env=child_env({}), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), VERIFY_TIMEOUT_S)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)  # the whole tree, not just /bin/sh
                    await proc.wait()
                    out = f"timeout after {VERIFY_TIMEOUT_S // 60} min".encode()
                results.append({"cmd": cmd, "code": proc.returncode, "out": out.decode(errors="replace")[-1500:]})
            failed = [r for r in results if r["code"] != 0]
            if failed:
                return "REJECTED: verify commands failed on the integration branch:\n" + "\n".join(
                    f"$ {r['cmd']} (exit {r['code']})\n{r['out']}" for r in failed) + \
                    "\nFix (merge the right work, or delegate fixes), or close with --status partial."
        await self.finish(ws, mid, status, report, results, by=f"meta-work {caller['id']}")
        return f"Mission closed as {status}; the human will review report.md. End your turn now."

    # ---------- read views (agents + UI) ----------
    def ls_text(self, caller: dict) -> str:
        rows = self.workers(caller["ws"], caller["mission"])
        if not rows:
            return "No workers yet."
        out = ["id | role | label | agent/model | status | turns | context | tokens | merged | branch"]
        for w in rows:
            out.append(f"{w['id']} | {w['role']} | {w['label']} | {w['agent']}/{w['model'] or '-'} | {w['status']} | "
                       f"{w['turns']} | ~{digest.fmt_tokens(w['context'])} | {digest.fmt_tokens(w['tokens'])} | "
                       f"{'yes' if w['merged'] else 'no'} | {w['branch']}")
        return "\n".join(out)

    def show_text(self, caller: dict, sid: str, turn: int | None, full: bool) -> str:
        w = self.own_worker(caller, sid)
        rd = self.mdir(w["ws"], w["mission"]) / "runs" / sid
        turns = self.db.q("SELECT * FROM turns WHERE session=? ORDER BY n", sid)
        if not turn:
            lines = [f"{sid}: role {w['role']}, label {w['label']}, {w['agent']}/{w['model']}, status {w['status']}, "
                     f"worktree {w['cwd']}, branch {w['branch']}, run dir {rd}"]
            for t in turns:
                first = (t["final"] or "").strip().splitlines()[:1]
                lines.append(f"- turn {t['n']}: {t['status']} · {digest.fmt_tokens(t['tokens'])} tokens · "
                             f"{first[0][:160] if first else ''}")
            return "\n".join(lines) + "\nUse --turn N (and --full) for details."
        t = next((x for x in turns if x["n"] == turn), None)
        if not t:
            raise AgentError(f"{sid} has no turn {turn}")
        files = {k: rd / f"t{turn}.{k}" for k in ("prompt.md", "out.md", "diff")}
        out = [f"{sid} turn {turn}: {t['status']} · commits {(t['start_commit'] or '')[:8]}..{(t['end_commit'] or '')[:8]}",
               *(f"{k}: {v}" for k, v in files.items() if v.exists())]
        body = files["out.md"].read_text() if files["out.md"].exists() else ""
        if full:
            diff = files["diff"].read_text() if files["diff"].exists() else ""
            out += ["", "## Output", body, "", "## Diff", diff[:200_000]]
        else:
            out += ["", digest.trim_words(body, 400)[0]]
        return "\n".join(out)

    def status_text(self, caller: dict) -> str:
        ws, mid = caller["ws"], caller["mission"]
        m, mdir = self.mission_row(ws, mid), self.mdir(ws, mid)
        done, total = digest.checklist((mdir / "plan.md").read_text())
        recent = self.db.q("SELECT kind, payload, ts FROM inbox WHERE ws=? AND mission=? ORDER BY id DESC LIMIT 8",
                           ws, mid)
        lines = [f"Mission {mid}: {m['status']}" + (f" ({m['outcome']})" if m["outcome"] else ""),
                 f"Meta-work: {m['meta_work'] or '-'} · plan checklist {done}/{total}",
                 digest.budget_line(self.budget(ws, mid)), "Workers:", self.ls_text(caller), "Recent events:"]
        for r in recent:
            first = json.loads(r["payload"]).get("text", "").splitlines()[:1]
            lines.append(f"- {time.strftime('%H:%M', time.localtime(r['ts']))} {r['kind']}: "
                         f"{first[0][:160] if first else ''}")
        return "\n".join(lines)

    def infra_text(self) -> str:
        lines = [self.infra.summary()]
        for key, (proc, _) in list(self.runner.procs.items()):
            s = self.db.session(key)
            if s:
                lines.append(f"- {key} ({s['kind']} {s['role']}): {tree_rss(proc.pid) / 2**30:.2f} GB RSS")
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
    def create_meta_chat(self, ws: str, mid: str, overrides: dict | None = None) -> dict:
        self.mission_row(ws, mid)
        # Chosen per chat by the human; unset fields fall back to the mission's meta-work settings
        mc = mission_config(self.wsp(ws), mid)["mission"]
        conf = {k: v for k, v in (overrides or {}).items() if k in ("agent", "model", "effort") and v}
        if conf.get("agent", mc["meta_work"]["agent"]) != mc["meta_work"]["agent"]:
            conf.setdefault("model", None)
            conf.setdefault("effort", None)
        conf = {"autocompact": None, **{k: mc["meta_work"].get(k) for k in ("agent", "model", "effort")}, **conf}
        reason = agents.check_agent_conf(conf["agent"], conf, agents.load_models())
        if reason:
            raise AgentError(reason)
        return self.new_session(ws, mid, "meta-chat", "meta-chat", "meta-chat", conf, self.wsp(ws) / "meta")

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
        self.publish("meta_chat_message", session=sid, ws=s["ws"], mission=s["mission"])
        self.spawn(self._meta_chat_turn(sid, text))

    async def _meta_chat_turn(self, sid: str, text: str) -> None:
        s = self.db.session(sid)
        p = self.wsp(s["ws"])
        prompt = text if s["turns"] else f"{self.mode_text(s)}\n\nHuman: {text}"
        try:
            res, n = await self.run_turn(s, prompt)
            workspace.enforce_protected(p)
            workspace.commit(p, f"meta-chat({sid})", f"meta-chat({sid}): turn {n}")
            answer = res.final or f"(error: {res.error})"
        except Exception as exc:  # release the session and show the failure in the chat
            log.exception("meta-chat turn failed for %s", sid)
            if self.db.session(sid)["status"] == "running":
                self.db.update("sessions", {"id": sid}, status="failed")
            answer = f"(platform error: {exc!r})"
        self.db.x("INSERT INTO messages(session, ts, role, text) VALUES(?,?,?,?)", sid, time.time(),
                  "assistant", answer)
        self.publish("meta_chat_message", session=sid, ws=s["ws"], mission=s["mission"])
