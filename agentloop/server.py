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
from .config import mission_config, workspace_config
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
            missions = []
            for mid in workspace.list_missions(p):
                row = engine.mission_row(name, mid)
                missions.append({"id": mid, "title": mission_config(p, mid)["mission"]["title"],
                                 "status": row["status"], "outcome": row["outcome"]})
            out.append({"name": name, "missions": missions})
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

    @app.post("/api/workspaces/{ws}/missions")
    async def create_mission(ws: str, req: Request):
        b = await body(req)
        config = b.get("config")
        if isinstance(config, str):
            config = yaml.safe_load(config) or {}
        mid = workspace.create_mission(workspace.ws_path(ws), b.get("title", ""), b.get("spec", ""),
                                       b.get("dod", ""), config)
        engine.mission_row(ws, mid)
        return {"id": mid}

    # ---------- missions ----------
    @app.get("/api/missions/{ws}/{mid}")
    def get_mission(ws: str, mid: str):
        p = workspace.ws_path(ws)
        row = engine.mission_row(ws, mid)
        files = workspace.read_mission_files(p, mid)
        sessions = engine.db.q("SELECT * FROM sessions WHERE ws=? AND mission=? ORDER BY created", ws, mid)
        for s in sessions:
            s.pop("token", None)
            s["live"] = engine.runner.running(s["id"])
        activity = engine.db.q("SELECT id, ts, kind, payload, consumed, deferred FROM inbox WHERE ws=? AND "
                               "mission=? ORDER BY id DESC LIMIT 40", ws, mid)
        for a in activity:
            a["payload"] = json.loads(a["payload"])
        done, total = digest.checklist(files["plan.md"])
        return {"ws": ws, "id": mid, "row": row, "files": files, "config": mission_config(p, mid)["mission"],
                "budget": engine.budget(ws, mid), "checklist": {"done": done, "total": total},
                "sessions": sessions, "activity": activity,
                "notes": engine.db.q("SELECT * FROM notes WHERE ws=? AND mission=? ORDER BY id DESC", ws, mid),
                "verify": digest.verify_commands(files["dod.md"]), "meta_active": (ws, mid) in engine.meta_active}

    @app.put("/api/missions/{ws}/{mid}/files/{name}")
    async def put_file(ws: str, mid: str, name: str, req: Request):
        engine.human_edit(ws, f"missions/{mid}/{name}", (await body(req)).get("text", ""))
        return {"ok": True}

    @app.post("/api/missions/{ws}/{mid}/{action}")
    async def mission_action(ws: str, mid: str, action: str, req: Request):
        b = await body(req)
        if action == "start":
            await engine.start_mission(ws, mid)
        elif action == "stop":
            await engine.stop_mission(ws, mid)
        elif action == "accept":
            return {"tags": await engine.accept(ws, mid)}
        elif action == "reopen":
            await engine.reopen(ws, mid, b.get("text", ""))
        elif action == "notes":
            return {"id": engine.add_note(ws, mid, b.get("text", ""), mode=b.get("mode", "queue"))}
        elif action == "advisors":
            s = engine.create_advisor(ws, mid, b)
            return {"id": s["id"]}
        else:
            raise HTTPException(404, "unknown action")
        return {"ok": True}

    @app.get("/api/missions/{ws}/{mid}/git")
    def mission_git(ws: str, mid: str):
        p = workspace.ws_path(ws)
        branch, repo = gitops.int_branch(mid), p / "repo"
        base = mission_config(p, mid)["mission"].get("base_branch") or gitops.current_branch(repo)
        return {"branches": gitops.branches(p, mid), "base": base,
                "integration_log": gitops.log(repo, f"{base}..{branch}") if gitops.branch_exists(repo, branch) else [],
                "workspace_log": [c for c in gitops.log(p, n=80) if mid in c["subject"] or c["author"] == "human"]}

    @app.post("/api/notes/{nid}/{verdict}")
    async def review_note(nid: int, verdict: str, req: Request):
        b = await body(req)
        note = engine.db.one("SELECT * FROM notes WHERE id=?", nid)
        if verdict == "approve" and b.get("reopen") and note:
            await engine.reopen(note["ws"], note["mission"], b.get("text") or note["text"], nid=nid)
        else:
            engine.review_note(nid, verdict == "approve", b.get("mode", "queue"), b.get("text"))
        return {"ok": True}

    # ---------- sessions ----------
    @app.get("/api/sessions/{sid}/events")
    def session_events(sid: str, limit: int = 400):
        return {"events": engine.session_events(sid, limit)}

    @app.get("/api/sessions/{sid}/turns")
    def session_turns(sid: str):
        return {"turns": engine.db.q("SELECT * FROM turns WHERE session=? ORDER BY n", sid)}

    @app.post("/api/sessions/{sid}/stop")
    async def session_stop(sid: str):
        return {"text": await engine.stop_worker(None, sid)}

    @app.get("/api/advisors/{sid}/messages")
    def advisor_messages(sid: str):
        s = engine.db.session(sid) or {}
        return {"messages": engine.db.q("SELECT * FROM messages WHERE session=? ORDER BY id", sid),
                "busy": engine.runner.running(sid), "agent": s.get("agent"), "model": s.get("model")}

    @app.post("/api/advisors/{sid}/messages")
    async def advisor_send(sid: str, req: Request):
        engine.advisor_send(sid, (await body(req)).get("text", ""))
        return {"ok": True}

    @app.get("/api/runs/{ws}/{mid}/{sid}/{name}")
    def run_file(ws: str, mid: str, sid: str, name: str):
        path = (engine.mdir(ws, mid) / "runs" / sid / name).resolve()
        if not path.is_relative_to((engine.mdir(ws, mid) / "runs").resolve()) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="text/plain")

    # ---------- infra and agent CLIs ----------
    @app.get("/api/infra")
    def infra():
        return {"latest": engine.infra.latest(), "history": list(engine.infra.history)[-200:]}

    @app.get("/api/agents")
    async def agents_info():
        return {"versions": await agents.versions(), "ops": engine.agent_ops}

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
        if cmd == "spawn":
            text = await engine.spawn_worker(caller, b.get("role", ""), b.get("label", ""), b.get("prompt", ""),
                                             bool(b.get("gpu")))
        elif cmd == "send":
            text = await engine.send_worker(caller, b.get("sid", ""), b.get("prompt", ""))
        elif cmd == "stop":
            text = await engine.stop_worker(caller, b.get("sid", ""))
        elif cmd == "merge":
            text = await asyncio.to_thread(engine.merge_worker, caller, b.get("sid", ""))
        elif cmd == "done":
            text = await engine.done(caller, b.get("status", ""), b.get("report", ""))
        elif cmd == "note":
            if caller["kind"] != "advisor":
                raise AgentError("orchestrators write strategy to plan.md; notes are proposed by advisors")
            nid = engine.add_note(caller["ws"], caller["mission"], b.get("text", ""), author="advisor",
                                  status="pending", source=caller["id"])
            text = f"Proposed note #{nid}; the human will approve or reject it in the UI."
        elif cmd == "ls":
            text = engine.ls_text(caller)
        elif cmd == "show":
            text = engine.show_text(caller, b.get("sid", ""), b.get("turn"), bool(b.get("full")))
        elif cmd == "status":
            text = engine.status_text(caller)
        elif cmd == "budget":
            text = digest.budget_line(engine.budget(caller["ws"], caller["mission"]))
        elif cmd == "infra":
            text = engine.infra_text()
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
