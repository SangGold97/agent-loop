import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agentloop import digest, guard
from agentloop.adapters.base import RunSpec, ParseState, to_tokens
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
    assert 'model_reasoning_effort="max"' in a and "-C" not in a
    assert "--dangerously-bypass-approvals-and-sandbox" in a and not any("sandbox_mode" in x for x in a)
    assert "project_root_markers=[]" in a
    assert "project_root_markers=[]" not in CodexAdapter().argv(RunSpec(agent="codex", kind="worker", cwd=tmp_path,
                                                                        prompt="x"))


def test_codex_parse_stream():
    st, ad = ParseState(), CodexAdapter()
    lines = [{"type": "thread.started", "thread_id": "T"},
             {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
             {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "a", "exit_code": 0}},
             {"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}},
             {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}}]
    events = [e for obj in lines for e in ad.parse(obj, st)]
    assert st.vendor_id == "T" and st.last_text == "DONE" and st.tokens == 45
    assert [e["type"] for e in events] == ["init", "tool", "tool_result", "text", "usage"]



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


def test_worker_digest_names_the_worker_and_earlier_commits():
    from types import SimpleNamespace
    sess = {"id": "w-1", "role": "reviewer", "label": "review"}
    res = SimpleNamespace(stopped=False, error="", final="**Result:** approved", tokens=88_000, context=23_000)
    cfg = {"max_output_words": 500, "max_diff_lines": 80}
    stat = " a.py | 1 +\n 1 file changed, 1 insertion(+)"
    text = digest.worker_digest(sess, 1, res, stat, "+x", {"out": "o", "diff": "d"}, cfg, ["b63d497 add a"])
    # Identity and outcome only: no duration, tokens or cost
    assert text.splitlines()[0] == "[Worker w-1 · role reviewer · label review · turn 1] ok"
    assert "tokens" not in text and "min" not in text and "$" not in text
    assert "[Note] This diff includes 1 commit(s) made before this turn" in text and "b63d497 add a" in text
    assert "[Note]" not in digest.worker_digest(sess, 1, res, stat, "+x", {"out": "o", "diff": "d"}, cfg)


def test_commits_before_separates_brought_in_work(tmp_path):
    import time
    from agentloop import gitops
    gitops.git(tmp_path, "init", "-q")
    commit = lambda msg, when: gitops.git(tmp_path, *gitops.BOT, "commit", "-q", "--allow-empty", "-m", msg,
                                          env={"GIT_COMMITTER_DATE": f"@{when} +0000"})
    now = int(time.time())
    commit("base", now - 300)
    start = gitops.head(tmp_path)
    commit("by another worker", now - 100)
    commit("by this worker", now + 5)
    assert [c.split(" ", 1)[1] for c in gitops.commits_before(tmp_path, start, gitops.head(tmp_path), now)] \
        == ["by another worker"]


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
    return {"LOOP_ROLE": role, "LOOP_WS": str(tmp_path), "LOOP_SESSION": "w-1",
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
    # Meta-agents may change code and git state; push and human-owned files stay blocked
    assert not guard.check(ev("Edit", file_path=str(tmp_path / "repo/app.py")), m)
    assert not guard.check(ev("Bash", command="git -C ../repo commit -am x"), m)
    assert guard.check(ev("Bash", command="git push"), m)
    assert guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/goal.md")), m)
    assert not guard.check(ev("Write", file_path=str(tmp_path / "goals/m1/plan.md")), m)
    patch = "*** Begin Patch\n*** Update File: " + str(tmp_path / "goals/m1/goal.md") + "\n@@\n*** End Patch"
    assert guard.check(ev("apply_patch", command=patch), w)
    assert guard.check(ev("Bash", command="ls"), {}) is None


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
    assert "chat replies in Vietnamese" in rule and "worker prompts in English" in rule and "commit messages" in rule


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
    assert usage_limit_until("usage limit reached", now) == now + 3600
