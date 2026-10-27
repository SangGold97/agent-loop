"""Advisor consultations share the goal inbox and budget and build only in their own worktree; only a follow-up
resumes one."""
import json
import pytest
import yaml
from agentloop.engine import AgentError
from agentloop import agents, engine as engine_mod, gitops, guard
from conftest import wait_for


def advice_goal(server, name, spec="ADVICE", max_meta=10, advisor="fake", autocompact=None):
    client, engine = server
    assert client.post("/api/workspaces", json={"name": name}).status_code == 200
    role = {"agent": "fake", "model": None, "effort": None}
    cfg = {"meta_work": role, "meta_advisor": {**role, "agent": advisor, "autocompact": autocompact},
           "workers": {"fake": {"enabled": True}}, "language": "Vietnamese",
           "budget": {"max_meta_turns": max_meta, "max_worker_turns": 10}}
    response = client.post(f"/api/workspaces/{name}/goals", json={
        "title": "Advice", "spec": spec, "goal": "Consider the advice and report.", "config": cfg})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def finished(client, ws, gid):
    return wait_for(lambda: (v := client.get(f"/api/goals/{ws}/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)


def test_advice_round_trip_is_fresh_and_accounted(server):
    client, engine = server
    gid = advice_goal(server, "advice-full", autocompact="300k")
    assert client.post(f"/api/goals/advice-full/{gid}/start").status_code == 200
    view = finished(client, "advice-full", gid)
    # The final meta-work turn may still be committing its files after closing the goal.
    wait_for(lambda: engine.db.goal("advice-full", gid)["meta_turns"] == 5)
    sessions = engine.db.q("SELECT * FROM sessions WHERE ws='advice-full' AND kind='meta-advisor'")
    assert len(sessions) == 2 and len({s["vendor_id"] for s in sessions}) == 2
    assert all(s["turns"] == 1 and s["status"] == "idle" for s in sessions)
    assert all(engine.build_spec(s, "q", 1).autocompact == "300k" for s in sessions)  # from goal.yaml
    assert view["row"]["outcome"] == "success" and view["budget"]["worker_turns"] == 0
    assert view["config"]["meta_advisor"]["agent"] == "fake"
    for s in sessions:
        turn = engine.db.one("SELECT * FROM turns WHERE session=?", s["id"])
        assert turn["final"] == "Consider a different validation split."
        assert client.post(f"/api/meta-chats/{s['id']}/messages", json={"text": "hello"}).status_code == 400
        events = client.get(f"/api/sessions/{s['id']}/events").json()["events"]
        # The mode text reaches the CLI once, as system text, not again in the message
        assert "MODE: META-ADVISOR" in engine.build_spec(s, "question", 1).system_text
        assert not any("MODE:" in e.get("prompt", "") for e in events)
    inbox = engine.db.q("SELECT * FROM inbox WHERE ws='advice-full' AND kind IN ('advice', 'advice_request')")
    assert len(inbox) == 4 and all(i["consumed"] == 1 for i in inbox)
    assert all("[Advice]" in json.loads(i["payload"])["text"] for i in inbox if i["kind"] == "advice")
    advice = client.get(f"/api/goals/advice-full/{gid}").json()["advice"]  # the PLAN & ADVICE tab
    text = "Consider a different validation split."
    assert advice == {s["id"]: [{"turn": 1, "status": "completed", "text": text}] for s in sessions}
    assert engine.db.goal("advice-full", gid)["tokens"] == 75
    assert not (engine.wsp("advice-full") / "repo/advisor.txt").exists()


def test_follow_up_resumes_the_same_consultation(server):
    client, engine = server
    gid = advice_goal(server, "advice-follow", spec="ADVICE_FOLLOW")
    client.post(f"/api/goals/advice-follow/{gid}/start")
    finished(client, "advice-follow", gid)
    wait_for(lambda: engine.db.goal("advice-follow", gid)["meta_turns"] == 5)
    [s] = engine.db.q("SELECT * FROM sessions WHERE ws='advice-follow' AND kind='meta-advisor'")
    assert s["turns"] == 2 and s["status"] == "idle"
    assert engine.build_spec(s, "q", 3).resume_id == s["vendor_id"]
    texts = [json.loads(i["payload"])["text"] for i in engine.db.q(
        "SELECT payload FROM inbox WHERE ws='advice-follow' AND kind='advice' ORDER BY id")]
    assert texts[0].startswith(f"[Advice] {s['id']} · completed\n")
    assert texts[1] == f"[Advice] {s['id']} · turn 2 · completed\nKeep the new split."
    advice = client.get(f"/api/goals/advice-follow/{gid}").json()["advice"][s["id"]]
    assert [a["turn"] for a in advice] == [1, 2]


def test_advisor_budget_waits_for_the_answer_before_final_turn(server):
    client, engine = server
    gid = advice_goal(server, "advice-budget", max_meta=2)
    client.post(f"/api/goals/advice-budget/{gid}/start")
    view = finished(client, "advice-budget", gid)
    wait_for(lambda: engine.db.goal("advice-budget", gid)["meta_turns"] == 3)
    advisors = [s for s in view["sessions"] if s["kind"] == "meta-advisor"]
    assert len(advisors) == 1 and advisors[0]["status"] == "idle"
    assert view["row"]["outcome"] == "success"
    # One work turn + one advice turn + the existing final budget turn; no second advisor was admitted.
    assert engine.db.one("SELECT COUNT(*) c FROM inbox WHERE ws='advice-budget' AND kind='advice'")["c"] == 1


def test_failed_advisor_reports_and_wakes_meta_work(server):
    client, engine = server
    gid = advice_goal(server, "advice-fail", spec="ADVICE_FAIL")
    client.post(f"/api/goals/advice-fail/{gid}/start")
    finished(client, "advice-fail", gid)
    replies = engine.db.q("SELECT payload FROM inbox WHERE ws='advice-fail' AND kind='advice'")
    assert len(replies) == 2 and all("failed on purpose" in r["payload"] for r in replies)
    assert not engine.advisors("advice-fail", gid)


def test_stopping_goal_stops_advisor_without_resuming_it(server):
    client, engine = server
    gid = advice_goal(server, "advice-stop", spec="ADVICE_STOP")
    client.post(f"/api/goals/advice-stop/{gid}/start")
    s = wait_for(lambda: engine.db.one("SELECT * FROM sessions WHERE ws='advice-stop' "
                                      "AND kind='meta-advisor' AND status='running'"))
    wait_for(lambda: engine.runner.running(s["id"]))
    client.post(f"/api/goals/advice-stop/{gid}/stop")
    wait_for(lambda: engine.db.session(s["id"])["status"] == "stopped")
    assert engine.db.goal("advice-stop", gid)["status"] == "stopped"
    wait_for(lambda: engine.db.one("SELECT id FROM inbox WHERE ws='advice-stop' AND kind='advice'"))
    assert not engine.runner.running(s["id"])


def test_advisor_quota_is_refunded_and_reported(server, monkeypatch):
    from agentloop.adapters import ADAPTERS
    client, engine = server
    monkeypatch.setitem(ADAPTERS, "fake-advice", ADAPTERS["fake"])
    gid = advice_goal(server, "advice-quota", spec="ADVICE_LIMIT", advisor="fake-advice")
    client.post(f"/api/goals/advice-quota/{gid}/start")
    try:
        wait_for(lambda: engine.db.one("SELECT id FROM inbox WHERE ws='advice-quota' AND kind='advice'"))
        reply = engine.db.one("SELECT payload FROM inbox WHERE ws='advice-quota' AND kind='advice'")
        assert "not counted" in reply["payload"] and engine.limit_until("fake-advice")
        assert len(engine.db.q("SELECT id FROM sessions WHERE ws='advice-quota' AND kind='meta-advisor'")) == 1
        goal = engine.db.goal("advice-quota", gid)
        assert goal["meta_turns"] <= 2
    finally:
        client.post(f"/api/goals/advice-quota/{gid}/stop")
        engine.vendor_limits.pop("fake-advice", None)


def test_advisor_cut_off_by_usage_limit_resumes_at_reset(server, monkeypatch):
    client, engine = server
    monkeypatch.setattr(engine_mod, "LIMIT_SLACK_S", 0)
    gid = advice_goal(server, "advice-resume", spec="ADVICE_LIMIT_SOON")
    client.post(f"/api/goals/advice-resume/{gid}/start")
    # The same consultation gets its question again at the reset, and its advice arrives as turn 2
    wait_for(lambda: engine.db.one("SELECT id FROM inbox WHERE ws='advice-resume' AND kind='advice' "
                                   "AND json_extract(payload, '$.turn')=2 AND payload LIKE '%completed%'"))
    s = engine.db.q("SELECT * FROM sessions WHERE ws='advice-resume' AND kind='meta-advisor' ORDER BY created")[0]
    turn = engine.db.one("SELECT * FROM turns WHERE session=? AND n=2", s["id"])
    assert turn["status"] == "idle" and s["resume_at"] is None
    events = client.get(f"/api/sessions/{s['id']}/events").json()["events"]
    prompt = [e["prompt"] for e in events if e.get("type") == "turn" and e.get("n") == 2][0]
    assert "cut off by the fake usage limit" in prompt and "Question 1" in prompt
    client.post(f"/api/goals/advice-resume/{gid}/stop")


def test_advisor_builds_in_its_worktree_and_resumes_with_its_job(server, monkeypatch):
    client, engine = server
    monkeypatch.setattr(engine_mod, "JOB_WAIT_S", 1)  # `loop run` leaves its job to the background at once
    gid = advice_goal(server, "advice-build", spec="ADVICE_BUILD")
    client.post(f"/api/goals/advice-build/{gid}/start")
    finished(client, "advice-build", gid)
    [s] = engine.db.q("SELECT * FROM sessions WHERE ws='advice-build' AND kind='meta-advisor'")
    p = engine.wsp("advice-build")
    assert s["turns"] == 2 and s["branch"] == f"loop/{gid}/advisor-{s['id']}" and s["cwd"] == str(p / "meta")
    texts = [json.loads(i["payload"])["text"] for i in engine.db.q(
        "SELECT payload FROM inbox WHERE ws='advice-build' AND kind='advice' ORDER BY id")]
    assert "is still running" in texts[0] and f"`loop merge {s['id']}`" in texts[0]
    assert texts[1].startswith(f"[Advice] {s['id']} · turn 2 · completed\nPrototype scored 42; merge it.")
    # The prototype was snapshotted on the advisor's branch and merged by the meta-work
    assert (p / "wt" / gid / "proto.py").exists() and engine.db.session(s["id"])["merged"] == 1
    assert client.post(f"/api/goals/advice-build/{gid}/accept").status_code == 200
    assert not (p / "wt" / s["id"]).exists() and not gitops.branch_exists(p / "repo", s["branch"])


def test_advisor_change_to_project_is_reported(server, monkeypatch):
    # The advisor writes only in its own worktree; a write the guard missed is reported, not reverted.
    # It runs alongside the meta-work turn that asked, so only repo/ is watched (see Engine.watch_project)
    client, engine = server
    gid = advice_goal(server, "advice-write", spec="ADVICE_WRITE")
    alerts, publish = [], engine.publish
    monkeypatch.setattr(engine, "publish", lambda kind, **d: (alerts.append(d) if kind == "alert" else None,
                                                               publish(kind, **d)))
    client.post(f"/api/goals/advice-write/{gid}/start")
    finished(client, "advice-write", gid)
    assert [a for a in alerts if a.get("text", "").startswith("Advisor ") and "repo/" in a["text"]]
    assert (engine.wsp("advice-write") / "repo" / "advisor.txt").exists()


def test_advisor_guard_and_configuration(tmp_path, monkeypatch):
    env = {"LOOP_WS": str(tmp_path), "LOOP_ROLE": "meta-advisor", "LOOP_SESSION": "ma-1", "LOOP_GOAL": "g"}
    assert not guard.check({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/anything"}}, env)
    assert guard.check({"tool_name": "apply_patch", "tool_input": {}}, env)
    # Its own worktree and data/ are writable; the meta-work's integration worktree is not
    own = tmp_path / "wt" / "ma-1"
    assert not guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{own}/proto.py"}}, env)
    assert not guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/data/x.csv"}}, env)
    assert not guard.check({"tool_name": "Bash", "cwd": str(own), "tool_input": {"command": "git commit -am x"}}, env)
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/wt/g/x.py"}}, env)
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/meta/memory/x.md"}}, env)
    for cmd in ("echo bad > plan.md", "git commit -am bad", "cp spec.md plan.md"):
        assert guard.check({"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": cmd}}, env)
    assert not guard.check({"tool_name": "Bash", "tool_input": {"command": "cat spec.md; git diff"}}, env)
    # The turn's own TMPDIR is writable too, for scratch files and verification scripts
    scratch = tmp_path / "scratch"
    env["TMPDIR"] = str(scratch)
    assert not guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{scratch}/check.py"}}, env)
    assert not guard.check({"tool_name": "Bash", "cwd": str(tmp_path),
                            "tool_input": {"command": f"echo x > {scratch}/out.txt"}}, env)
    patch = f"*** Begin Patch\n*** Add File: {scratch}/a.py\n+x\n*** End Patch"
    assert not guard.check({"tool_name": "apply_patch", "tool_input": {"input": patch}}, env)
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/repo/x.py"}}, env)
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": f"{scratch}/../x.py"}}, env)
    assert guard.check({"tool_name": "apply_patch", "tool_input": {}}, env)
    monkeypatch.setattr(agents, "load_models", lambda: {"claude": {"version": "test", "models": [
        {"id": "opus", "efforts": ["high"]}]}})
    assert agents.config_errors({"meta_advisor": {"agent": "claude", "model": "opus", "effort": "high"}}) == []
    assert agents.config_errors({"meta_advisor": {"agent": "claude", "model": "opus", "effort": "invalid"}})
    assert agents.config_errors({"meta_advisor": {"agent": "unknown"}})


def test_advisor_recovery_delivers_saved_results_once_and_keeps_queued_requests(server, monkeypatch):
    import time
    client, engine = server
    gid = advice_goal(server, "advice-recovery")
    conf = {"agent": "fake", "model": None, "effort": None}
    meta = engine.new_session("advice-recovery", gid, "meta-work", "meta-work", conf,
                              engine.wsp("advice-recovery") / "meta")
    engine.set_goal("advice-recovery", gid, status="running", meta_work=meta["id"])
    monkeypatch.setattr(engine, "dispatch_advice", lambda *_: None)  # as if the server restarted before dispatch
    for question in ("queued question", "interrupted question", "completed question"):
        engine.advise(meta, question)
    queued, interrupted, complete = engine.advisors("advice-recovery", gid)
    engine.db.update("sessions", {"id": interrupted["id"]}, status="interrupted", turns=1)
    engine.db.update("sessions", {"id": complete["id"]}, status="idle", turns=1)
    for s in (interrupted, complete):  # these two were dispatched before the restart
        engine.db.x("UPDATE inbox SET consumed=1 WHERE kind='advice_request' AND json_extract(payload, '$.sid')=?",
                    s["id"])
    answer = "Saved advice " * 2000
    engine.db.x("INSERT INTO turns(session, n, ended, status, final) VALUES(?,1,?,'idle',?)",
                complete["id"], time.time(), answer)
    engine.recover_advice()
    engine.recover_advice()
    replies = engine.db.q("SELECT payload FROM inbox WHERE ws='advice-recovery' AND kind='advice'")
    assert len(replies) == 2
    assert any(answer in json.loads(r["payload"])["text"] for r in replies)
    assert any("interrupted" in json.loads(r["payload"])["text"] for r in replies)
    pending = engine.db.q("SELECT payload FROM inbox WHERE ws='advice-recovery' "
                          "AND kind='advice_request' AND consumed=0")
    assert len(pending) == 1 and json.loads(pending[0]["payload"])["sid"] == queued["id"]
    # A follow-up needs a finished consultation of this goal with a conversation to resume
    for sid, error in ((queued["id"], "in progress"), (complete["id"], "no conversation"), ("ma-none", "no consult")):
        with pytest.raises(AgentError, match=error):
            engine.advise(meta, "follow-up question", sid)
    engine.db.update("sessions", {"id": complete["id"]}, vendor_id="vendor-1")
    assert "Follow-up" in engine.advise(meta, "follow-up question", complete["id"])
    pending = engine.db.q("SELECT payload FROM inbox WHERE ws='advice-recovery' "
                          "AND kind='advice_request' AND consumed=0")
    assert json.loads(pending[-1]["payload"]) == {"sid": complete["id"], "turn": 2, "question": "follow-up question"}
    # Once the human moves the advisor to another CLI, its earlier consultations cannot be followed up
    engine.db.update("sessions", {"id": complete["id"]}, status="idle")
    goal_yaml = engine.gdir("advice-recovery", gid) / "goal.yaml"
    cfg = yaml.safe_load(goal_yaml.read_text())
    cfg["meta_advisor"]["agent"] = "claude"
    goal_yaml.write_text(yaml.safe_dump(cfg))
    with pytest.raises(AgentError, match="the advisor is now claude"):
        engine.advise(meta, "another follow-up", complete["id"])
    client.post(f"/api/goals/advice-recovery/{gid}/stop")
    assert engine.db.session(queued["id"])["status"] == "stopped"
    assert not engine.advisors("advice-recovery", gid)
