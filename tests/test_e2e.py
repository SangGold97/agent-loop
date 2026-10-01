"""End-to-end with the scripted fake agent: workspace -> goal -> meta-work -> worker -> merge -> done."""
import subprocess
from pathlib import Path

import yaml

from conftest import wait_for

FAKE_ROLE = {"agent": "fake", "model": None, "effort": None, "autocompact": None}


def goal_config():
    return {"meta_work": FAKE_ROLE, "workers": {"fake": {**FAKE_ROLE, "enabled": True}},
            "budget": {"max_concurrent_workers": 2, "max_worker_turns": 5, "max_meta_turns": 10},
            "base_branch": None, "push": "manual"}


def test_full_goal(server):
    client, engine = server
    assert client.post("/api/workspaces", json={"name": "demo"}).status_code == 200
    ws = Path(engine.wsp("demo"))
    assert (ws / "meta/.claude/skills").is_symlink() and "{{PY}}" not in (ws / "meta/hooks.json").read_text()

    r = client.post("/api/workspaces/demo/goals", json={
        "title": "Hello world", "spec": "Create hello.py", "goal": "- hello.py exists\n- verify: `test -f hello.py`",
        "config": goal_config()})
    gid = r.json()["id"]
    assert client.post(f"/api/goals/demo/{gid}/start").json() == {"ok": True}

    view = wait_for(lambda: (v := client.get(f"/api/goals/demo/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)
    assert view["row"]["outcome"] == "success" and view["checklist"] == {"done": 1, "total": 1}
    assert "Status: **success**" in view["files"]["report.md"] and "`test -f hello.py` → exit 0" in view["files"]["report.md"]
    workers = [s for s in view["sessions"] if s["kind"] == "worker"]
    assert len(workers) == 1 and workers[0]["merged"] == 1
    sid = workers[0]["id"]
    run = ws / "goals" / gid / "runs" / sid
    assert (run / "t1.prompt.md").exists() and "hello.py" in (run / "t1.diff").read_text()
    assert (ws / "wt" / f"_int-{gid}" / "hello.py").exists()
    authors = subprocess.run(["git", "log", "--format=%an"], cwd=ws, capture_output=True, text=True).stdout
    assert "control-plane" in authors and "meta-work(" in authors

    # Human edits of goal are committed as human; agents cannot touch it
    client.put(f"/api/goals/demo/{gid}/files/goal.md", json={"text": "- hello.py exists\n"})
    assert subprocess.run(["git", "log", "-1", "--format=%an"], cwd=ws, capture_output=True,
                          text=True).stdout.strip() == "human"

    # Meta-chat proposes a note; the human approves it
    adv = client.post(f"/api/goals/demo/{gid}/meta-chats", json={}).json()["id"]
    client.post(f"/api/meta-chats/{adv}/messages", json={"text": "How is it going?"})
    msgs = wait_for(lambda: len(m := client.get(f"/api/meta-chats/{adv}/messages").json()["messages"]) == 2 and m)
    assert "Proposed note" in msgs[1]["text"]
    pending = [n for n in client.get(f"/api/goals/demo/{gid}").json()["notes"] if n["status"] == "pending"]
    assert pending and client.post(f"/api/notes/{pending[0]['id']}/approve", json={"mode": "queue"}).status_code == 200
    assert "Consider adding a test" in (ws / "goals" / gid / "notes.md").read_text()

    # Accept: worktrees removed, integration branch kept
    assert client.post(f"/api/goals/demo/{gid}/accept").status_code == 200
    assert not (ws / "wt" / sid).exists()
    branches = subprocess.run(["git", "branch", "--list", "loop/*"], cwd=ws / "repo", capture_output=True,
                              text=True).stdout
    assert f"loop/{gid}/integration" in branches and sid not in branches
    assert client.get(f"/api/goals/demo/{gid}").json()["row"]["status"] == "done"


def test_agent_api_requires_token(server):
    client, _ = server
    r = client.post("/api/agent/spawn", json={"role": "coder", "prompt": "x"})
    assert r.status_code == 400 and "token" in r.json()["error"]


def test_budget_final_turn(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "budget"})
    cfg = goal_config()
    cfg["budget"]["max_meta_turns"] = 1
    gid = client.post("/api/workspaces/budget/goals", json={
        "title": "Tight budget", "spec": "x", "goal": "- verify: `test -f nope.txt`", "config": cfg}).json()["id"]
    client.post(f"/api/goals/budget/{gid}/start")
    view = wait_for(lambda: (v := client.get(f"/api/goals/budget/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)
    # verify fails -> success rejected; budget exhausted -> platform closes as partial
    assert view["row"]["outcome"] == "partial"
    goal_yaml = yaml.safe_load((Path(engine.wsp("budget")) / "goals" / gid / "goal.yaml").read_text())
    assert goal_yaml["budget"]["max_meta_turns"] == 1


def test_harness_editor(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "harness"})
    files = client.get("/api/workspaces/harness/harness").json()["files"]
    assert "meta/AGENTS.md" in files and "worker/hooks.json" in files
    assert not any("/.claude/skills" in f or "claude-plugin/skills" in f for f in files)
    r = client.put("/api/workspaces/harness/harness", json={"path": "worker/skills/fit/SKILL.md", "text": "---\nname: fit\n---\n"})
    assert r.status_code == 200
    assert "worker/skills/fit/SKILL.md" in client.get("/api/workspaces/harness/harness").json()["files"]
    assert client.put("/api/workspaces/harness/harness", json={"path": "repo/x.py", "text": "x"}).status_code == 400
    assert client.put("/api/workspaces/harness/harness", json={"path": "meta/hooks.json", "text": "{bad"}).status_code == 400


def test_cross_origin_post_refused(server):
    client, _ = server
    r = client.post("/api/workspaces", content='{"name":"x"}', headers={"Origin": "http://evil.example",
                                                                        "Content-Type": "text/plain"})
    assert r.status_code == 403


def test_worker_platform_failure_reaches_meta_work(server, monkeypatch):
    client, engine = server
    client.post("/api/workspaces", json={"name": "broken"})

    def broken_diff(*_a):
        raise RuntimeError("index.lock exists")

    monkeypatch.setattr("agentloop.engine.gitops.diff", broken_diff)
    gid = client.post("/api/workspaces/broken/goals", json={
        "title": "Broken git", "spec": "x", "goal": "- x", "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/broken/{gid}/start")
    wait_for(lambda: engine.db.one("SELECT id FROM inbox WHERE ws='broken' AND kind='worker_done' "
                                   "AND payload LIKE '%failed inside the platform%'"))
    workers = [s for s in client.get(f"/api/goals/broken/{gid}").json()["sessions"] if s["kind"] == "worker"]
    assert workers and all(w["status"] != "running" for w in workers)
    client.post(f"/api/goals/broken/{gid}/stop")


def test_codex_home_keeps_system_skills_out_of_harness(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "cx"})
    from agentloop import workspace
    ws = Path(engine.wsp("cx"))
    (ws / "worker/skills/submit").mkdir(parents=True)
    (ws / "worker/skills/submit/SKILL.md").write_text("---\nname: submit\n---\n")
    home = workspace.codex_home(ws, "worker")
    assert (home / "skills").is_dir() and not (home / "skills").is_symlink()
    assert (home / "skills/submit/SKILL.md").exists()
    (home / "skills/.system").mkdir()  # what Codex does on first run
    assert not (ws / "worker/skills/.system").exists()
    assert "worker/skills/submit/SKILL.md" in workspace.harness_files(ws)


def test_cli_update_flow(server, monkeypatch):
    client, engine = server
    from agentloop import agents

    async def fake_smoke(name):
        return {"ok": True, "final": "OK"}

    monkeypatch.setitem(agents.UPDATE_CMD, "claude", ["echo", "updated to 9.9.9"])
    monkeypatch.setattr(agents, "smoke", fake_smoke)
    assert client.post("/api/agents/claude/update").status_code == 200
    op = wait_for(lambda: (o := client.get("/api/agents").json()["ops"].get("claude")) and o["state"] == "done" and o)
    assert op["result"]["ok"] and "9.9.9" in op["result"]["output"] and op["result"]["smoke"]["ok"]
    # Refused while a session of that CLI is running
    engine.db.x("INSERT INTO sessions(id, agent, status) VALUES('busy-1', 'claude', 'running')")
    try:
        r = client.post("/api/agents/claude/update")
        assert r.status_code == 400 and "running" in r.json()["error"]
    finally:
        engine.db.x("DELETE FROM sessions WHERE id='busy-1'")


def test_fixed_agent_settings(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "fixed"})
    cfg = goal_config()
    gid = client.post("/api/workspaces/fixed/goals", json={
        "title": "Fixed", "spec": "x", "goal": "- x", "config": cfg}).json()["id"]
    client.post(f"/api/goals/fixed/{gid}/start")
    worker = wait_for(lambda: next((s for s in client.get(f"/api/goals/fixed/{gid}").json()["sessions"]
                                    if s["kind"] == "worker"), None))
    meta = engine.db.one("SELECT * FROM sessions WHERE ws='fixed' AND kind='meta-work'")
    caller = {**meta}
    # A disabled CLI is refused; the meta-work only picks enabled ones
    cfg["workers"]["fake"]["enabled"] = False
    text = yaml.safe_dump({"title": "Fixed", **cfg})
    assert client.put(f"/api/goals/fixed/{gid}/files/goal.yaml", json={"text": text}).status_code == 200
    import asyncio
    engine.db.update("goals", {"ws": "fixed", "id": gid}, status="running")
    try:
        asyncio.run(engine.spawn_worker(caller, "fake", "coder", "x", "do it"))
        raise AssertionError("disabled agent was accepted")
    except Exception as exc:
        assert "not enabled" in str(exc)
    # Model/effort follow the human config at the next turn
    cfg["workers"]["fake"].update(enabled=True, model="m2", effort="low")
    client.put(f"/api/goals/fixed/{gid}/files/goal.yaml", json={"text": yaml.safe_dump({"title": "Fixed", **cfg})})
    w = engine.db.session(worker["id"])
    spec = engine.build_spec(w, "again", w["turns"] + 1)
    assert (spec.model, spec.effort) == ("m2", "low") and engine.db.session(w["id"])["model"] == "m2"
    engine.db.update("goals", {"ws": "fixed", "id": gid}, status="stopped")


def test_spec_falls_back_to_latest_goal(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "specs"})
    ws = Path(engine.wsp("specs"))
    new = lambda title, spec: client.post("/api/workspaces/specs/goals", json={
        "title": title, "spec": spec, "goal": "- x", "config": goal_config()}).json()["id"]
    first = new("First", "")
    assert "Explore the project repo" in (ws / "goals" / first / "spec.md").read_text()
    second = new("Second", "Use SQLite.")
    third = new("Third", "")
    spec = (ws / "goals" / third / "spec.md").read_text()
    assert spec.startswith(f"> Reused from goal `{second}`") and "Use SQLite." in spec
    # A reused spec is passed on once more without stacking the header
    spec = (ws / "goals" / new("Fourth", "") / "spec.md").read_text()
    assert spec.startswith(f"> Reused from goal `{third}`") and spec.count("> Reused") == 1
