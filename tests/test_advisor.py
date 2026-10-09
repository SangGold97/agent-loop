"""Advisor consultations share the goal inbox and budget, but never a writable project or a resumed session."""
import json
from agentloop import agents, guard
from conftest import wait_for


def advice_goal(server, name, spec="ADVICE", max_meta=10, advisor="fake"):
    client, engine = server
    assert client.post("/api/workspaces", json={"name": name}).status_code == 200
    role = {"agent": "fake", "model": None, "effort": None}
    cfg = {"meta_work": role, "meta_advisor": {**role, "agent": advisor},
           "workers": {"fake": {"enabled": True}}, "language": "Vietnamese",
           "budget": {"max_meta_turns": max_meta, "max_worker_turns": 10}}
    response = client.post(f"/api/workspaces/{name}/goals", json={
        "title": "Advice", "spec": spec, "goal": "Consider the advice and report.", "config": cfg})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def finished(client, ws, gid):
    return wait_for(lambda: (v := client.get(f"/api/goals/{ws}/{gid}").json())["row"]["status"]
                    == "awaiting_review" and v)


def test_advice_round_trip_is_fresh_readonly_and_accounted(server):
    client, engine = server
    gid = advice_goal(server, "advice-full")
    assert client.post(f"/api/goals/advice-full/{gid}/start").status_code == 200
    view = finished(client, "advice-full", gid)
    # The final meta-work turn may still be committing its journal after closing the goal.
    wait_for(lambda: engine.db.goal("advice-full", gid)["meta_turns"] == 5)
    sessions = engine.db.q("SELECT * FROM sessions WHERE ws='advice-full' AND kind='meta-advisor'")
    assert len(sessions) == 2 and len({s["vendor_id"] for s in sessions}) == 2
    assert all(s["turns"] == 1 and s["status"] == "idle" for s in sessions)
    assert view["row"]["outcome"] == "success" and view["budget"]["worker_turns"] == 0
    assert view["config"]["meta_advisor"]["agent"] == "fake"
    for s in sessions:
        turn = engine.db.one("SELECT * FROM turns WHERE session=?", s["id"])
        assert turn["final"] == "Consider a different validation split."
        assert engine.build_spec(s, "question", 1).resume_id is None
        assert client.post(f"/api/meta-chats/{s['id']}/messages", json={"text": "hello"}).status_code == 400
        events = client.get(f"/api/sessions/{s['id']}/events").json()["events"]
        assert any("MODE: META-ADVISOR" in e.get("prompt", "") for e in events)
    inbox = engine.db.q("SELECT * FROM inbox WHERE ws='advice-full' AND kind IN ('advice', 'advice_request')")
    assert len(inbox) == 4 and all(i["consumed"] == 1 for i in inbox)
    assert all("[Advice]" in json.loads(i["payload"])["text"] for i in inbox if i["kind"] == "advice")
    assert engine.db.goal("advice-full", gid)["tokens"] == 75
    assert not (engine.wsp("advice-full") / "repo/advisor.txt").exists()


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


def test_advisor_change_to_project_is_reported(server, monkeypatch):
    # Like a meta-chat, the advisor only reads the project; a write the guard missed is reported, not reverted
    client, engine = server
    gid = advice_goal(server, "advice-write", spec="ADVICE_WRITE")
    alerts, publish = [], engine.publish
    monkeypatch.setattr(engine, "publish", lambda kind, **d: (alerts.append(d) if kind == "alert" else None,
                                                               publish(kind, **d)))
    client.post(f"/api/goals/advice-write/{gid}/start")
    finished(client, "advice-write", gid)
    assert [a for a in alerts if a.get("text", "").startswith("Advisor ") and "integration worktree" in a["text"]]
    assert (engine.wsp("advice-write") / "wt" / f"_int-{gid}" / "advisor.txt").exists()


def test_advisor_guard_and_configuration(tmp_path, monkeypatch):
    env = {"LOOP_WS": str(tmp_path), "LOOP_ROLE": "meta-advisor"}
    assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/anything"}}, env)
    assert guard.check({"tool_name": "apply_patch", "tool_input": {}}, env)
    for cmd in ("echo bad > plan.md", "git commit -am bad", "cp plan.md journal.md"):
        assert guard.check({"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": cmd}}, env)
    assert not guard.check({"tool_name": "Bash", "tool_input": {"command": "cat spec.md; git diff"}}, env)
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
    monkeypatch.setattr(engine, "kick", lambda *_: None)
    for question in ("queued question", "interrupted question", "completed question"):
        engine.advise(meta, question)
    queued, interrupted, complete = engine.advisors("advice-recovery", gid)
    engine.db.update("sessions", {"id": interrupted["id"]}, status="interrupted", turns=1)
    engine.db.update("sessions", {"id": complete["id"]}, status="idle", turns=1)
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
    client.post(f"/api/goals/advice-recovery/{gid}/stop")
    assert engine.db.session(queued["id"])["status"] == "stopped"
    assert not engine.advisors("advice-recovery", gid)
