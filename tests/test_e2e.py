"""End-to-end with the scripted fake agent: workspace -> goal -> meta-work -> worker -> merge -> done."""
import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from agentloop import workspace
from agentloop.engine import AgentError
from agentloop.runner import scratch_root, systemd_available

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
    row = client.get(f"/api/goals/quota/{gid}").json()["row"]
    assert row["meta_turns"] == 2 and row["fail_streak"] == 0


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
    chat = client.post(f"/api/goals/chatw/{gid}/meta-chats", json={}).json()["id"]
    client.post(f"/api/meta-chats/{chat}/messages", json={"text": "CHATWRITE please"})
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


def test_loop_run_grants_ram_and_stops_a_job_over_its_grant(server):
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
    adj, ok, over, spawn, orphan = out
    assert "granted 0.5 GB" in ok and "job ran, adj 0" in ok and "done (exit 0)" in ok
    assert "workers can only use" in spawn
    assert not engine.grants and not engine.ram_waiting
    if systemd_available():  # the pool, the job scopes and the OOM order need systemd
        assert adj == "0 1000"
        assert "outgrowing its 0.1 GB grant" in over and orphan == "job stopped"
        digest = engine.db.one("SELECT payload FROM inbox WHERE ws='ram' AND kind='worker_done'")["payload"]
        assert "Memory in this worker's turn" in digest and "outgrowing its 0.1 GB grant" in digest


def test_goal_refused_for_ram_waits_instead_of_pausing(server, monkeypatch):
    client, engine = server
    client.post("/api/workspaces", json={"name": "ramwait"})
    monkeypatch.setattr(engine, "ram_free", lambda: 0.0)  # another goal holds the pool
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
    # RAM is back: one event wakes the meta-work, whose next turn (here) closes the goal
    wait_for(lambda: client.get(f"/api/goals/ramwait/{gid}").json()["row"]["status"] == "awaiting_review")
    assert engine.db.one("SELECT 1 FROM inbox WHERE ws='ramwait' AND payload LIKE '%RAM is free again%'")
    assert ("ramwait", gid) not in engine.ram_wait_goals


def test_ram_grant_counts_grants_in_full(server):
    _, engine = server
    worker = {"id": "w-ramtest", "kind": "worker"}
    with pytest.raises(AgentError, match="belongs to workers"):
        asyncio.run(engine.ram_grant({"id": "mc-x", "kind": "meta-chat"}, 1, 0, os.getpid(), "x"))
    with pytest.raises(AgentError, match="at most"):
        asyncio.run(engine.ram_grant(worker, engine.pool_gb() + 1, 0, os.getpid(), "x"))
    free = engine.ram_free()
    first = asyncio.run(engine.ram_grant(worker, round(free * 0.6, 1), 0, os.getpid(), "python big.py"))
    # The first grant still counts in full although its job uses nothing yet, so a second one cannot see that RAM
    with pytest.raises(AgentError, match="not free") as refused:
        asyncio.run(engine.ram_grant(worker, round(free * 0.6, 1), 0, os.getpid(), "python other.py"))
    assert "w-ramtest" in str(refused.value) and "python big.py" in str(refused.value)
    engine.ram_release(worker, first["grant"])
    assert not engine.grants
    # A grant whose `loop run` died without releasing it is dropped by the sampling loop
    dead = subprocess.Popen(["true"])
    dead.wait()
    asyncio.run(engine.ram_grant(worker, 0.1, 0, dead.pid, "true"))
    wait_for(lambda: not engine.grants, timeout=10)
