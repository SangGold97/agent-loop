"""End-to-end with the scripted fake agent: workspace -> goal -> meta-work -> worker -> merge -> done."""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import psutil
import pytest
import yaml

from agentloop import engine as engine_mod, runner as runner_mod, workspace
from agentloop.engine import AgentError
from agentloop.runner import jobs_tmp_root, scratch_root, systemd_available

from conftest import wait_for

FAKE_ROLE = {"agent": "fake", "model": None, "effort": None, "autocompact": None}


def goal_config():
    return {"meta_work": FAKE_ROLE, "workers": {"fake": {**FAKE_ROLE, "enabled": True}},
            "budget": {"max_concurrent_workers": 2, "max_worker_turns": 5, "max_meta_turns": 10},
            "base_branch": None}


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
    assert (run / "t1.prompt.md").exists() and (run / "t1.out.md").exists() and not list(run.glob("*.diff"))
    # The worker got the meta-work's prompt as is; its outputs are kept, its .artifacts/ never committed
    assert (run / "artifacts" / "prompt.txt").read_text() == "Create hello.py"
    # The turn's own scratch dir (its TMPDIR) is gone once the turn ended
    scratch = Path((run / "artifacts" / "tmpdir.txt").read_text())
    assert scratch.parent == scratch_root() and scratch.name == f"{sid}-t1" and not scratch.exists()
    committed = subprocess.run(["git", "ls-tree", "-r", "--name-only", workers[0]["branch"]], cwd=ws / "repo",
                               capture_output=True, text=True).stdout
    assert "hello.py" in committed and ".artifacts" not in committed
    # The shared venv the sessions ran in exists and stays out of the workspace's git
    assert (ws / ".venv" / "bin" / "python").exists()
    assert subprocess.run(["git", "check-ignore", "-q", ".venv"], cwd=ws).returncode == 0
    log = engine.turn_log("demo", sid, 1)
    assert "$ Bash python hello.py" in log and "  hello" in log
    assert (ws / "wt" / gid / "hello.py").exists()
    # The meta-work's own uncommitted change survived the merge and was committed under its name
    int_log = subprocess.run(["git", "log", "--format=%an|%s", "--", "meta_note.txt"], cwd=ws / "wt" / gid,
                             capture_output=True, text=True).stdout
    assert int_log.startswith("meta-work(") and "before merging " + sid in int_log
    authors = subprocess.run(["git", "log", "--format=%an"], cwd=ws, capture_output=True, text=True).stdout
    assert "control-plane" in authors and "meta-work(" in authors

    # Human edits of goal are committed as human; agents cannot touch it
    client.put(f"/api/goals/demo/{gid}/files/goal.md", json={"text": "- hello.py exists\n"})
    assert subprocess.run(["git", "log", "-1", "--format=%an"], cwd=ws, capture_output=True,
                          text=True).stdout.strip() == "human"

    # Meta-chat proposes a note; the human approves it. A chat is stored only with its first message
    assert client.post(f"/api/goals/demo/{gid}/meta-chats", json={"text": " "}).status_code == 400
    adv = client.post(f"/api/goals/demo/{gid}/meta-chats", json={"text": "How is it going?"}).json()["id"]
    msgs = wait_for(lambda: len(m := client.get(f"/api/meta-chats/{adv}/messages").json()["messages"]) == 2 and m)
    assert "Proposed note" in msgs[1]["text"]
    notes = client.get(f"/api/goals/demo/{gid}").json()["notes"]
    pending = [n for n in notes if n["status"] == "pending"]
    assert pending and client.post(f"/api/notes/{pending[0]['id']}/approve", json={"mode": "queue"}).status_code == 200
    # Notes are numbered within their goal
    assert pending[0]["seq"] == len(notes) and f"Proposed note #{len(notes)};" in msgs[1]["text"]
    assert f"## Note #{len(notes)} " in (ws / "goals" / gid / "notes.md").read_text()
    assert "Consider adding a test" in (ws / "goals" / gid / "notes.md").read_text()
    # Deleting the chat drops its session, messages and logs; the note it proposed stays
    assert client.delete(f"/api/meta-chats/{adv}").status_code == 200
    assert not any(s["id"] == adv for s in client.get(f"/api/goals/demo/{gid}").json()["sessions"])
    assert not engine.db.q("SELECT id FROM messages WHERE session=?", adv)
    assert not any(p.exists() for p in engine.logs("demo", adv))
    assert "Consider adding a test" in (ws / "goals" / gid / "notes.md").read_text()

    # Accept: worktrees removed, integration branch kept
    assert client.post(f"/api/goals/demo/{gid}/accept").status_code == 200
    assert not (ws / "wt" / sid).exists()
    branches = subprocess.run(["git", "branch", "--list", "loop/*"], cwd=ws / "repo", capture_output=True,
                              text=True).stdout
    assert f"loop/{gid}/integration" in branches and sid not in branches
    assert client.get(f"/api/goals/demo/{gid}").json()["row"]["status"] == "done"


def test_retired_worker_keeps_branch_and_log_but_cannot_be_sent(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "retire"})
    ws = Path(engine.wsp("retire"))
    gid = client.post("/api/workspaces/retire/goals", json={
        "title": "Retire", "spec": "Create hello.py RETIRE", "goal": "- hello.py exists",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/retire/{gid}/start")
    view = wait_for(lambda: (v := client.get(f"/api/goals/retire/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)
    w = next(s for s in view["sessions"] if s["kind"] == "worker")
    merged, retired, send, again, ls = (ws / "goals" / gid / ".retire.txt").read_text().split("\n---\n")
    assert merged.startswith("Merged") and retired.startswith(f"Retired {w['id']}")
    assert "is retired" in send and w["branch"] in send and again == f"{w['id']} is already retired."
    assert "| retired |" in ls and w["status"] == "retired"
    # The worktree is gone; the branch and the kept outputs stay
    assert not Path(w["cwd"]).exists() and (ws / "goals" / gid / "runs" / w["id"] / "artifacts" / "prompt.txt").exists()
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", w["branch"]], cwd=ws / "repo").returncode == 0
    assert client.post(f"/api/goals/retire/{gid}/accept").status_code == 200


def test_worker_edit_to_human_file_is_reverted(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "tamper"})
    ws = Path(engine.wsp("tamper"))
    goal_text = "- hello.py exists\n- verify: `test -f hello.py`"
    gid = client.post("/api/workspaces/tamper/goals", json={
        "title": "Tamper", "spec": "Create hello.py TAMPER", "goal": goal_text, "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/tamper/{gid}/start")
    wait_for(lambda: client.get(f"/api/goals/tamper/{gid}").json()["row"]["status"] == "awaiting_review")
    assert (ws / "goals" / gid / "goal.md").read_text() == goal_text + "\n"
    assert list((ws / ".loop" / "reverted").rglob("goal.md"))
    digests = engine.db.q("SELECT payload FROM inbox WHERE ws=? AND goal=? AND kind='worker_done'", "tamper", gid)
    assert any("Reverted this worker's edits" in d["payload"] for d in digests)


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

    def broken_digest(*_a):
        raise RuntimeError("index.lock exists")

    monkeypatch.setattr("agentloop.engine.digest.worker_digest", broken_digest)
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
        asyncio.run(engine.spawn_worker(caller, "fake", "coder", "do it"))
        raise AssertionError("disabled agent was accepted")
    except Exception as exc:
        assert "not enabled" in str(exc)
    # An existing worker of that CLI cannot be resumed either
    with pytest.raises(AgentError, match="no longer enabled"):
        asyncio.run(engine.send_worker(caller, worker["id"], "go on"))
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


def test_delete_goal_and_workspace(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "trash"})
    ws = Path(engine.wsp("trash"))
    new = lambda title: client.post("/api/workspaces/trash/goals", json={
        "title": title, "spec": "Create hello.py", "goal": "- verify: `test -f hello.py`",
        "config": goal_config()}).json()["id"]
    gid, kept = new("Doomed"), new("Kept")
    client.post(f"/api/goals/trash/{gid}/start")
    wait_for(lambda: client.get(f"/api/goals/trash/{gid}").json()["row"]["status"] == "awaiting_review")
    sids = [s["id"] for s in engine.db.q("SELECT id FROM sessions WHERE ws='trash' AND goal=?", gid)]
    assert sids
    (ws / ".loop" / "sessions" / f"{sids[0]}.t1.last.txt").write_text("last")  # as Codex writes it

    # A goal goes with its sessions, worktrees, branches, files and rows; other goals stay
    assert client.delete(f"/api/goals/trash/{gid}").json() == {"ok": True}
    git = lambda *a: subprocess.run(["git", *a], cwd=ws / "repo", capture_output=True, text=True).stdout
    assert f"loop/{gid}/" not in git("branch", "--list", "loop/*") and len(git("worktree", "list").splitlines()) == 1
    assert not (ws / "goals" / gid).exists() and not list((ws / ".loop" / "sessions").glob(f"{sids[0]}.*"))
    assert not engine.db.q("SELECT id FROM sessions WHERE ws='trash' AND goal=?", gid)
    assert not engine.db.q("SELECT id FROM turns WHERE session IN (%s)" % ",".join("?" * len(sids)), *sids)
    assert not engine.db.goal("trash", gid) and (ws / "goals" / kept / "goal.yaml").exists()
    assert subprocess.run(["git", "log", "-1", "--format=%an %s"], cwd=ws, capture_output=True,
                          text=True).stdout.strip() == f"human human: deleted goal {gid}"
    assert client.get(f"/api/goals/trash/{gid}").status_code == 400

    # A workspace goes entirely
    assert client.delete("/api/workspaces/trash").json() == {"ok": True}
    assert not ws.exists() and not engine.db.q("SELECT id FROM goals WHERE ws='trash'")
    assert "trash" not in [w["name"] for w in client.get("/api/state").json()["workspaces"]]


def test_push_opens_pr_and_never_touches_base(server, monkeypatch, tmp_path):
    client, engine = server
    from agentloop import gitops
    monkeypatch.setattr(gitops.shutil, "which", lambda name, **kw: None)  # no gh: push only, the PR is skipped
    client.post("/api/workspaces", json={"name": "pushing"})
    repo = Path(engine.wsp("pushing")) / "repo"
    git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()
    gid = client.post("/api/workspaces/pushing/goals", json={
        "title": "Push me", "spec": "Create hello.py", "goal": "- verify: `test -f hello.py`",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/pushing/{gid}/start")
    wait_for(lambda: client.get(f"/api/goals/pushing/{gid}").json()["row"]["status"] == "awaiting_review")
    push = lambda: client.post(f"/api/goals/pushing/{gid}/push", json={})
    assert "accept the goal" in push().json()["error"]
    client.post(f"/api/goals/pushing/{gid}/accept")
    view = client.get(f"/api/goals/pushing/{gid}").json()
    assert view["base"] == "main" and view["row"]["base"] == "main" and view["remote"] is False
    assert "no remote" in push().json()["error"]

    remote = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote))
    main_before = git("rev-parse", "main")
    r = push().json()
    assert r["branch"] == f"loop/{gid}/integration" and r["base"] == "main" and r["pr"] is None and r["pr_error"]
    assert git("branch", "--format=%(refname:short)", cwd=remote) == f"loop/{gid}/integration"
    assert git("rev-parse", "main") == main_before and not (repo / "hello.py").exists()
    assert client.get(f"/api/goals/pushing/{gid}").json()["row"]["pushed"]


def test_start_pulls_base_from_origin(server, tmp_path):
    client, engine = server
    client.post("/api/workspaces", json={"name": "pulling"})
    repo = Path(engine.wsp("pulling")) / "repo"
    git = lambda *a, cwd=repo: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                                              capture_output=True, text=True).stdout.strip()
    remote, other = tmp_path / "origin.git", tmp_path / "other"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote)), git("push", "-q", "origin", "main")
    # A PR merged on the remote after the clone
    git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    (other / "merged.txt").write_text("from a PR\n")
    git("add", "-A", cwd=other), git("commit", "-qm", "merged PR", cwd=other), git("push", "-q", cwd=other)
    new = lambda title: client.post("/api/workspaces/pulling/goals", json={
        "title": title, "spec": "Create hello.py", "goal": "- x", "config": goal_config()}).json()["id"]

    gid = new("Pull first")
    assert client.post(f"/api/goals/pulling/{gid}/start").json() == {"ok": True}
    assert (repo / "merged.txt").exists() and git("rev-parse", "main") == git("rev-parse", "HEAD", cwd=other)
    assert git("rev-parse", f"loop/{gid}/integration") == git("rev-parse", "main")
    client.post(f"/api/goals/pulling/{gid}/stop")

    # A local main that cannot fast-forward refuses the start and is left as it is
    (repo / "local.txt").write_text("not pushed\n")
    git("add", "-A"), git("commit", "-qm", "local only")
    (other / "second.txt").write_text("x\n")
    git("add", "-A", cwd=other), git("commit", "-qm", "second PR", cwd=other), git("push", "-q", cwd=other)
    local, gid2 = git("rev-parse", "main"), new("Diverged")
    r = client.post(f"/api/goals/pulling/{gid2}/start")
    assert r.status_code == 400 and "fast-forward" in r.json()["error"] and git("rev-parse", "main") == local
    assert client.get(f"/api/goals/pulling/{gid2}").json()["row"]["status"] == "draft"


def test_meta_usage_limit_waits_for_reset(server, monkeypatch):
    client, engine = server
    monkeypatch.setattr("agentloop.engine.LIMIT_SLACK_S", 0)
    client.post("/api/workspaces", json={"name": "quota"})
    gid = client.post("/api/workspaces/quota/goals", json={
        "title": "Quota", "spec": "Create hello.py LIMIT", "goal": "- hello.py exists\n- verify: `test -f hello.py`",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/quota/{gid}/start")
    # The refused attempt costs no meta turn and no failure; the kickoff is redelivered after the reset
    wait_for(lambda: client.get(f"/api/goals/quota/{gid}").json()["row"]["status"] == "awaiting_review")
    # The closing turn is counted once it ends, just after `loop done`
    row = wait_for(lambda: (r := client.get(f"/api/goals/quota/{gid}").json()["row"])["meta_turns"] == 2 and r)
    assert row["fail_streak"] == 0


def test_worker_usage_limit_is_reported_and_refunded(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "wquota"})
    gid = client.post("/api/workspaces/wquota/goals", json={
        "title": "Worker quota", "spec": "Create hello.py WLIMIT", "goal": "- hello.py exists",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/wquota/{gid}/start")
    try:
        wait_for(lambda: engine.db.one("SELECT id FROM inbox WHERE ws='wquota' AND kind='worker_done' "
                                       "AND payload LIKE '%usage limit until%'"))
        assert client.get(f"/api/goals/wquota/{gid}").json()["row"]["worker_turns"] == 0
        with pytest.raises(AgentError, match="usage limit"):
            engine.check_limit("fake")
    finally:
        client.post(f"/api/goals/wquota/{gid}/stop")
        engine.vendor_limits.clear()  # the fake CLI serves the other tests


def test_worker_cut_off_by_usage_limit_resumes_at_reset(server, monkeypatch):
    client, engine = server
    monkeypatch.setattr("agentloop.engine.LIMIT_SLACK_S", 0)
    client.post("/api/workspaces", json={"name": "wresume"})
    gid = client.post("/api/workspaces/wresume/goals", json={
        "title": "Worker resume", "spec": "Create hello.py WLIMIT_SOON", "goal": "- hello.py exists",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/wresume/{gid}/start")
    # The platform resumes the worker with its task at the reset; only that turn counts
    wait_for(lambda: client.get(f"/api/goals/wresume/{gid}").json()["row"]["status"] == "awaiting_review")
    w = engine.db.one("SELECT * FROM sessions WHERE ws='wresume' AND kind='worker'")
    assert w["turns"] == 2 and w["resume_at"] is None
    assert engine.db.goal("wresume", gid)["worker_turns"] == 1
    prompt = (engine.gdir("wresume", gid) / "runs" / w["id"] / "t2.prompt.md").read_text()
    assert prompt.startswith("[System] Your last turn was cut off by the fake usage limit") and \
        prompt.endswith("Create hello.py")


def int_git(ws: Path, gid: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=ws / "wt" / gid, capture_output=True, text=True).stdout


def test_meta_work_does_the_work_itself_over_several_turns(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "direct"})
    ws = Path(engine.wsp("direct"))
    gid = client.post("/api/workspaces/direct/goals", json={
        "title": "Direct", "spec": "Small change DIRECT", "goal": "- steps exist\n- verify: `test -f step3.py`",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/direct/{gid}/start")
    # Three turns that only change files (no worker, no commit) count as progress, so the goal never pauses
    wait_for(lambda: client.get(f"/api/goals/direct/{gid}").json()["row"]["status"] == "awaiting_review")
    view = client.get(f"/api/goals/direct/{gid}").json()
    assert view["row"]["outcome"] == "success" and view["row"]["worker_turns"] == 0
    assert not [s for s in view["sessions"] if s["kind"] == "worker"]
    assert int_git(ws, gid, "status", "--porcelain") == ""
    authors = int_git(ws, gid, "log", "--format=%an", f"loop/{gid}/integration", "-3").split()
    assert len(authors) == 3 and all(a.startswith("meta-work(") for a in authors)
    turns = engine.db.q("SELECT start_commit, end_commit FROM turns WHERE session=? ORDER BY n",
                        view["row"]["meta_work"])
    assert turns[0]["start_commit"] != turns[0]["end_commit"]


def test_verify_that_edits_tracked_files_is_rejected_and_undone(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "vedit"})
    ws = Path(engine.wsp("vedit"))
    gid = client.post("/api/workspaces/vedit/goals", json={
        "title": "Verify edits", "spec": "Small change DIRECT",
        "goal": "- steps exist\n- verify: `echo extra >> step1.py && mkdir -p out && touch out/run.log`",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/vedit/{gid}/start")
    view = wait_for(lambda: (v := client.get(f"/api/goals/vedit/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)
    assert view["row"]["outcome"] == "partial"
    wt = ws / "wt" / gid
    assert (wt / "step1.py").read_text() == "print(1)\n" and not (wt / "out").exists()
    # The goal closes inside the turn, so its output is recorded a moment later
    final = wait_for(lambda: engine.db.one("SELECT final FROM turns WHERE session=? ORDER BY n DESC LIMIT 1",
                                           view["row"]["meta_work"])["final"])
    assert "changed tracked files" in final


def test_failed_checkpoint_blocks_spawn_and_done(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "offbranch"})
    ws = Path(engine.wsp("offbranch"))
    gid = client.post("/api/workspaces/offbranch/goals", json={
        "title": "Off branch", "spec": "OFFBRANCH", "goal": "- wip", "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/offbranch/{gid}/start")
    view = wait_for(lambda: (v := client.get(f"/api/goals/offbranch/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)
    final = wait_for(lambda: engine.db.one("SELECT final FROM turns WHERE session=?", view["row"]["meta_work"])["final"])
    assert final.count("could not commit your changes") == 2 and "Goal closed as partial" in final
    assert view["row"]["outcome"] == "partial" and not [s for s in view["sessions"] if s["kind"] == "worker"]
    # Back on the branch, the change was saved by the close; the touched repo/ was reported, not reverted
    assert "wip.py" in int_git(ws, gid, "show", "--name-only", "--format=", f"loop/{gid}/integration")
    assert (ws / "repo" / "stray.txt").exists()
    wait_for(lambda: engine.db.one("SELECT id FROM inbox WHERE ws='offbranch' AND goal=? AND payload LIKE "
                                   "'%repo/ (branch%'", gid))


def test_meta_chat_change_to_project_is_reported(server, monkeypatch):
    client, engine = server
    client.post("/api/workspaces", json={"name": "chatw"})
    gid = client.post("/api/workspaces/chatw/goals", json={
        "title": "Chat write", "spec": "Small change DIRECT", "goal": "- verify: `test -f step3.py`",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/chatw/{gid}/start")
    wait_for(lambda: client.get(f"/api/goals/chatw/{gid}").json()["row"]["status"] == "awaiting_review")
    wait_for(lambda: ("chatw", gid) not in engine.meta_active)  # the closing turn has wrapped up
    alerts, publish = [], engine.publish
    monkeypatch.setattr(engine, "publish", lambda kind, **d: (alerts.append(d) if kind == "alert" else None,
                                                               publish(kind, **d)))
    client.post(f"/api/goals/chatw/{gid}/meta-chats", json={"text": "CHATWRITE please"})
    wait_for(lambda: any("integration worktree" in a.get("text", "") for a in alerts))


def test_goal_id_is_the_title_slug_and_must_be_new(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "ids"})
    ws = Path(engine.wsp("ids"))
    new = lambda title: client.post("/api/workspaces/ids/goals", json={
        "title": title, "spec": "Small change DIRECT", "goal": "- x", "config": goal_config()})
    first = new("Lọc từ theo tần suất")
    assert first.status_code == 200 and first.json()["id"] == "loc-tu-theo-tan-suat"
    # Same slug, other spelling: refused, nothing created
    dup = new("lọc TỪ theo tần-suất!")
    assert dup.status_code == 400 and "already exists" in dup.json()["error"]
    assert new("w-1a2b3c").status_code == 400  # would share wt/ with a worker
    # A branch left by a deleted goal's id still blocks it
    subprocess.run(["git", "branch", "loop/old-goal/integration"], cwd=ws / "repo", check=True)
    assert "already exists" in new("Old goal").json()["error"]
    # Newest first by creation time, not by id
    time.sleep(1.1)
    assert new("Another goal").json()["id"] == "another-goal"
    assert workspace.list_goals(ws) == ["another-goal", "loc-tu-theo-tan-suat"]


def test_loop_run_starts_jobs_in_the_pool_and_reports_short_ones_in_place(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "ram"})
    ws = Path(engine.wsp("ram"))
    gid = client.post("/api/workspaces/ram/goals", json={
        "title": "RAM job", "spec": "Create hello.py RAMJOB", "goal": "- hello.py exists",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/ram/{gid}/start")
    view = wait_for(lambda: (v := client.get(f"/api/goals/ram/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)
    sid = next(s["id"] for s in view["sessions"] if s["kind"] == "worker")
    out = (ws / "goals" / gid / "runs" / sid / "artifacts" / "ram.txt").read_text().split("\n---\n")
    adj, ok, old, spawn, gpu, threads = out
    assert "started with the RAM pool" in ok and "cuda ''" in ok and "done (exit 0)" in ok
    # The job's own OMP_NUM_THREADS wins over the default; what it leaves unset gets the defaults
    own, default = [line.split() for line in threads.splitlines() if line.startswith("threads")]
    assert own[1] == "3" and int(own[2]) >= 2 and own[3] == "PASSIVE"
    assert int(default[1]) >= 2 and default[1] == default[2] and default[3] == "PASSIVE"
    assert "peak RAM" in ok  # the job lives long enough to be sampled
    assert "--ram is ignored" in old and "done (exit 0)" in old
    if engine.infra.latest()["gpus"]:
        assert "0.5 GB VRAM on GPU" in gpu and "gpu job" in gpu and " 0.5" in gpu
    else:
        assert "no GPU on this host" in gpu
    assert "workers can only use" in spawn
    assert not engine.grants and not engine.ram_waiting
    # Jobs that ended while their `loop run` showed them were reported there, not by resuming the worker
    assert all(j["reported"] for j in engine.db.q("SELECT reported FROM jobs WHERE ws='ram'"))
    assert engine.db.session(sid)["turns"] == 1
    if systemd_available():  # the pool, the job scopes and the OOM order need systemd
        assert adj == "0 200"
        # Jobs share the CPU per scope at equal weight; the pool slice yields to its siblings
        assert default[5:] == ["100", str(runner_mod.POOL_CPU_WEIGHT)]


def test_long_job_outlives_its_turn_and_resumes_the_worker(server, monkeypatch):
    client, engine = server
    monkeypatch.setattr(engine_mod, "JOB_WAIT_S", 1)  # `loop run` leaves its job to the background at once
    client.post("/api/workspaces", json={"name": "detach"})
    ws = Path(engine.wsp("detach"))
    gid = client.post("/api/workspaces/detach/goals", json={
        "title": "Detached job", "spec": "Create hello.py DETACH", "goal": "- hello.py exists",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/detach/{gid}/start")
    wait_for(lambda: client.get(f"/api/goals/detach/{gid}").json()["row"]["status"] == "awaiting_review", timeout=60)
    gdir = ws / "goals" / gid
    sid = engine.db.one("SELECT id FROM sessions WHERE ws='detach' AND kind='worker'")["id"]
    started = (gdir / "runs" / sid / "artifacts" / "detach.txt").read_text()
    assert started.count("is running in the background") == 2 and "instead of polling the job" in started
    # While the worker was idle, the meta-work saw its jobs running
    assert (gdir / ".ls.txt").read_text().count(f"| {sid} |") == 2
    # The short job outlived the turn that started it, and its result started the worker's next turn
    resumed = (gdir / "runs" / sid / "artifacts" / "job.txt").read_text()
    # It names the job's log, which holds the output, instead of quoting it
    log = re.search(r"^Output: (\S+)$", resumed, re.M).group(1)
    assert "ended] exit 0" in resumed and resumed.endswith(f"Output: {log}")
    assert f"detached job output {jobs_tmp_root()}" in Path(log).read_text()
    assert resumed.startswith("[System] The `loop run` job is done with the result of j-")
    digests = engine.db.q("SELECT payload FROM inbox WHERE ws='detach' AND kind='worker_done' ORDER BY id")
    assert len(digests) == 2 and "result of j-" in digests[1]["payload"]
    # Both digests tell the meta-work that the long job still runs and the worker resumes with its result
    long_job = engine.db.one("SELECT id FROM jobs WHERE ws='detach' AND cmd='sleep 300'")["id"]
    assert all(long_job in d["payload"] and f"still running; {sid} resumes" in d["payload"] for d in digests)
    assert f"Job {long_job} is still running" in digests[1]["payload"]
    # Closing the goal stopped the long job; no grant, process or scratch dir is left
    assert sorted(j["status"] for j in engine.db.q("SELECT status FROM jobs WHERE ws='detach'")) == ["done", "stopped"]
    assert not engine.grants
    # A job's record keeps what restores it after a restart, not its last RAM or VRAM measurement
    assert not any("rss_gb" in json.loads(j["grant_info"])
                   for j in engine.db.q("SELECT grant_info FROM jobs WHERE ws='detach'"))
    wait_for(lambda: not [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] == ["sleep", "300"]],
             timeout=10)
    assert not jobs_tmp_root().exists() or not any(jobs_tmp_root().iterdir())


def test_detached_jobs_end_at_their_time_limit_or_with_their_helper(server):
    client, engine = server
    client.post("/api/workspaces", json={"name": "jobs"})
    helper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    gone = subprocess.Popen(["true"])
    gone.wait()
    for jid, pid, started in (("j-old", helper.pid, time.time() - 10 * 3600), ("j-gone", gone.pid, time.time())):
        engine.db.x("INSERT INTO jobs(id, session, ws, goal, cmd, log, pid, started, status) VALUES(?,?,?,?,?,?,?,?,?)",
                    jid, "w-none", "jobs", "g", "sleep 60", "/nonexistent.log", pid, started, "running")
    # The sampling loop stops a job past job_timeout_h and ends one whose helper is gone
    wait_for(lambda: not engine.db.q("SELECT 1 FROM jobs WHERE ws='jobs' AND status='running'"), timeout=10)
    assert helper.wait(timeout=10) == -9
    results = {r["id"]: r["result"] for r in engine.db.q("SELECT id, result FROM jobs WHERE ws='jobs'")}
    assert "stopped at the 9 h limit" in results["j-old"] and "ended without reporting" in results["j-gone"]


def test_cli_usage_limits_are_kept_shown_and_warned(server, monkeypatch):
    client, engine = server
    client.post("/api/workspaces", json={"name": "usage"})
    gid = client.post("/api/workspaces/usage/goals", json={
        "title": "Usage", "spec": "Create hello.py", "goal": "- hello.py exists", "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/usage/{gid}/start")
    wait_for(lambda: client.get(f"/api/goals/usage/{gid}").json()["row"]["status"] == "awaiting_review")
    # Every turn's report is kept per CLI and shown to agents and the UI
    assert [w["name"] for w in engine.quotas["fake"]["windows"]] == ["5h"]
    assert "CLI usage limits: fake 5h 25%" in engine.infra_text()
    assert client.get("/api/agents").json()["quotas"]["fake"]["windows"][0]["used"] == 0.25
    # A queued note waits until the human delivers it now
    nid = client.post(f"/api/goals/usage/{gid}/notes", json={"text": "later", "mode": "queue"}).json()["id"]
    note = lambda: next(n for n in client.get(f"/api/goals/usage/{gid}").json()["notes"] if n["id"] == nid)
    assert note()["status"] == "queued"
    assert client.post(f"/api/notes/{nid}/deliver", json={}).status_code == 200
    assert note()["status"] == "delivered" and client.post(f"/api/notes/{nid}/deliver", json={}).status_code == 400
    # ...or rejects it, which takes it out of notes.md before the meta-work reads it
    nid = client.post(f"/api/goals/usage/{gid}/notes", json={"text": "never mind", "mode": "queue"}).json()["id"]
    assert client.post(f"/api/notes/{nid}/reject", json={}).status_code == 200
    notes_md = (engine.wsp("usage") / "goals" / gid / "notes.md").read_text()
    assert note()["status"] == "rejected" and "never mind" not in notes_md and "later" in notes_md
    # Past QUOTA_WARN, running goals hear it once, deferred, until the window drops below it again
    engine.db.x("UPDATE goals SET status='running' WHERE ws='usage' AND id=?", gid)
    try:
        high = [{"name": "5h", "used": 0.95, "resets_at": time.time() + 3600}]
        for windows in (high, high, [{**high[0], "used": 0.2}], high):
            engine.set_quota("fake", windows)
        warned = engine.db.q("SELECT deferred FROM inbox WHERE ws='usage' AND payload LIKE '%fake has used 95%'")
        assert [r["deferred"] for r in warned] == [1, 1]
        # Low disk wakes the meta-work once, and again only after free disk has recovered
        kicked = []
        monkeypatch.setattr(engine, "kick", lambda ws, g: kicked.append(ws))
        for free in (50, 8, 5, 15, 8, 25, 9):
            engine.check_disk(free)
        disk = engine.db.q("SELECT deferred FROM inbox WHERE ws='usage' AND payload LIKE '%of disk is free%'")
        assert [r["deferred"] for r in disk] == [0, 0] and kicked == ["usage", "usage"]
    finally:
        engine.db.x("UPDATE goals SET status='awaiting_review' WHERE ws='usage' AND id=?", gid)
        engine.db.x("UPDATE inbox SET consumed=1 WHERE ws='usage'")
        engine.set_quota("fake", [{"name": "5h", "used": 0.25, "resets_at": time.time() + 3600}])
        engine.check_disk(100)


def test_goal_refused_for_ram_waits_instead_of_pausing(server, monkeypatch):
    client, engine = server
    client.post("/api/workspaces", json={"name": "ramwait"})
    monkeypatch.setattr(engine, "ram_share", lambda: 0.92)  # another goal fills the pool
    gid = client.post("/api/workspaces/ramwait/goals", json={
        "title": "RAM wait", "spec": "Create hello.py", "goal": "- hello.py exists",
        "config": goal_config()}).json()["id"]
    client.post(f"/api/goals/ramwait/{gid}/start")
    wait_for(lambda: engine.db.one("SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session WHERE s.ws='ramwait' "
                                   "AND s.kind='meta-work' AND t.status='idle'"))
    time.sleep(1.5)
    # The refused spawn leaves the goal waiting: still running, not counted idle, not re-prompted
    row = client.get(f"/api/goals/ramwait/{gid}").json()["row"]
    assert row["status"] == "running" and row["idle_streak"] == 0 and row["meta_turns"] == 1
    assert ("ramwait", gid) in engine.ram_wait_goals
    monkeypatch.undo()
    # The pool has room again: one event wakes the meta-work, whose next turn (here) closes the goal
    wait_for(lambda: client.get(f"/api/goals/ramwait/{gid}").json()["row"]["status"] == "awaiting_review")
    assert engine.db.one("SELECT 1 FROM inbox WHERE ws='ramwait' AND payload LIKE '%has room again%'")
    assert ("ramwait", gid) not in engine.ram_wait_goals


def test_loop_run_waits_while_the_pool_is_full(server, monkeypatch):
    _, engine = server
    worker = {"id": "w-ramtest", "kind": "worker"}
    with pytest.raises(AgentError, match="belongs to workers"):
        asyncio.run(engine.ram_grant({"id": "mc-x", "kind": "meta-chat"}, 0, os.getpid(), "x"))
    monkeypatch.setattr(engine, "ram_share", lambda: 0.91)
    with pytest.raises(AgentError, match="RAM pool has no room") as refused:
        asyncio.run(engine.ram_grant(worker, 0, os.getpid(), "python big.py"))
    assert "waits from 90%" in str(refused.value)
    monkeypatch.undo()
    first = asyncio.run(engine.ram_grant(worker, 5, os.getpid(), "python big.py"))
    assert "started with the RAM pool" in first["text"]
    # One job starts per sample, so the next one sees what the first took
    started = time.time()
    second = asyncio.run(engine.ram_grant(worker, 5, os.getpid(), "python other.py"))
    assert time.time() - started > 0.5
    # The infra page lists both, oldest first, with the cores they use once sampled twice
    jobs = wait_for(lambda: len(j := engine.infra.latest().get("jobs", [])) == 2
                    and all(x["cpu_cores"] is not None for x in j) and j, timeout=10)
    assert [j["cmd"] for j in jobs] == ["python big.py", "python other.py"] and not any(j["job"] for j in jobs)
    for key in (first["grant"], second["grant"]):
        engine.ram_release(worker, key)
    assert not engine.grants
    # A job whose `loop run` died without ending it is dropped by the sampling loop
    dead = subprocess.Popen(["true"])
    dead.wait()
    asyncio.run(engine.ram_grant(worker, 5, dead.pid, "true"))
    wait_for(lambda: not engine.grants, timeout=10)


def test_job_threads_follow_free_cores_and_a_saturated_cpu_is_reported_once(server, monkeypatch):
    client, engine = server
    # The sampling loop must not add real samples to the history the test sets
    monkeypatch.setattr(engine.infra, "sample", lambda: (_ for _ in ()).throw(RuntimeError("paused by the test")))
    cpus = engine.infra.latest()["cpus"]
    monkeypatch.setattr(engine, "grants", {})
    assert engine.job_threads() == max(engine_mod.MIN_JOB_THREADS, cpus)
    # A job not yet measured counts with its own default, so a job started right after it gets the minimum
    engine.grants[1] = {"threads": cpus}
    assert engine.job_threads() == engine_mod.MIN_JOB_THREADS
    # Once measured, it counts with the cores it uses
    engine.grants[1]["cpu_cores"] = 1.0
    assert engine.job_threads() == max(engine_mod.MIN_JOB_THREADS, cpus - 1)
    engine.grants.clear()

    client.post("/api/workspaces", json={"name": "cpu"})
    gid = client.post("/api/workspaces/cpu/goals", json={
        "title": "CPU", "spec": "x", "goal": "- x", "config": goal_config()}).json()["id"]
    engine.db.x("UPDATE goals SET status='running' WHERE ws='cpu' AND id=?", gid)
    history = engine.infra.history
    try:
        def fill(pct, n=history.maxlen):
            engine.infra.history = deque([{**history[-1], "cpu_pct": pct}] * n, maxlen=history.maxlen)
            engine.check_cpu()

        # Under 10 minutes of samples says nothing; a saturated 10-minute mean is said once, deferred, until the
        # mean falls below the re-arm level
        for pct, n in ((99, history.maxlen - 1), (95, history.maxlen), (95, history.maxlen), (80, history.maxlen),
                       (95, history.maxlen), (60, history.maxlen), (92, history.maxlen)):
            fill(pct, n)
        warned = engine.db.q("SELECT deferred FROM inbox WHERE ws='cpu' AND payload LIKE '%busy over the last 10%'")
        assert [r["deferred"] for r in warned] == [1, 1]
        assert "(92% over 10 min)" in engine.infra_text()
    finally:
        engine.infra.history = history
        engine.cpu_warned = False
        engine.db.x("UPDATE goals SET status='awaiting_review' WHERE ws='cpu' AND id=?", gid)
        engine.db.x("UPDATE inbox SET consumed=1 WHERE ws='cpu'")


def test_newest_heavy_process_is_stopped_when_the_pool_is_full(server, monkeypatch):
    _, engine = server
    worker = {"id": "w-rampolice", "kind": "worker"}
    # The sampling loop must not measure the fake jobs over what the test sets
    monkeypatch.setattr(engine.infra, "sample", lambda: (_ for _ in ()).throw(RuntimeError("paused by the test")))
    monkeypatch.setattr(engine_mod, "HEAVY_GB", 0.02)
    helpers = [subprocess.Popen(["sleep", "60"]) for _ in range(2)]
    tool = None
    try:
        a, b = [], []
        for out, h in zip((a, b), helpers):
            engine.ram_admitted = 0.0
            out.append(asyncio.run(engine.ram_grant(worker, 0, h.pid, f"job{h.pid}"))["grant"])
        a, b = a[0], b[0]
        engine.grants[a]["rss_gb"], engine.grants[b]["rss_gb"] = 0.5, 0.01
        # A tool process of the worker's turn, started after the jobs (a process's start time is known to about a
        # second) and holding more than HEAVY_GB
        time.sleep(1.5)
        tool = subprocess.Popen([sys.executable, "-c", "import time; x = bytearray(60 * 2**20); time.sleep(60)"])
        wait_for(lambda: psutil.Process(tool.pid).memory_info().rss > 50 * 2**20)
        engine.turn_mem[worker["id"]] = {"peak": 0.0, "oom": 0, "over": [], "tools": {tool.pid}}
        # Below RAM_KILL nothing is stopped
        monkeypatch.setattr(engine, "ram_share", lambda: 0.92)
        engine.police_ram()
        assert tool.poll() is None and a in engine.grants and b in engine.grants
        # Above it the newest heavy process goes first, and only it; a job under HEAVY_GB is never chosen
        monkeypatch.setattr(engine, "ram_share", lambda: 0.96)
        engine.police_ram()
        assert tool.wait(timeout=5) == -9 and a in engine.grants
        assert "was stopped at 0.1 GB because the RAM pool was 96% full" in engine.turn_mem[worker["id"]]["over"][0]
        # While its memory frees, nothing else goes; then the next newest heavy one does
        engine.police_ram()
        assert a in engine.grants
        engine.ram_settle = 0.0
        engine.police_ram()
        assert a not in engine.grants and b in engine.grants
        assert "newest heavy process" in engine.ram_release(worker, a)["text"]
    finally:
        engine.turn_mem.pop(worker["id"], None)
        if tool:
            tool.kill()
            tool.wait()
        for key in [k for k, g in engine.grants.items() if g["sid"] == worker["id"]]:
            engine.ram_release(worker, key)
        for h in helpers:
            h.kill()
            h.wait()


def test_vram_grants_fit_gpus_and_a_full_gpu_stops_its_newest_job(server, monkeypatch):
    _, engine = server
    worker = {"id": "w-vramtest", "kind": "worker"}
    gpus = [{"index": 0, "name": "big", "mem_used_gb": 2.0, "mem_total_gb": 24.0, "util": 0.0, "procs": {}},
            {"index": 1, "name": "small", "mem_used_gb": 0.0, "mem_total_gb": 12.0, "util": 0.0, "procs": {}}]
    snap = {**engine.infra.latest(), "gpus": gpus, "sessions": []}
    snap["gpus_ts"] = snap["ts"]  # a slow real nvidia-smi must not make the fake reading stale

    # The sampling loop sees the same two fake GPUs, so it measures and polices them too
    monkeypatch.setattr(engine.infra, "latest", lambda: snap)
    monkeypatch.setattr(engine.infra, "sample", lambda: snap)
    helpers = [subprocess.Popen(["sleep", "60"]) for _ in range(6)]

    def grant(vram, i):
        engine.ram_admitted = 0.0  # the fake sample never advances
        return asyncio.run(engine.ram_grant(worker, 0, helpers[i].pid, f"job{i}", vram=vram))

    try:
        with pytest.raises(AgentError, match="at most 23.0 GB"):
            grant(30, 0)
        # Best fit: the 10 GB job takes the small GPU, the 15 GB one the big GPU, and only it sees that GPU
        a, b = grant(10, 0), grant(15, 1)
        assert a["env"]["CUDA_VISIBLE_DEVICES"] == "1" and "10 GB VRAM on GPU1" in a["text"]
        assert b["env"] == {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "0", "LOOP_VRAM_GB": "15"}
        # Grants count in full before their jobs load: GPU0 has 23 - 2 - 15 = 6 GB left, GPU1 1 GB
        with pytest.raises(AgentError, match="10 GB VRAM is not free"):
            grant(10, 2)
        d = grant(5, 3)
        assert d["env"]["CUDA_VISIBLE_DEVICES"] == "0"
        # A GPU nearly full stops the newest job with processes on it, whether or not it keeps to its grant
        gpus[0]["procs"] = {helpers[1].pid: 14.0, helpers[3].pid: 7.0}
        gpus[0]["mem_used_gb"] = 23.5
        engine.sample_vram(snap)
        assert gpus[0]["jobs_gb"] == 21.0
        engine.police_vram(snap)
        assert b["grant"] in engine.grants and d["grant"] not in engine.grants
        assert "GPU0 had less than 1 GB free and it was the newest job" in engine.ram_release(worker, d["grant"])["text"]
        # The sampling loop polices the fake GPUs too: each one is given room again once its case is checked
        gpus[0]["procs"], gpus[0]["mem_used_gb"] = {helpers[1].pid: 14.0}, 23.0  # 1 GB free: not policed, no room
        # Without a process list, what the GPU uses above its level without grants is its jobs', and the newest
        # job granted it goes
        engine.vram_settle.clear()
        gpus[1]["mem_used_gb"] = 11.5
        engine.sample_vram(snap)
        assert gpus[1]["jobs_gb"] == 11.5
        engine.police_vram(snap)
        assert a["grant"] not in engine.grants
        assert "GPU1 had less than 1 GB free" in engine.ram_release(worker, a["grant"])["text"]
        gpus[1]["mem_used_gb"] = 0.0
        engine.sample_vram(snap)
        e, f = grant(4, 4), grant(4, 5)
        assert e["env"]["CUDA_VISIBLE_DEVICES"] == f["env"]["CUDA_VISIBLE_DEVICES"] == "1"
        # Where the driver names the processes on a GPU but not their memory (WSL), a newer job not on the GPU yet
        # is passed over
        engine.vram_settle.clear()
        gpus[1]["mem_used_gb"], gpus[1]["pids"] = 11.5, [helpers[4].pid]
        engine.sample_vram(snap)
        engine.police_vram(snap)
        assert e["grant"] not in engine.grants and f["grant"] in engine.grants
        engine.ram_release(worker, e["grant"])
        # While the stopped job's memory frees, the job left on that GPU is not blamed for it
        gpus[1]["pids"] = [helpers[5].pid]
        engine.sample_vram(snap)
        engine.police_vram(snap)
        assert f["grant"] in engine.grants
        gpus[1]["mem_used_gb"] = 0.0
        # Memory outside the jobs filling a GPU stops nothing
        engine.vram_settle.clear()
        gpus[0]["procs"], gpus[0]["mem_used_gb"] = {1: 22.0}, 23.5
        engine.sample_vram(snap)
        engine.police_vram(snap)
        assert b["grant"] in engine.grants
    finally:
        for key in [k for k, g in engine.grants.items() if g["sid"] == worker["id"]]:
            engine.ram_release(worker, key)
        for h in helpers:
            h.kill()
            h.wait()
    assert not engine.grants
