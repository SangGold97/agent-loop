import json
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
                add_dirs=[tmp_path / "mission"], system_text="MODE", last_message_file=tmp_path / "l.txt")
    a = CodexAdapter().argv(s)
    assert a[:4] == ["codex", "exec", "resume", "t1"] and a[-1] == "-"
    assert 'model_reasoning_effort="max"' in a and "-C" not in a
    assert any("writable_roots" in x for x in a) and "--dangerously-bypass-approvals-and-sandbox" not in a


def test_codex_parse_stream():
    st, ad = ParseState(), CodexAdapter()
    lines = [{"type": "thread.started", "thread_id": "T"},
             {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
             {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "a", "exit_code": 0}},
             {"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}},
             {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}]
    events = [e for obj in lines for e in ad.parse(obj, st)]
    assert st.vendor_id == "T" and st.last_text == "DONE" and st.tokens == 105
    assert [e["type"] for e in events] == ["init", "tool", "tool_result", "text", "usage"]


def test_claude_parse_result():
    st, ad = ParseState(), ClaudeAdapter()
    ad.parse({"type": "system", "subtype": "init", "session_id": "S"}, st)
    ad.parse({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                              "input": {"command": "ls"}}]}}, st)
    ad.parse({"type": "result", "subtype": "success", "result": "ok", "session_id": "S", "total_cost_usd": 0.5,
              "usage": {"input_tokens": 1, "output_tokens": 2}}, st)
    assert (st.vendor_id, st.final, st.cost, st.tokens, st.is_error) == ("S", "ok", 0.5, 3, False)


def test_verify_and_checklist():
    goal = "- tests pass\n- verify: `make test`\nverify: python -c 'print(1)'\n"
    assert digest.verify_commands(goal) == ["make test", "python -c 'print(1)'"]
    assert digest.checklist("- [x] a\n- [ ] b\n* [X] c") == (2, 3)


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
            "LOOP_RUN_DIR": str(tmp_path / "missions/m1/runs/w-1"), "LOOP_GUARD_PATTERNS": json.dumps(["aws .*prod"])}


def test_guard_rules(tmp_path):
    for d in ("wt/w-1", "missions/m1/runs/w-1", "repo", "meta"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    w, m = guard_env(tmp_path, "worker"), guard_env(tmp_path, "meta-work")
    ev = lambda tool, **inp: {"tool_name": tool, "tool_input": inp, "cwd": str(tmp_path / "wt/w-1")}
    assert guard.check(ev("Bash", command="git push origin main"), w)
    assert guard.check(ev("Bash", command="aws s3 ls --profile prod"), w)
    assert guard.check(ev("Bash", command="echo x > $LOOP_MISSION_DIR/goal.md"), w)
    assert guard.check(ev("Write", file_path=str(tmp_path / "missions/m1/spec.md")), w)
    assert guard.check(ev("Write", file_path=str(tmp_path / "meta/AGENTS.md")), w)
    assert guard.check(ev("Bash", command="rm -rf /"), w)
    assert guard.check(ev("Bash", command="rm -rf /home/other/stuff"), w)
    assert not guard.check(ev("Write", file_path="hello.py"), w)
    assert not guard.check(ev("Write", file_path=str(tmp_path / "missions/m1/runs/w-1/artifacts/a.txt")), w)
    assert not guard.check(ev("Bash", command="rm -rf build && pytest -q"), w)
    assert guard.check(ev("Edit", file_path=str(tmp_path / "repo/app.py")), m)
    assert guard.check(ev("Bash", command="git -C ../repo commit -am x"), m)
    assert not guard.check(ev("Write", file_path=str(tmp_path / "missions/m1/plan.md")), m)
    patch = "*** Begin Patch\n*** Update File: " + str(tmp_path / "missions/m1/goal.md") + "\n@@\n*** End Patch"
    assert guard.check(ev("apply_patch", command=patch), w)
    assert guard.check(ev("Bash", command="ls"), {}) is None


def test_guard_ignores_prompt_text(tmp_path):
    (tmp_path / "wt/w-1").mkdir(parents=True)
    m = guard_env(tmp_path, "meta-work")
    ev = lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
    spawn = "loop spawn --role coder <<'EOF'\nImplement X, git commit your work, never git push.\nRead $LOOP_MISSION_DIR/goal.md > notes\nEOF"
    assert guard.check(ev(spawn), m) is None
    assert guard.check(ev('loop note "please git commit more often"'), m) is None
    assert guard.check(ev("cd ../repo && git commit -m 'x'"), m)
    assert guard.check(ev("git push origin HEAD"), guard_env(tmp_path, "worker"))
    assert guard.check(ev("cat <<EOF > $LOOP_MISSION_DIR/goal.md\nx\nEOF"), m)


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
