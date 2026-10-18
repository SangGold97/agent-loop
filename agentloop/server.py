"""HTTP API (human UI + agent `loop` CLI), live events over SSE, and the static web UI."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agents, digest, gitops, workspace
from .adapters.claude import user_autocompact as claude_user_autocompact
from .config import goal_config, workspace_config
from .engine import AgentError, Engine

WEB = Path(__file__).parent / "web"


def create_app(api_url: str) -> FastAPI:
    engine = Engine(api_url)

    @asynccontextmanager
    async def lifespan(_app):
        await engine.startup()
        yield
        await engine.shutdown()

    app = FastAPI(title="agent-loop", lifespan=lifespan)
    app.state.engine = engine

    @app.exception_handler(AgentError)
    @app.exception_handler(ValueError)
    @app.exception_handler(RuntimeError)
    async def refused(_req, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Any web page could POST text/plain to localhost; only same-origin (or non-browser) writes are accepted
    @app.middleware("http")
    async def same_origin(req: Request, call_next):
        origin = req.headers.get("origin")
        if req.method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != req.headers.get("host"):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        return await call_next(req)

    async def body(req: Request) -> dict:
        try:
            return await req.json()
        except ValueError:
            return {}

    # ---------- overview ----------
    @app.get("/api/state")
    def state():
        out = []
        for name in workspace.list_workspaces():
            p = workspace.ws_path(name)
            goals = []
            for gid in workspace.list_goals(p):
                row = engine.goal_row(name, gid)
                goals.append({"id": gid, "title": goal_config(p, gid)["goal"]["title"],
                                 "status": row["status"], "outcome": row["outcome"]})
            out.append({"name": name, "goals": goals})
        return {"workspaces": out}

    @app.post("/api/workspaces")
    async def create_ws(req: Request):
        b = await body(req)
        p = await asyncio.to_thread(workspace.create_workspace, b.get("name", ""), b.get("repo") or None,
                                    b.get("base_branch") or None)
        return {"name": p.name}

    @app.get("/api/workspaces/{ws}")
    def get_ws(ws: str):
        p = workspace.ws_path(ws)
        return {"name": ws, "loop_yaml": (p / "loop.yaml").read_text(), "config": workspace_config(p),
                "path": str(p), "log": gitops.log(p, n=40)}

    @app.delete("/api/workspaces/{ws}")
    async def delete_ws(ws: str):
        await engine.delete_workspace(ws)
        return {"ok": True}

    @app.get("/api/workspaces/{ws}/harness")
    def harness(ws: str, path: str | None = None):
        p = workspace.ws_path(ws)
        if path:
            target = (p / path).resolve()
            if not target.is_relative_to(p.resolve()) or path not in workspace.harness_files(p):
                raise HTTPException(404)
            return {"path": path, "text": target.read_text(errors="replace")}
        return {"files": workspace.harness_files(p)}

    @app.put("/api/workspaces/{ws}/harness")
    async def put_harness(ws: str, req: Request):
        b = await body(req)
        workspace.harness_write(workspace.ws_path(ws), b.get("path", ""), b.get("text", ""))
        return {"ok": True}

    @app.put("/api/workspaces/{ws}/config")
    async def put_ws_config(ws: str, req: Request):
        engine.human_edit(ws, "loop.yaml", (await body(req)).get("text", ""))
        return {"ok": True}

    @app.post("/api/workspaces/{ws}/goals")
    async def create_goal(ws: str, req: Request):
        b = await body(req)
        config = b.get("config")
        if isinstance(config, str):
            config = yaml.safe_load(config) or {}
        errors = agents.config_errors(config or {})
        if errors:
            raise AgentError("; ".join(errors))
        gid = workspace.create_goal(workspace.ws_path(ws), b.get("title", ""), b.get("spec", ""),
                                       b.get("goal", ""), config)
        engine.goal_row(ws, gid)
        return {"id": gid}

    # ---------- goals ----------
    @app.get("/api/goals/{ws}/{gid}")
    def get_goal(ws: str, gid: str):
        p = workspace.ws_path(ws)
        row = engine.goal_row(ws, gid)
        files = workspace.read_goal_files(p, gid)
        sessions = engine.db.q("SELECT * FROM sessions WHERE ws=? AND goal=? ORDER BY created", ws, gid)
        for s in sessions:
            s.pop("token", None)
            s["live"] = engine.runner.running(s["id"])
        done, total = digest.checklist(files["plan.md"])
        # What each consultation sent back to the meta-work: the [Advice] text without its header line
        advice = {}
        for item in engine.db.q("SELECT payload FROM inbox WHERE ws=? AND goal=? AND kind='advice'", ws, gid):
            payload = json.loads(item["payload"])
            head, _, text = payload["text"].partition("\n")
            advice[payload["sid"]] = {"status": head.rpartition(" · ")[2], "text": text}
        return {"ws": ws, "id": gid, "row": row, "files": files, "config": goal_config(p, gid)["goal"],
                "budget": engine.budget(ws, gid), "checklist": {"done": done, "total": total},
                "sessions": sessions, "advice": advice,
                "notes": engine.db.q("SELECT * FROM notes WHERE ws=? AND goal=? ORDER BY id DESC", ws, gid),
                "verify": digest.verify_commands(files["goal.md"]), "meta_active": (ws, gid) in engine.meta_active,
                "base": engine.goal_base(ws, gid),
                "remote": row["status"] == "done" and gitops.has_origin(p / "repo")}

    @app.delete("/api/goals/{ws}/{gid}")
    async def delete_goal(ws: str, gid: str):
        await engine.delete_goal(ws, gid)
        return {"ok": True}

    @app.put("/api/goals/{ws}/{gid}/files/{name}")
    async def put_file(ws: str, gid: str, name: str, req: Request):
        engine.human_edit(ws, f"goals/{gid}/{name}", (await body(req)).get("text", ""))
        return {"ok": True}

    @app.post("/api/goals/{ws}/{gid}/{action}")
    async def goal_action(ws: str, gid: str, action: str, req: Request):
        b = await body(req)
        if action == "start":
            await engine.start_goal(ws, gid)
        elif action == "stop":
            await engine.stop_goal(ws, gid)
        elif action == "accept":
            return {"tags": await engine.accept(ws, gid)}
        elif action == "push":
            return await engine.push_goal(ws, gid)
        elif action == "reopen":
            await engine.reopen(ws, gid, b.get("text", ""))
        elif action == "notes":
            return {"id": engine.add_note(ws, gid, b.get("text", ""), mode=b.get("mode", "queue"))}
        elif action == "meta-chats":
            s = engine.create_meta_chat(ws, gid, b)
            return {"id": s["id"]}
        else:
            raise HTTPException(404, "unknown action")
        return {"ok": True}

    @app.get("/api/goals/{ws}/{gid}/git")
    def goal_git(ws: str, gid: str):
        p = workspace.ws_path(ws)
        branch, repo = gitops.int_branch(gid), p / "repo"
        base = engine.goal_base(ws, gid)
        return {"branches": gitops.branches(p, gid), "base": base,
                "integration_log": gitops.log(repo, f"{base}..{branch}") if gitops.branch_exists(repo, branch) else [],
                "workspace_log": [c for c in gitops.log(p, n=80) if gid in c["subject"] or c["author"] == "human"]}

    @app.post("/api/notes/{nid}/{verdict}")
    async def review_note(nid: int, verdict: str, req: Request):
        b = await body(req)
        note = engine.db.one("SELECT * FROM notes WHERE id=?", nid)
        if verdict == "approve" and b.get("reopen") and note:
            await engine.reopen(note["ws"], note["goal"], b.get("text") or note["text"], nid=nid)
        else:
            engine.review_note(nid, verdict == "approve", b.get("mode", "queue"), b.get("text"))
        return {"ok": True}

    # ---------- sessions ----------
    @app.get("/api/sessions/{sid}/events")
    def session_events(sid: str, limit: int = 400, types: str | None = None):
        return {"events": engine.session_events(sid, limit, set(types.split(",")) if types else None)}

    @app.get("/api/sessions/{sid}/turns")
    def session_turns(sid: str):
        return {"turns": engine.db.q("SELECT * FROM turns WHERE session=? ORDER BY n", sid)}

    @app.post("/api/sessions/{sid}/stop")
    async def session_stop(sid: str):
        return {"text": await engine.stop_worker(None, sid)}

    @app.get("/api/meta-chats/{sid}/messages")
    def meta_chat_messages(sid: str):
        s = engine.db.session(sid) or {}
        return {"messages": engine.db.q("SELECT * FROM messages WHERE session=? ORDER BY id", sid),
                "busy": engine.runner.running(sid) or s.get("status") == "running"}

    @app.post("/api/meta-chats/{sid}/messages")
    async def meta_chat_send(sid: str, req: Request):
        engine.meta_chat_send(sid, (await body(req)).get("text", ""))
        return {"ok": True}

    @app.get("/api/runs/{ws}/{gid}/{sid}/{name}")
    def run_file(ws: str, gid: str, sid: str, name: str):
        path = (engine.gdir(ws, gid) / "runs" / sid / name).resolve()
        if not path.is_relative_to((engine.gdir(ws, gid) / "runs").resolve()) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="text/plain")

    # ---------- infra and agent CLIs ----------
    @app.get("/api/infra")
    def infra():
        return {"latest": engine.infra.latest(), "history": list(engine.infra.history)}

    @app.get("/api/agents")
    async def agents_info():
        return {"versions": await agents.versions(), "ops": engine.agent_ops, "models": agents.load_models(),
                "quotas": engine.quotas}

    @app.get("/api/models")
    def models():
        catalog = agents.load_models()
        # The human's Claude Code auto-compact window is what sessions use when the goal sets none
        if "claude" in catalog:
            catalog["claude"]["autocompact"] = claude_user_autocompact()
        return catalog

    @app.post("/api/models/refresh")
    async def models_refresh():
        catalog = await agents.refresh_models()
        engine.publish("models")
        return catalog

    @app.post("/api/agents/{name}/{op}")
    async def agents_op(name: str, op: str):
        if name not in agents.UPDATE_CMD or op not in ("update", "smoke"):
            raise HTTPException(404)
        if engine.agent_ops.get(name, {}).get("state") == "running":
            raise AgentError(f"{name} {op} already running")
        # An update swaps the binary under live sessions, so it waits until that CLI is idle
        busy = engine.db.one("SELECT COUNT(*) AS n FROM sessions WHERE status='running' AND agent=?", name)["n"]
        if op == "update" and busy:
            raise AgentError(f"{busy} {name} session(s) running; stop them or wait before updating")
        engine.agent_ops[name] = {"state": "running", "op": op, "started": time.time()}

        async def job():
            try:
                result = await (agents.update(name) if op == "update" else agents.smoke(name))
                engine.agent_ops[name] = {"state": "done", "op": op, "result": result, "ended": time.time()}
            except Exception as exc:  # surface any failure in the UI
                engine.agent_ops[name] = {"state": "failed", "op": op, "result": {"error": repr(exc)}}
            engine.publish("agents", name=name)

        engine.spawn(job())
        return {"ok": True}

    # ---------- agent API (`loop` CLI) ----------
    @app.post("/api/agent/{cmd}")
    async def agent_cmd(cmd: str, req: Request, x_loop_token: str | None = Header(None)):
        caller, b = engine.caller(x_loop_token), await body(req)
        # Workers only use the RAM pool; steering the goal stays with the meta-work
        if caller["kind"] == "worker" and cmd not in ("ram", "ram-release", "infra"):
            raise AgentError("workers can only use `loop run` and `loop infra`")
        if caller["kind"] == "meta-advisor" and cmd not in ("ls", "show", "status", "budget", "infra"):
            raise AgentError("the advisor has read-only access; return advice to the meta-work in your final reply")
        if cmd in ("spawn", "send", "stop", "merge", "done", "advise") and caller["kind"] != "meta-work":
            raise AgentError(f"`loop {cmd}` belongs to the meta-work; propose a note with `loop note` instead")
        if cmd == "spawn":
            text = await engine.spawn_worker(caller, b.get("agent", ""), b.get("role", ""), b.get("prompt", ""))
        elif cmd == "advise":
            text = engine.advise(caller, b.get("prompt", ""))
        elif cmd == "send":
            text = await engine.send_worker(caller, b.get("sid", ""), b.get("prompt", ""))
        elif cmd == "stop":
            text = await engine.stop_worker(caller, b.get("sid", ""))
        elif cmd == "merge":
            text = await asyncio.to_thread(engine.merge_worker, caller, b.get("sid", ""))
        elif cmd == "done":
            text = await engine.done(caller, b.get("status", ""), b.get("report", ""))
        elif cmd == "note":
            if caller["kind"] != "meta-chat":
                raise AgentError("meta-work sessions write strategy to plan.md; notes are proposed by meta-chats")
            nid = engine.add_note(caller["ws"], caller["goal"], b.get("text", ""), author="meta-chat",
                                  status="pending", source=caller["id"])
            text = f"Proposed note #{nid}; the human will approve or reject it in the UI."
        elif cmd == "ls":
            text = engine.ls_text(caller)
        elif cmd == "show":
            text = engine.show_text(caller, b.get("sid", ""), b.get("turn"), bool(b.get("full")), bool(b.get("log")))
        elif cmd == "status":
            text = engine.status_text(caller)
        elif cmd == "budget":
            text = digest.budget_line(engine.budget(caller["ws"], caller["goal"]))
        elif cmd == "infra":
            text = engine.infra_text()
        elif cmd == "ram":
            return await engine.ram_grant(caller, float(b.get("gb") or 0), float(b.get("wait") or 0),
                                          int(b.get("pid") or 0), str(b.get("cmd", "")), float(b.get("vram") or 0))
        elif cmd == "ram-release":
            return engine.ram_release(caller, int(b.get("grant") or 0), bool(b.get("killed")))
        else:
            raise HTTPException(404, f"unknown command {cmd}")
        return {"text": text}

    # ---------- live events ----------
    @app.get("/api/events")
    async def events(request: Request):
        q = engine.bus.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    try:
                        evt = await asyncio.wait_for(q.get(), 15)
                        yield f"data: {json.dumps(evt, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                engine.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        # Version static assets by mtime so browsers never run a stale UI after an upgrade
        html = (WEB / "index.html").read_text()
        for name in ("app.js", "style.css"):
            html = html.replace(f"/static/{name}", f"/static/{name}?v={int((WEB / name).stat().st_mtime)}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    return app
