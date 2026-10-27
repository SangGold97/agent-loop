import io
import json
import os
import subprocess
import time
from collections import deque, namedtuple
from datetime import datetime, timezone
from pathlib import Path

from agentloop import agents, digest, guard, infra
from agentloop.adapters.agy import AgyAdapter
from agentloop.adapters.base import RunSpec, ParseState, to_tokens, usage_limit_until
from agentloop.adapters.claude import ClaudeAdapter
from agentloop.adapters.codex import CodexAdapter
from agentloop.config import deep_merge


def test_deep_merge():
    out = deep_merge({"workers": {"claude": {"model": "a", "effort": "high"}}, "x": 1},
                     {"workers": {"claude": {"model": "b"}}})
    assert out == {"workers": {"claude": {"model": "b", "effort": "high"}}, "x": 1}


def test_to_tokens():
    assert to_tokens("400k") == 400_000 and to_tokens("1M") == 1_000_000 and to_tokens(None) is None


def test_claude_argv_worker(tmp_path):
    s = RunSpec(agent="claude", kind="worker", cwd=tmp_path, prompt="x", model="opus", effort="high",
                autocompact="300k", resume_id="abc", plugin_dir=tmp_path / "p", append_file=tmp_path / "A.md",
                settings_file=tmp_path / "h.json", add_dirs=[tmp_path / "m"])
    a = ClaudeAdapter().argv(s)
    assert a[:2] == ["claude", "-p"] and "--dangerously-skip-permissions" in a
    assert a[a.index("--resume") + 1] == "abc" and a[a.index("--autocompact") + 1] == "300000"
    assert "--append-system-prompt-file" in a and "--append-system-prompt" not in a


def test_codex_argv_meta_resume(tmp_path):
    s = RunSpec(agent="codex", kind="meta", cwd=tmp_path, prompt="x", model="m", effort="max", resume_id="t1",
                system_text="MODE", last_message_file=tmp_path / "l.txt")
    a = CodexAdapter().argv(s)
    assert a[:4] == ["codex", "exec", "resume", "t1"] and a[-1] == "-"
    assert 'model_reasoning_effort="max"' in a and 'model_reasoning_summary="auto"' in a and "-C" not in a
    assert "--dangerously-bypass-approvals-and-sandbox" in a and not any("sandbox_mode" in x for x in a)
    assert "project_root_markers=[]" in a
    assert "project_root_markers=[]" not in CodexAdapter().argv(RunSpec(agent="codex", kind="worker", cwd=tmp_path,
                                                                        prompt="x"))


def test_codex_parse_stream():
    st, ad = ParseState(), CodexAdapter()
    lines = [{"type": "thread.started", "thread_id": "T"},
             {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
             {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "a", "exit_code": 0}},
             {"type": "item.completed", "item": {"type": "reasoning", "text": "**Checking output**\n\nIt lists a."}},
             {"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}},
             {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}}]
    events = [e for obj in lines for e in ad.parse(obj, st)]
    assert st.vendor_id == "T" and st.last_text == "DONE" and st.tokens == 45
    assert [e["type"] for e in events] == ["init", "tool", "tool_result", "thinking", "text", "usage"]
    assert events[3]["text"] == "Checking output\n\nIt lists a."



def test_agy_argv_and_stdin(tmp_path):
    s = RunSpec(agent="agy", kind="meta", cwd=tmp_path, prompt="[Kickoff] go", model="gemini-4-argon",
                effort="high", autocompact="300k", resume_id="c1", system_text="MODE: META-WORK")
    a = AgyAdapter().argv(s)
    assert a[:5] == ["agy", "--output-format", "stream-json", "--input-format", "stream-json"]
    assert a[a.index("--model") + 1] == "gemini-4-argon" and a[a.index("--effort") + 1] == "high"
    assert a[a.index("--conversation") + 1] == "c1" and "--dangerously-skip-permissions" in a and "300000" not in a
    msg = json.loads(AgyAdapter().stdin(s))
    assert msg["event"] == "user" and msg["message"]["content"].startswith("<instructions>\nMODE: META-WORK\n")
    assert msg["message"]["content"].endswith("\n\n[Kickoff] go")
    s.system_text = None
    assert json.loads(AgyAdapter().stdin(s))["message"]["content"] == "[Kickoff] go"


def test_agy_parse_stream():
    st, ad = ParseState(), AgyAdapter()
    usage = lambda i, o: {"input_tokens": i, "output_tokens": o, "cache_read_tokens": 40, "total_tokens": i + o}
    step = lambda n, state, kind, **kw: {"event": "step_update", "step_update": {
        "conversation_id": "C", "step_index": n, "state": state, "step_type": kind, **kw}}
    call = {"name": "run_command", "parameters": {"CommandLine": "ls", "Cwd": "/w"}}
    lines = [{"event": "init", "conversation_id": "C", "init": {"model": "gemini-4-argon-high"}},
             step(0, "DONE", "user_input"),
             step(1, "DONE", "agent_response", usage=usage(100, 5)),
             step(2, "ACTIVE", "tool", tool_name="run_command", tool_info=call),
             step(2, "ACTIVE", "tool", tool_name="run_command", tool_info=call),
             step(2, "DONE", "tool", tool_name="run_command", tool_info={**call, "output": "a.txt"}),
             step(3, "ACTIVE", "tool", tool_name="write_to_file", tool_info={"parameters": {"TargetFile": "/x"}}),
             step(3, "ERROR", "tool", tool_name="write_to_file", tool_info={
                 "parameters": {"TargetFile": "/x"}, "error": {"type": "TOOL_ERROR", "message": "denied"}}),
             step(4, "ACTIVE", "agent_response", text_delta="DO"),
             step(4, "DONE", "agent_response", text_delta="NE\n", usage=usage(200, 10)),
             {"event": "result", "result": {"conversation_id": "C", "status": "SUCCESS", "response": "DONE\n",
                                            "num_turns": 3, "usage": usage(9000, 99)}}]
    events = [e for obj in lines for e in ad.parse(obj, st)]
    assert [e["type"] for e in events] == ["init", "usage", "tool", "tool_result", "tool", "tool_result", "delta",
                                           "text", "usage"]
    assert events[2]["detail"] == "ls" and events[5]["error"] and events[5]["text"] == "denied"
    # This turn's own calls count, not the conversation total in `result`; context is the last call's input
    assert st.vendor_id == "C" and st.final == "DONE" and st.tokens == 315 and st.context == 240
    assert not st.is_error


def test_claude_usage_limit_reads_the_error_code_and_reset():
    # As Claude Code 2.1 refuses a turn: a rejected rate-limit event, then an error result with its code
    st, reset = ParseState(), 1792404000
    ad = ClaudeAdapter()
    ad.parse({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": reset,
                                                              "rateLimitType": "five_hour"}}, st)
    ad.parse({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
              "api_error": "usage_limit_reached", "result": "Some new wording · try later"}, st)
    assert st.is_error and usage_limit_until(f"{st.error}\n{st.final}") == float(reset)


def test_agy_failed_result():
    st = ParseState()
    out = AgyAdapter().parse({"event": "result", "result": {"conversation_id": "", "status": "ERROR", "response": "",
                                                            "error": "You have exhausted your quota on this model."}}, st)
    assert st.is_error and out[0]["type"] == "error" and usage_limit_until(st.error)


def test_codex_ignores_hook_trust_notice():
    notice = {"type": "item.completed", "item": {"type": "error", "message":
              "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation."}}
    assert CodexAdapter().parse(notice, ParseState()) == []

def test_claude_parse_result():
    st, ad = ParseState(), ClaudeAdapter()
    ad.parse({"type": "system", "subtype": "init", "session_id": "S"}, st)
    ad.parse({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                              "input": {"command": "ls"}}]}}, st)
    ad.parse({"type": "result", "subtype": "success", "result": "ok", "session_id": "S", "total_cost_usd": 0.5,
              "usage": {"input_tokens": 1, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 90, "output_tokens": 2}}, st)
    assert (st.vendor_id, st.final, st.tokens, st.is_error) == ("S", "ok", 7, False)
    assert not hasattr(st, "cost")



def test_claude_quota_from_rate_limit_event():
    st = ParseState()
    ClaudeAdapter().parse({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.06, "resetsAt": 1792314000},
        "seven_day_opus": {"utilization": 0.5, "resetsAt": 1792886400}}}}, st)
    assert st.quota == [{"name": "5h", "used": 0.06, "resets_at": 1792314000},
                        {"name": "7d opus", "used": 0.5, "resets_at": 1792886400}]


def test_claude_quota_from_usage_command():
    text = ("You are currently using your subscription to power your Claude Code usage\n\n"
            "Current session: 26% used · resets Oct 23, 5:20pm (Asia/Bangkok)\n"
            "Current week (all models): 39% used · resets Oct 25, 7am (Asia/Bangkok)\n"
            "Current week (Fable): 7% used\n")
    now = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc).timestamp()
    assert agents.claude_usage(text, now) == [
        {"name": "5h", "used": 0.26, "resets_at": datetime(2026, 10, 23, 10, 20, tzinfo=timezone.utc).timestamp()},
        {"name": "7d", "used": 0.39, "resets_at": datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc).timestamp()},
        {"name": "7d fable", "used": 0.07, "resets_at": None}]


def test_agy_quota_from_usage_command(tmp_path, monkeypatch):
    data = {"groups": [{"name": "Claude and GPT models", "buckets": [
        {"window": "weekly", "remaining_fraction": 0.25, "reset_time": "2026-10-25T08:25:12Z"},
        {"window": "5h", "reset_time": "2026-10-18T13:25:12Z"}]}]}  # nothing left: the field is omitted
    out = json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": data}})
    calls = []
    monkeypatch.setattr("agentloop.adapters.agy.subprocess.run",
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, out, ""))
    st = ParseState()
    AgyAdapter().finish(st, RunSpec(agent="agy", kind="worker", cwd=tmp_path, prompt="x"))
    assert calls == [["agy", "-p", "/usage", "--output-format", "json"]]
    assert [(w["name"], w["used"]) for w in st.quota] == [("Claude and GPT models weekly", 0.75),
                                                          ("Claude and GPT models 5h", 1.0)]
    assert st.quota[1]["resets_at"] == datetime(2026, 10, 18, 13, 25, 12, tzinfo=timezone.utc).timestamp()


def test_claude_stream_deltas():
    st, ad = ParseState(), ClaudeAdapter()
    delta = lambda d, parent=None: {"type": "stream_event", "parent_tool_use_id": parent,
                                    "event": {"type": "content_block_delta", "delta": d}}
    assert ad.parse(delta({"type": "text_delta", "text": "One"}), st) == [{"type": "delta", "text": "One"}]
    assert ad.parse(delta({"type": "thinking_delta", "thinking": ""}), st) == []
    assert ad.parse(delta({"type": "text_delta", "text": "x"}, parent="t1"), st) == []
    assert "--include-partial-messages" in ad.argv(RunSpec(agent="claude", kind="worker", cwd=Path("."), prompt="x"))

def test_verify_and_checklist():
    goal = "- tests pass\n- verify: `make test`\nverify: python -c 'print(1)'\n"
    assert digest.verify_commands(goal) == ["make test", "python -c 'print(1)'"]
    assert digest.checklist("- [x] a\n- [ ] b\n* [X] c") == (2, 3)


def test_envelope_carries_events_and_budget_only_when_low():
    items = [{"kind": "worker_done", "payload": json.dumps({"text": "[Worker w-1] done"})}]
    b = {"max_worker_turns": 10, "worker_turns": 7, "max_meta_turns": 0, "meta_turns": 50}
    assert not digest.budget_low(b)
    assert digest.envelope(items, None) == "[Worker w-1] done"
    assert digest.budget_low({**b, "worker_turns": 8})
    assert digest.envelope([], "[Budget] x") == "[Budget] x"
    assert digest.envelope([]) == "[System] No new events."


def test_worker_digest_is_the_reply_as_is():
    from types import SimpleNamespace
    sess = {"id": "w-1", "role": "reviewer"}
    reply = "**Result:** approved\n" + "word " * 2000
    res = SimpleNamespace(stopped=False, error="", final=reply, tokens=88_000, context=23_000)
    # One line naming the worker and outcome, then the whole reply: no diff, trimming, tokens or cost
    assert digest.worker_digest(sess, 1, res) == "[Worker w-1 · role reviewer · turn 1] ok\n" + reply


def test_db_drops_cost_columns(tmp_path):
    import sqlite3
    from agentloop.db import DB
    old = sqlite3.connect(tmp_path / "s.db")
    old.execute("CREATE TABLE turns(id INTEGER PRIMARY KEY, session TEXT, n INT, started REAL, ended REAL, status TEXT, "
                "start_commit TEXT, end_commit TEXT, exit_code INT, tokens INT, cost REAL, final TEXT)")
    old.execute("INSERT INTO turns(session, n, tokens, cost) VALUES('w-1', 1, 7, 0.5)")
    old.commit()
    old.close()
    db = DB(tmp_path / "s.db")
    assert db.q("SELECT * FROM turns") == [{"id": 1, "session": "w-1", "n": 1, "started": None, "ended": None,
                                            "status": None, "start_commit": None, "end_commit": None,
                                            "exit_code": None, "tokens": 7, "final": None}]
    assert all("cost" not in [r["name"] for r in db.q(f"PRAGMA table_info({t})")] for t in ("goals", "sessions"))


def test_trim_words_keeps_tail():
    text, cut = digest.trim_words(" ".join(f"w{i}" for i in range(1000)), 100)
    assert cut and "w0" in text and "w999" in text
    assert len(text.split()) == 101  # 60 head + marker + 40 tail
    report = "**Result:** done\n" + "x " * 300 + "\n**Issues:** none\n**Next:** ship"
    text, cut = digest.trim_words(report, 50)
    assert cut and text.startswith("**Result:** done\n") and text.endswith("**Issues:** none\n**Next:** ship")
    assert digest.trim_words("a\nb", 5) == ("a\nb", False)


def guard_env(tmp_path: Path, role: str) -> dict:
    return {"LOOP_ROLE": role, "LOOP_WS": str(tmp_path), "LOOP_SESSION": "w-1", "LOOP_GOAL": "m1",
            "LOOP_GUARD_PATTERNS": json.dumps(["aws .*prod"])}


def test_guard_rules(tmp_path):
    for d in ("wt/w-1", "goals/m1/runs/w-1", "repo", "meta"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    w, m = guard_env(tmp_path, "worker"), guard_env(tmp_path, "meta-work")
    ev = lambda tool, **inp: {"tool_name": tool, "tool_input": inp, "cwd": str(tmp_path / "wt/w-1")}
    assert guard.check(ev("Bash", command="git push origin main"), w)
    assert guard.check(ev("Bash", command="aws s3 ls --profile prod"), w)
    assert guard.check(ev("Bash", command="echo x > $LOOP_GOAL_DIR/goal.md"), w)
    assert guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/spec.md")), w)
    assert guard.check(ev("Write", file_path=str(tmp_path / "meta/AGENTS.md")), w)
    assert guard.check(ev("Bash", command="rm -rf /"), w)
    assert guard.check(ev("Bash", command="rm -rf /home/other/stuff"), w)
    assert not guard.check(ev("Write", file_path="hello.py"), w)
    assert not guard.check(ev("Write", file_path=".artifacts/plot.png"), w)
    assert not guard.check(ev("Write", file_path=str(tmp_path / "data/raw.csv")), w)
    # Workers write only in their checkout and data/, and messages name no platform concepts
    reason = guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/runs/w-1/a.txt")), w)
    assert reason and "worktree" not in reason and "run dir" not in reason
    assert "journal" not in guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/spec.md")), w)
    assert not guard.check(ev("Bash", command="rm -rf build && pytest -q"), w)
    # Meta-work changes code only in its goal's integration worktree; push and human-owned files stay blocked
    assert not guard.check(ev("Edit", file_path=str(tmp_path / "wt/m1/app.py")), m)
    assert guard.check(ev("Edit", file_path=str(tmp_path / "repo/app.py")), m)
    assert guard.check(ev("Bash", command="git -C ../repo commit -am x"), m)
    assert guard.check(ev("Bash", command="git push"), m)
    assert guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/goal.md")), m)
    assert not guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/plan.md")), m)
    patch = "*** Begin Patch\n*** Update File: " + str(tmp_path / "goals/m1/goal.md") + "\n@@\n*** End Patch"
    assert guard.check(ev("apply_patch", command=patch), w)
    assert guard.check(ev("Bash", command="ls"), {}) is None


def test_guard_blocks_polling(tmp_path):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    w, m = guard_env(tmp_path, "worker"), guard_env(tmp_path, "meta-chat")
    ev = lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path / "wt/w-1")}
    for cmd in ('until ! pgrep -f "tools.score" >/dev/null; do sleep 5; done; cat out.err',
                "while [ ! -f out.json ]\ndo\n  sleep 10\ndone", "bash -c 'while true; do sleep 1; done'",
                "tail -f job.log", "timeout 60 tail -F job.log", "watch -n 5 nvidia-smi"):
        assert guard.check(ev(cmd), w) and guard.check(ev(cmd), m), cmd
    # Retries, short pauses, one-off reads and the loops of a `loop run` job pass
    for cmd in ("for i in 1 2 3; do kaggle datasets download x && break; sleep 30; done", "sleep 1; cat lvl.log",
                "tail -n 40 job.log", "while read f; do wc -l $f; done < files.txt",
                "loop run -- bash -c 'until test -f done; do sleep 5; done'", "git log --follow a.py",
                "python - <<'EOF'\nwhile x:\n    time.sleep(1)\nEOF"):
        assert not guard.check(ev(cmd), w), cmd


def test_guard_ignores_prompt_text(tmp_path):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    m = guard_env(tmp_path, "meta-work")
    ev = lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
    spawn = "loop spawn --role coder <<'EOF'\nImplement X, git commit your work, never git push.\nRead $LOOP_GOAL_DIR/goal.md > notes\nEOF"
    assert guard.check(ev(spawn), m) is None
    assert guard.check(ev('loop note "please git commit more often"'), m) is None
    assert guard.check(ev("cd ../repo && git push"), m)
    assert guard.check(ev("git push origin HEAD"), guard_env(tmp_path, "worker"))
    assert guard.check(ev("cat <<EOF > $LOOP_GOAL_DIR/goal.md\nx\nEOF"), m)


def test_guard_agy_payload(tmp_path, monkeypatch, capsys):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    w, m = guard_env(tmp_path, "worker"), guard_env(tmp_path, "meta-work")
    # agy runs hooks in the plugin dir: the agent's cwd comes from the call or the session's workspace
    ev = lambda name, **args: {"toolCall": {"name": name, "args": args}, "workspacePaths": [str(tmp_path / "wt/w-1")]}
    assert guard.check(ev("run_command", CommandLine="git push"), w)
    assert guard.check(ev("run_command", CommandLine="git -C ../../repo commit -am x", Cwd=str(tmp_path / "wt/w-1")), m)
    assert not guard.check(ev("run_command", CommandLine="pytest -q"), w)
    assert guard.check(ev("write_to_file", TargetFile=str(tmp_path / "goals/m1/goal.md")), w)
    assert not guard.check(ev("replace_file_content", TargetFile=str(tmp_path / "wt/w-1/app.py")), w)
    assert guard.check(ev("write_to_file", TargetFile="/x"), guard_env(tmp_path, "meta-advisor"))
    # A denial is a JSON decision on stdout, not an exit code
    for key, val in w.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ev("run_command", CommandLine="git push"))))
    guard.main()
    assert json.loads(capsys.readouterr().out)["decision"] == "deny"


def test_agy_plugin_and_worker_text(tmp_path, monkeypatch):
    from agentloop import workspace
    monkeypatch.setattr(workspace, "AGY_PLUGIN", tmp_path / "plugin")
    hooks = json.loads((workspace.agy_plugin() / "hooks.json").read_text())["agentloop-guard"]["PreToolUse"][0]
    assert "run_command" in hooks["matcher"] and hooks["hooks"][0]["command"].startswith('[ -z "$LOOP_ROLE" ] ||')
    ws = tmp_path / "ws"
    (ws / "worker/skills/plot").mkdir(parents=True)
    (ws / "worker/AGENTS.md").write_text("# Environment\n")
    assert workspace.agy_worker_text(ws) == "# Environment"
    (ws / "worker/skills/plot/SKILL.md").write_text("---\nname: plot\ndescription: >-\n  Use for\n  plots.\n---\nx\n")
    assert workspace.agy_worker_text(ws).endswith(f"- plot: Use for plots. ({ws / 'worker/skills/plot/SKILL.md'})")


def test_agy_catalog_groups_efforts(monkeypatch):
    import asyncio
    from agentloop import agents

    async def fake(argv, timeout=30):
        return 0, ("Fetching available models...\ngemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
                   "gemini-4-argon-high\tGemini 4 Argon (High)\ngemini-4-argon-low\tGemini 4 Argon (Low)\n"
                   "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n")
    monkeypatch.setattr(agents, "run_cmd", fake)
    assert asyncio.run(agents._agy_catalog()) == [
        {"id": "gemini-4-argon", "name": "Gemini 4 Argon", "efforts": ["low", "medium", "high"]},
        {"id": "gpt-oss-120b", "name": "GPT-OSS 120B", "efforts": ["medium"]}]


def test_guard_blocks_only_writes_to_human_files(tmp_path):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    w, m = guard_env(tmp_path, "worker"), guard_env(tmp_path, "meta-work")
    ev = lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
    # Reads stay allowed even next to stderr redirections or a copy out
    for cmd in ("cat goals/m1/spec.md; cat goals/m1/goal.yaml 2>/dev/null; ls -la goals/m1/",
                f"cat {tmp_path}/goals/m1/goal.md && ls tests .artifacts 2>&1; cat .gitignore",
                "cp goals/m1/goal.md /tmp/g.md", "grep -c x goals/m1/goal.md > /tmp/count"):
        assert guard.check(ev(cmd), m) is None, cmd
    for cmd in ("echo x >> goals/m1/goal.md", "echo x 2>/dev/null >goals/m1/spec.md", "sed -i s/a/b/ goals/m1/spec.md",
                "cp /tmp/g.md goals/m1/goal.md", "date | tee -a goals/m1/goal.md", "mv goals/m1/goal.md /tmp/",
                "X=1 truncate -s0 loop.yaml", "echo {} > meta/hooks.json"):
        assert guard.check(ev(cmd), m), cmd
    # The worker's message is an ordinary environment limit, naming no platform
    reason = guard.check(ev("echo x > goals/m1/goal.md"), w)
    assert reason == "goals/m1/goal.md is read-only"


def test_guard_meta_scope(tmp_path):
    for d in ("wt/w-1", "wt/m1", "wt/m2", "repo", "meta"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    m, c = guard_env(tmp_path, "meta-work"), guard_env(tmp_path, "meta-chat")
    ev = lambda cmd, cwd="meta": {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path / cwd)}
    # Allowed for the meta-work: its integration worktree, its own files, reads anywhere
    for cmd in ("cd ../wt/m1 && echo x > a.py && git add -A && git commit -qm x",
                "git -C ../wt/m1 checkout -- a.py", "sed -i s/a/b/ ../wt/m1/a.py",
                "echo - x >> ../goals/m1/plan.md",
                "git -C ../repo log --oneline | head", "cat ../wt/w-1/a.py > /tmp/a.py", "cd ../repo && git status"):
        assert guard.check(ev(cmd), m) is None, cmd
    assert guard.check(ev("echo x > a.py", "wt/m1"), m) is None
    # Blocked: repo/, worker worktrees, other goals' integration, also after a cd or with git -C
    for cmd in ("echo x > ../repo/a.py", "cd ../repo && git checkout -b x", "git -C ../wt/w-1 commit -am x",
                "cp /tmp/a.py ../wt/m2/a.py", "cd ../wt/w-1; rm a.py", "mv ../wt/m1/a.py ../repo/"):
        assert guard.check(ev(cmd), m), cmd
    assert guard.check(ev("git commit -am x", "repo"), m)
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "wt/w-1/a.py")},
                        "cwd": str(tmp_path)}, m)
    # A meta-chat reads the project but changes no code, not even in the integration worktree
    assert guard.check(ev("cat ../wt/m1/a.py && git -C ../wt/m1 log"), c) is None
    assert "loop note" in guard.check(ev("echo x > ../wt/m1/a.py"), c)
    assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "wt/m1/a.py")},
                        "cwd": str(tmp_path)}, c)
    # Workers keep their old rules: the shell scope check is for meta sessions only
    assert guard.check(ev("echo x > ../repo/a.py", "wt/w-1"), guard_env(tmp_path, "worker")) is None


def test_legacy_integration_worktree_moves(tmp_path):
    from agentloop import gitops
    repo = tmp_path / "repo"
    repo.mkdir()
    gitops.git(repo, "init", "-q", "-b", "main")
    gitops.git(repo, *gitops.BOT, "commit", "-q", "--allow-empty", "-m", "init")
    gitops.git(repo, "branch", "loop/g1/integration")
    gitops.git(repo, "worktree", "add", "-q", str(tmp_path / "wt" / "_int-g1"), "loop/g1/integration")
    (tmp_path / "wt" / "_int-g1" / "wip.txt").write_text("uncommitted\n")
    assert gitops.goal_refs(tmp_path, "g1") == ["loop/g1/integration"] and not gitops.goal_refs(tmp_path, "g")
    assert gitops.move_legacy_worktree(tmp_path, "g1")
    wt = gitops.int_worktree(tmp_path, "g1")
    assert (wt / "wip.txt").exists() and not (tmp_path / "wt" / "_int-g1").exists()
    assert gitops.current_branch(wt) == "loop/g1/integration"
    assert not gitops.move_legacy_worktree(tmp_path, "g1")  # once only


def test_checkpoint_and_restore_after_run(tmp_path):
    from agentloop import gitops
    repo = tmp_path / "repo"
    repo.mkdir()
    gitops.git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("a\n")
    gitops.git(repo, "add", "-A")
    gitops.git(repo, *gitops.BOT, "commit", "-qm", "init")
    wt = gitops.ensure_integration(tmp_path, "g1", "main")
    # The meta-work's change is committed under its name; .venv/ stays out from the start
    (wt / "b.py").write_text("b\n")
    (wt / ".venv").mkdir()
    (wt / ".venv" / "x").write_text("")
    head = gitops.checkpoint(tmp_path, "g1", "meta-work(mw-1)", "meta-work(mw-1): turn 1")
    assert gitops.git(wt, "log", "-1", "--format=%an|%s") == "meta-work(mw-1)|meta-work(mw-1): turn 1"
    assert gitops.git(wt, "show", "--name-only", "--format=", head) == "b.py"
    assert gitops.checkpoint(tmp_path, "g1", "meta-work(mw-1)", "again") == head  # nothing new, no commit
    # Off the integration branch, nothing is committed and the caller is told
    gitops.git(wt, "checkout", "-q", "-b", "side")
    (wt / "c.py").write_text("c\n")
    try:
        gitops.checkpoint(tmp_path, "g1", "meta-work(mw-1)", "x")
        raise AssertionError("checkpoint must refuse a worktree off its branch")
    except RuntimeError as exc:
        assert "side" in str(exc)
    gitops.git(wt, "checkout", "-q", "loop/g1/integration")
    gitops.checkpoint(tmp_path, "g1", "meta-work(mw-1)", "c")
    # A run's tracked edits and new files are undone; files that were there before and ignored ones stay
    (wt / "keep.txt").write_text("mine\n")
    before = gitops.untracked(wt)
    (wt / "a.py").write_text("changed\n")
    (wt / "out" / "deep").mkdir(parents=True)
    (wt / "out" / "deep" / "log.txt").write_text("")
    (wt / ".venv" / "y").write_text("")
    changed, created = gitops.restore_after_run(wt, before)
    assert "changed" in changed and created == ["out/deep/log.txt"]
    assert (wt / "a.py").read_text() == "a\n" and not (wt / "out").exists()
    assert (wt / "keep.txt").exists() and (wt / ".venv" / "y").exists()


def test_guard_rm_edge_cases(tmp_path):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    w = guard_env(tmp_path, "worker")
    ev = lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path / "wt/w-1")}
    assert guard.check(ev(f"rm -rf {tmp_path}/repo\nls"), w)
    assert guard.check(ev('rm -rf "${HOME}/.config"'), w)
    assert guard.check(ev("rm -rf ../../repo"), w)
    assert guard.check(ev("echo $((1<<2))\ngit push"), w)
    assert not guard.check(ev("rm -rf build dist && pytest -q"), w)


def test_config_errors_use_catalog(tmp_path, monkeypatch):
    from agentloop import agents
    catalog = {"claude": {"version": "2.1", "models": [{"id": "opus", "efforts": ["low", "high"]},
                                                      {"id": "haiku", "efforts": []}]},
               "codex": {"version": "0.1", "models": [{"id": "gpt-x", "efforts": ["low", "max"]}]}}
    monkeypatch.setattr(agents, "load_models", lambda: catalog)
    ok = {"meta_work": {"agent": "claude", "model": "opus", "effort": "high"},
          "workers": {"codex": {"enabled": True, "model": "gpt-x", "effort": "max"},
                      "claude": {"enabled": False, "model": "nope"}}}
    assert agents.config_errors(ok) == []
    bad = {"meta_work": {"agent": "claude", "model": "haiku", "effort": "high"},
           "workers": {"codex": {"model": "gpt-y"}, "gemini": {}}}
    errors = agents.config_errors(bad)
    assert len(errors) == 3 and "haiku" in errors[0] and "gpt-y" in errors[1] and "gemini" in errors[2]


def test_claude_catalog_filters_by_version(tmp_path, monkeypatch):
    from agentloop import agents
    cache = tmp_path / ".claude/cache/model-catalog"
    cache.mkdir(parents=True)
    models = [{"id": "claude-new", "short_name": "Opus", "section": "main", "min_claude_code_version": "2.2.0",
               "thinking": {"effort_options": [{"id": "high"}]}},
              {"id": "claude-old", "short_name": "Opus", "section": "main", "thinking": {"effort_options": [{"id": "low"}]}}]
    (cache / "acct-cc.json").write_text(json.dumps({"catalog": {"config": {"models": models}}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ids = [m["id"] for m in agents._claude_catalog("2.1.285 (Claude Code)")]
    assert ids == ["opus", "claude-old"]


def test_language_rule():
    from agentloop.engine import Engine
    assert Engine.language_rule("English") == "" and Engine.language_rule(None) == ""
    rule = Engine.language_rule("Vietnamese")
    assert "chat replies in Vietnamese" in rule and rule.endswith("Write worker prompts in English.")


def test_slug_transliterates():
    from agentloop.gitops import slug
    assert slug("Lọc từ theo số lần xuất hiện", 40) == "loc-tu-theo-so-lan-xuat-hien"
    assert slug("Đường dẫn") == "duong-dan" and slug("!!!") == "w"


def test_guard_blocks_global_installs(tmp_path):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    w = guard_env(tmp_path, "worker")
    ev = lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path / "wt/w-1")}
    assert guard.check(ev("python -m pip install --user pytest==8.4.2"), w)
    assert guard.check(ev("pip install --break-system-packages x"), w)
    assert guard.check(ev("sudo apt install jq"), w)
    assert guard.check(ev("conda install numpy"), w)
    assert not guard.check(ev("python -m venv .venv && .venv/bin/pip install pytest"), w)
    assert not guard.check(ev("conda install -p ./env numpy"), w)


def test_enforce_protected_reverts_a_single_edit(tmp_path):
    from agentloop import gitops, workspace
    gitops.git(tmp_path, "init", "-q")
    (tmp_path / "goals/g").mkdir(parents=True)
    (tmp_path / "goals/g/goal.md").write_text("- criterion\n")
    workspace.commit(tmp_path, "human", "init")
    # One unstaged edit: the first (and only) porcelain line, the case a fixed-column parse got wrong
    (tmp_path / "goals/g/goal.md").write_text("- nothing\n")
    (tmp_path / "goals/g/spec.md").write_text("new\n")
    assert sorted(workspace.enforce_protected(tmp_path)) == ["goals/g/goal.md", "goals/g/spec.md"]
    assert (tmp_path / "goals/g/goal.md").read_text() == "- criterion\n" and not (tmp_path / "goals/g/spec.md").exists()
    assert list((tmp_path / ".loop/reverted").rglob("goal.md"))


def test_agent_commit_leaves_human_files_for_enforce_protected(tmp_path):
    from agentloop import gitops, workspace
    gitops.git(tmp_path, "init", "-q")
    (tmp_path / "goals/g").mkdir(parents=True)
    (tmp_path / "goals/g/goal.md").write_text("- criterion\n")
    workspace.commit(tmp_path, "human", "init")
    # Another agent's commit lands while a worker's edit to goal.md is pending: it must not seal that edit
    (tmp_path / "goals/g/goal.md").write_text("- nothing\n")
    (tmp_path / "goals/g/plan.md").write_text("# Plan\n")
    assert workspace.commit(tmp_path, "meta-work(mw-1)", "turn", protected=False)
    assert gitops.git(tmp_path, "show", "--name-only", "--format=", "HEAD") == "goals/g/plan.md"
    assert workspace.enforce_protected(tmp_path) == ["goals/g/goal.md"]
    assert (tmp_path / "goals/g/goal.md").read_text() == "- criterion\n"


def test_claude_settings_exclude_instructions_above_the_agent(tmp_path):
    from agentloop import workspace
    ws = tmp_path / "home" / "workspaces" / "demo"
    for layer in ("meta", "worker"):
        (ws / layer).mkdir(parents=True)
        (ws / layer / "hooks.json").write_text('{"hooks": {"PreToolUse": []}}')
    worker, meta = (workspace.claude_settings_doc(ws, layer) for layer in ("worker", "meta"))
    assert worker["hooks"] == {"PreToolUse": []}
    # The platform repo (any parent) and the workspace root are hidden; meta/ and the worktree keep their own
    for doc in (worker, meta):
        assert str(tmp_path / "home" / "CLAUDE.md") in doc["claudeMdExcludes"]
        assert str(tmp_path / "home" / "AGENTS.md") in doc["claudeMdExcludes"]
        assert str(tmp_path / "home" / ".claude" / "AGENTS.md") in doc["claudeMdExcludes"]
        assert str(ws / "CLAUDE.md") in doc["claudeMdExcludes"]
        assert not any(x.startswith((str(ws / "meta"), str(ws / "wt" / "w-1"))) for x in doc["claudeMdExcludes"])
    assert str(ws / "wt" / "AGENTS.md") in worker["claudeMdExcludes"]
    assert workspace.claude_settings(ws, "worker").read_text().startswith("{")
    # No MCP server unless loop.yaml allows it, claude.ai connectors included
    assert meta["allowedMcpServers"] == []
    (ws / "loop.yaml").write_text("mcp_servers: [https://pubmed.mcp.claude.com/mcp]\n")
    assert workspace.claude_settings_doc(ws, "meta")["allowedMcpServers"] == [
        {"serverUrl": "https://pubmed.mcp.claude.com/mcp"}]


def test_sync_harness_keeps_human_edits(tmp_path, monkeypatch):
    from agentloop import workspace
    tpl = tmp_path / "tpl" / "workspace"
    for rel, text in (("PLATFORM.md", "p1"), ("meta/AGENTS.md", "m1"), ("worker/AGENTS.md", "w1")):
        (tpl / rel).parent.mkdir(parents=True, exist_ok=True)
        (tpl / rel).write_text(text)
    monkeypatch.setattr(workspace, "TEMPLATES", tmp_path / "tpl")
    ws = tmp_path / "ws"
    assert sorted(workspace.sync_harness(ws)["updated"]) == ["PLATFORM.md", "meta/AGENTS.md", "worker/AGENTS.md"]
    # Human customises meta, deletes PLATFORM.md; the template then changes everywhere
    (ws / "meta/AGENTS.md").write_text("mine")
    (ws / "PLATFORM.md").unlink()
    for rel, text in (("PLATFORM.md", "p2"), ("meta/AGENTS.md", "m2"), ("worker/AGENTS.md", "w2")):
        (tpl / rel).write_text(text)
    assert workspace.sync_harness(ws) == {"updated": ["worker/AGENTS.md"], "kept": ["meta/AGENTS.md"]}
    assert (ws / "worker/AGENTS.md").read_text() == "w2" and (ws / "meta/AGENTS.md").read_text() == "mine"
    assert not (ws / "PLATFORM.md").exists()


def test_claude_uses_user_autocompact(tmp_path, monkeypatch):
    from agentloop.adapters import claude
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"autoCompactWindow": 500000, "autoCompactEnabled": True}))
    monkeypatch.setattr(claude, "USER_SETTINGS", settings)
    argv = lambda ac=None: ClaudeAdapter().argv(RunSpec(agent="claude", kind="meta", cwd=tmp_path, prompt="x", autocompact=ac))
    assert argv()[argv().index("--autocompact") + 1] == "500000"
    assert argv("300k")[argv("300k").index("--autocompact") + 1] == "300000"
    settings.write_text(json.dumps({"autoCompactWindow": 500000, "autoCompactEnabled": False}))
    assert "--autocompact" not in argv()


def test_codex_context_from_rollout(tmp_path):
    day = tmp_path / "sessions/2026/10/01"
    day.mkdir(parents=True)
    usage = lambda n: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": n}}}})
    (day / "rollout-2026-10-01T16-53-05-T1.jsonl").write_text(usage(100) + "\n" + usage(18702) + "\n")
    st = ParseState(vendor_id="T1", context=104139)
    CodexAdapter().finish(st, RunSpec(agent="codex", kind="worker", cwd=tmp_path, prompt="x", codex_home=tmp_path))
    assert st.context == 18702


def test_codex_turn_tokens_from_rollout(tmp_path):
    # The rollout holds the thread's running total: a turn gets its growth, whether it completed or was stopped
    day = tmp_path / "sessions/2026/10/06"
    day.mkdir(parents=True)
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    task = lambda t: json.dumps({"timestamp": stamp(t), "type": "event_msg", "payload": {"type": "task_started"}})
    count = lambda t, n: json.dumps({"timestamp": stamp(t), "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": n, "cached_input_tokens": n // 2, "output_tokens": 10},
        "last_token_usage": {"total_tokens": 70}}}})
    start = time.time()
    (day / "rollout-2026-10-06T16-45-19-T1.jsonl").write_text("\n".join(
        [task(start - 100), count(start - 90, 1000), task(start + 1), count(start + 2, 1400), count(start + 3, 1600)]))
    spec = RunSpec(agent="codex", kind="worker", cwd=tmp_path, prompt="x", codex_home=tmp_path)
    for reported in (810, 0):  # turn.completed's running total, or nothing for a stopped turn
        st = ParseState(vendor_id="T1", started=start, tokens=reported)
        CodexAdapter().finish(st, spec)
        assert (st.tokens, st.context) == (810 - 510, 70)
    st = ParseState(vendor_id="T1", started=start + 10)  # stopped before its task started
    CodexAdapter().finish(st, spec)
    assert st.tokens == 0


def test_codex_quota_from_rollout(tmp_path):
    day = tmp_path / "sessions/2026/10/17"
    day.mkdir(parents=True)
    limits = {"primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": 1792226188},
              "secondary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": 1792655184}}
    (day / "rollout-2026-10-17T11-14-22-T1.jsonl").write_text(json.dumps(
        {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": limits}}) + "\n")
    st = ParseState(vendor_id="T1")
    CodexAdapter().finish(st, RunSpec(agent="codex", kind="worker", cwd=tmp_path, prompt="x", codex_home=tmp_path))
    assert st.quota == [{"name": "5h", "used": 0.01, "resets_at": 1792226188},
                        {"name": "7d", "used": 0.17, "resets_at": 1792655184}]


def test_claude_stopped_turn_counts_its_calls():
    # Without `result` the turn keeps what its model calls used; `result` then has the final word
    st, ad = ParseState(), ClaudeAdapter()
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 1}
    for _ in range(2):
        ad.parse({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": usage}}}, st)
        ad.parse({"type": "stream_event", "event": {"type": "message_delta", "usage": {**usage, "output_tokens": 8}}}, st)
    ad.parse({"type": "stream_event", "parent_tool_use_id": "t",
              "event": {"type": "message_start", "message": {"usage": usage}}}, st)
    assert st.tokens == 2 * (2 + 100 + 8)
    ad.parse({"type": "result", "subtype": "success", "result": "ok", "usage": {"input_tokens": 4, "output_tokens": 16}}, st)
    assert st.tokens == 20


def test_reap_scope_stops_what_a_turn_left_running():
    import subprocess
    import time
    import pytest
    from agentloop import runner
    if not runner.systemd_available():
        pytest.skip("needs systemd --user")
    unit = f"{runner.unit_prefix()}-test-{int(time.time() * 1000)}"
    # The scope's main process exits at once, leaving a detached job behind, as a CLI ending its turn would
    subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "--",
                    "bash", "-c", "setsid sleep 300 >/dev/null 2>&1 < /dev/null & exit 0"], check=True)
    assert runner.reap_scope(unit, 0) == ("sleep 300",)
    for _ in range(50):
        if not runner.reap_scope(unit, 0):
            break
        time.sleep(0.1)
    assert runner.reap_scope(unit, 0) == ()


def test_envelope_drops_expired_quota_warnings():
    import time
    from agentloop import digest
    item = lambda text, until: {"kind": "system", "payload": json.dumps({"text": text, "until": until})}
    out = digest.envelope([item("[System] old", time.time() - 60), item("[System] live", time.time() + 60)])
    assert "old" not in out and "live" in out


def test_usage_limit_until_reads_the_reset():
    import time
    from agentloop.adapters.base import usage_limit_until
    now = time.mktime((2026, 10, 6, 19, 55, 0, 0, 0, -1))
    assert usage_limit_until("exit 1: rate limit exceeded, retry", now) is None
    assert usage_limit_until("Claude AI usage limit reached|1791300000", now) == 1791300000.0
    at = usage_limit_until("You've hit your usage limit. Upgrade to Pro ... or try again at 9:45 PM.", now)
    assert time.localtime(at)[3:5] == (21, 45) and at > now
    morning = usage_limit_until("You've hit your limit · resets 3am (Asia/Saigon)", now)
    assert time.localtime(morning)[2:5] == (7, 3, 0)
    assert time.localtime(usage_limit_until("You've hit your session limit · resets 5pm (Asia/Bangkok)", now))[2:5] \
        == (7, 17, 0)
    assert usage_limit_until("usage limit reached", now) == now + 3600


def test_wsl_disk_free_counts_the_virtual_disks_own_free_blocks(monkeypatch, tmp_path):
    vhdx = tmp_path / "ext4.vhdx"
    vhdx.write_bytes(b"")
    os.truncate(vhdx, 200 * 2**30)  # sparse: the virtual disk has grown to 200 GiB
    usage = namedtuple("usage", "total used free")
    gib = 2**30
    sizes = {"/": usage(1000 * gib, 140 * gib, 860 * gib), str(vhdx.parents[-3]): usage(238 * gib, 202 * gib, 36 * gib)}
    monkeypatch.setattr(infra.psutil, "disk_usage", lambda p: sizes[str(p)])
    probe = infra.Infra.__new__(infra.Infra)
    probe.disk_path, probe.vhdx, probe.history, probe.gpu_reading = "/", vhdx, deque(), (0.0, [])
    # 60 GiB free inside the disk (deleted files never shrink it) plus 36 GiB it can still grow into
    snap = probe.sample()
    assert (snap["disk_free_gb"], snap["disk_total_gb"]) == (96, 236)
